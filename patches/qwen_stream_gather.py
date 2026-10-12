"""Streaming PLE n-gram gather: read rows from a memory-mapped checkpoint into pinned staging.

Why this shape
--------------
The vendored mmap path (arm C) hands the *GPU* a host virtual address and requires
PAGEABLE_MEMORY_ACCESS_USES_HOST_PAGE_TABLES, which is 0 on discrete RTX parts -- a hardware
gate, not a configuration problem. Nothing, however, stops the *CPU* from reading pageable
mapped file pages. So: CPU memcpys the needed rows into pinned staging, one batch ahead of
graph replay, and the device reads only DMA-capable memory.

Measured on this shape (47.684 GiB fixture, real n-gram access trace, capped container):
  * row copy beats page-granular pread by ~143x at 2048 tokens (243 us vs 34.7 ms), because page
    staging moves 4096 bytes per 160-byte row;
  * pipelined one batch ahead, exposed cost is 0.22% of prefill at p50 and 0.39% at p99, even
    while the file is cold and 9.9 GiB is read from disk mid-run;
  * decode (16 rows/token) is 16.6 us p50, 25 us p99 with a warm cache;
  * real traces hit 86-94% in the page cache versus the 36-41% a uniform-access model predicts.

Everything here is host-side; dequantisation stays on the existing device path untouched.
"""
from __future__ import annotations

import ctypes
import os
import resource
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np

_MADV_RANDOM = 1
_PROT_READ = 1
_MAP_PRIVATE = 2

_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.mmap.restype = ctypes.c_void_p
_libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int,
                       ctypes.c_int, ctypes.c_long]
_libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_libc.madvise.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]


def _load_rowcopy(so_path: str | None = None):
    """Load the native row-copy helper. Required, not optional: the Python-loop fallback is
    GIL-bound and measures slower as threads are added, so silently degrading would produce
    numbers that look like the design is slow when the harness is."""
    import numpy as np  # noqa: F401  (used in argtypes)
    path = so_path or os.environ.get("PLE_ROWCOPY_SO", "")
    if not path or not os.path.exists(path):
        raise RuntimeError(
            "PLE row-copy helper not found; set PLE_ROWCOPY_SO or build patches/ple_rowcopy.c "
            "(gcc -O3 -shared -fPIC -o ple_rowcopy.so ple_rowcopy.c). Refusing to fall back to "
            "a per-row Python loop, which measures 100x slower and would misreport the design.")
    lib = ctypes.CDLL(path)
    lib.ple_copy_rows.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                  np.ctypeslib.ndpointer(dtype=np.int64, ndim=1,
                                                         flags="C_CONTIGUOUS"),
                                  ctypes.c_long, ctypes.c_long]
    lib.ple_copy_rows.restype = None
    return lib


class StreamGather:
    """Gathers table rows from an mmapped checkpoint into double-buffered pinned staging."""

    def __init__(self, table_path: str, *, row_bytes: int = 160, n_rows: int | None = None,
                 max_rows_per_batch: int = 2048 * 16, threads: int = 24,
                 rowcopy_so: str | None = None, device: str = "cuda",
                 madv_random: bool = True, small_copy_rows: int = 4096,
                 buffers: int = 4):
        import torch

        self.torch = torch
        self.row_bytes = int(row_bytes)
        self.fd = os.open(table_path, os.O_RDONLY)
        self.size = os.fstat(self.fd).st_size
        self.n_rows = int(n_rows) if n_rows else self.size // self.row_bytes
        if self.n_rows * self.row_bytes > self.size:
            raise ValueError(
                f"{table_path}: {self.size} bytes cannot hold {self.n_rows} rows x "
                f"{self.row_bytes}; n_rows from the safetensors header, not guessed")

        self.base = _libc.mmap(None, self.size, _PROT_READ, _MAP_PRIVATE, self.fd, 0)
        if not self.base or self.base == ctypes.c_void_p(-1).value:
            raise OSError(f"mmap({table_path}) failed: {os.strerror(ctypes.get_errno())}")
        if madv_random:
            # Random 4 KiB fetches over a 47 GiB table: without this the kernel reads ahead and
            # turns 16 useful rows into megabytes of unwanted I/O.
            _libc.madvise(ctypes.c_void_p(self.base), self.size, _MADV_RANDOM)

        self._copy = _load_rowcopy(rowcopy_so)
        self.threads = max(1, int(threads))
        self.device = device
        self.max_rows = int(max_rows_per_batch)
        nbytes = self.max_rows * self.row_bytes
        # A ring of arenas, each guarded by the CUDA event of its last H2D.
        #
        # Two buffers and one-batch-ahead pipelining are NOT safe: filling batch k+2 writes the
        # buffer that batch k used, and k's copy_ was issued non-blocking one iteration earlier, so
        # the CPU can overwrite pinned memory the DMA engine is still reading. That is both a
        # correctness hole and the explanation for a measured effect -- with two buffers, the last
        # quartile of a 600-batch run filled 5x slower than the first while faulting zero pages,
        # which is what competing with an in-flight DMA over the same cache lines looks like.
        # Reuse now waits on the buffer's event, and depth is configurable rather than hard-coded.
        self._nbuf = int(buffers)
        self._arena = [torch.empty(nbytes, dtype=torch.uint8, pin_memory=True)
                       for _ in range(self._nbuf)]
        self._arena_base = [int(a.data_ptr()) for a in self._arena]
        self._free_ev = [torch.cuda.Event() for _ in range(self._nbuf)]
        self._ev_armed = [False] * self._nbuf
        self._dst = torch.empty(nbytes, dtype=torch.uint8, device=device)
        # Two executors on purpose. _pool runs one whole-batch fill at a time (the pipelined
        # path); _fan spreads that fill's row-copy slices across threads. Sharing one pool means
        # a fill submits children to the pool it occupies, so 1 + threads tasks exceed
        # max_workers and correctness depends on the queue draining -- a latent deadlock if a
        # second fill ever overlaps.
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ple-fill")
        self._fan = ThreadPoolExecutor(max_workers=self.threads, thread_name_prefix="ple-fan")
        self._lock = threading.Lock()
        # FIFO of (future, buffer, count). A single slot cannot express "filling k+1 while the
        # device still needs k", which is the entire premise of the design; the first version used
        # one slot and silently degenerated to synchronous fills.
        self._queued: "deque" = deque()
        self._next_buf = 0
        self._small_copy_rows = int(small_copy_rows)
        self.stats = {"batches": 0, "rows": 0, "fill_s": 0.0, "disk_hints": 0}
        self.rusage = []   # (batches, rows, minflt, majflt) sampled at each wait()

    # ------------------------------------------------------------------ fill (host side)
    def _fill(self, byte_off: np.ndarray, buf: int) -> float:
        n = len(byte_off)
        if n > self.max_rows:
            raise ValueError(f"{n} rows exceeds staging capacity {self.max_rows}")
        if self._ev_armed[buf]:
            self._free_ev[buf].synchronize()   # do not overwrite a buffer the DMA engine still owns
        base = self._arena_base[buf]
        k = self.threads
        bounds = [int(x) for x in np.linspace(0, n, k + 1)]
        cbase = ctypes.c_void_p(self.base)

        def work(i):
            lo, hi = bounds[i], bounds[i + 1]
            if hi <= lo:
                return
            self._copy.ple_copy_rows(cbase, ctypes.c_void_p(base + lo * self.row_bytes),
                                     byte_off[lo:hi], hi - lo, self.row_bytes)

        t0 = time.perf_counter()
        if n <= self._small_copy_rows or k <= 1:
            # Rebind the slice BEFORE copying: work(0) reads bounds[0]:bounds[1], and the
            # linspace bounds from above would hand it a 1/k slice. Written the other way round
            # this copies a fraction of the batch and every gate downstream reads stale bytes.
            bounds = [0, n]
            work(0)          # inline: pool dispatch costs more than the copy for small n
        else:
            list(self._fan.map(work, range(k)))
        return time.perf_counter() - t0

    def submit(self, row_ids: np.ndarray) -> None:
        """Begin filling staging for these row ids into the buffer not currently in use.

        row_ids are absolute table row indices in [0, n_rows). Call this one batch ahead of
        consuming, which is legal because n-gram ids are computable on the host before graph
        replay (the MTP drafts come from the previous step).
        """
        ids = np.ascontiguousarray(row_ids, dtype=np.int64).reshape(-1)
        if ids.size and (int(ids.min()) < 0 or int(ids.max()) >= self.n_rows):
            raise ValueError(f"row id out of range: [{int(ids.min())}, {int(ids.max())}] "
                             f"vs n_rows={self.n_rows}")
        with self._lock:
            buf = self._next_buf
            self._queued.append((self._pool.submit(self._fill, ids * self.row_bytes, buf),
                                 buf, ids.size))
            self._next_buf = (self._next_buf + 1) % self._nbuf

    def wait(self) -> tuple:
        """Block until the batch passed to submit() is resident. Returns (view, count)."""
        if not self._queued:
            raise RuntimeError("wait() without a preceding submit(); use fetch() for one-shot use")
        fut, buf, count = self._queued.popleft()   # FIFO: submit(k+1) must not consume k's slot
        dt = fut.result()
        self.stats["batches"] += 1
        self.stats["rows"] += count
        self.stats["fill_s"] += dt
        ru = resource.getrusage(resource.RUSAGE_SELF)
        self.rusage.append((self.stats["batches"], count, ru.ru_minflt, ru.ru_majflt))
        return self._arena[buf][: count * self.row_bytes], count, buf

    # ----------------------------------------------------------------- device side
    def _upload(self, view, count: int, index, buf: int):
        torch = self.torch
        dst = self._dst[: count * self.row_bytes]
        dst.copy_(view, non_blocking=True)
        self._free_ev[buf].record()            # this buffer is free for the CPU once this completes
        self._ev_armed[buf] = True
        rows = dst.view(count, self.row_bytes)
        if index is not None:
            rows = rows[torch.from_numpy(np.ascontiguousarray(index, dtype=np.int64)).to(
                self.device, non_blocking=True)]
        return rows

    def upload_and_gather(self, row_ids: np.ndarray, index: np.ndarray | None = None):
        """wait() -> one H2D -> device gather of the requested rows.

        `index` selects and orders rows within the uploaded batch (defaults to identity). The
        engine's dequant path then consumes [tokens, heads, row_bytes] exactly as it does today.
        """
        view, count, buf = self.wait()
        return self._upload(view, count, index, buf)

    def fetch(self, row_ids: np.ndarray, index: np.ndarray | None = None):
        """Synchronous path (decode: 16 rows, nothing to pipeline)."""
        ids = np.ascontiguousarray(np.asarray(row_ids, dtype=np.int64).reshape(-1), dtype=np.int64)
        if ids.size and (int(ids.min()) < 0 or int(ids.max()) >= self.n_rows):
            raise ValueError("row id out of range in fetch()")
        buf = self._next_buf
        self._next_buf = (self._next_buf + 1) % self._nbuf
        dt = self._fill(ids * self.row_bytes, buf)
        self.stats["batches"] += 1
        self.stats["rows"] += ids.size
        self.stats["fill_s"] += dt
        return self._upload(self._arena[buf][: ids.size * self.row_bytes], ids.size, index, buf)

    # ----------------------------------------------------------------- verification
    def verify(self, sample: int = 4000, seed: int = 3) -> int:
        """Correctness gate: gathered bytes must equal a direct positional read.

        Sampled from random rows, never row 0 -- an earlier gate that checked only row 0 passed
        unconditionally while the copy path returned the wrong bytes. Returns the mismatch count.
        """
        rng = np.random.default_rng(seed)
        ids = rng.choice(self.n_rows, size=min(sample, self.n_rows), replace=False)
        rows = self.fetch(ids)
        bad = 0
        cpu = rows.detach().to("cpu", non_blocking=False).numpy()
        for i, rid in enumerate(ids):
            want = os.pread(self.fd, self.row_bytes, int(rid) * self.row_bytes)
            if len(want) != self.row_bytes:
                raise OSError(f"direct read short at row {rid}")
            if want != cpu[i].tobytes():   # os.pread already returns bytes
                bad += 1
        return bad

    def close(self):
        self._pool.shutdown(wait=True)
        self._fan.shutdown(wait=True)
        _libc.munmap(ctypes.c_void_p(self.base), self.size)
        os.close(self.fd)


class StreamTable:
    """Row gather over a sharded table of base addresses -- the arithmetic, not the mapping.

    Index-shard semantics are copied from `_gather_mapped_rows_kernel` in ple_pageable.py so the two
    backends cannot drift:

        owned = vocab_start <= row < vocab_end
        shard = row // rows_per_shard ; local = row - shard * rows_per_shard
        unowned rows are written as ZEROS, never left as residue

    That last rule is not cosmetic. The kernel's store is deliberately unmasked by ownership because
    with CUDA graphs the destination may still hold a previous replay's bytes; a host-side copy that
    only writes owned rows would reproduce exactly the bug their comment guards against.

    It takes base addresses rather than paths because the engine already mmaps these files:
    `MappedTable` builds per-shard numpy views whose addresses are what its Triton kernel uses. A
    second mmap layer would be a second notion of rows_per_shard to keep in sync, so this class owns
    the arithmetic and `from_paths` exists only for tests.
    """

    def __init__(self, shard_bases, *, rows_per_shard: int, row_bytes: int = 160,
                 threads: int = 24, small_copy_rows: int = 4096, rowcopy_so: str | None = None):
        import numpy as np
        self.np = np
        if not len(shard_bases):
            raise ValueError("no shard bases given")
        self.bases = [int(b) for b in shard_bases]
        self.row_bytes = int(row_bytes)
        self.rows_per_shard = int(rows_per_shard)
        if self.rows_per_shard <= 0 or self.row_bytes <= 0:
            raise ValueError("rows_per_shard and row_bytes must be positive")
        self.num_shards = len(self.bases)
        self.num_rows = self.num_shards * self.rows_per_shard
        self.threads = max(1, int(threads))
        self._small = int(small_copy_rows)
        self._copy = _load_rowcopy(rowcopy_so)
        self._fanpool = None
        self._owns_maps: list = []        # populated by from_paths so it can unmap

    @classmethod
    def from_paths(cls, paths, *, rows_per_shard: int, row_bytes: int = 160, threads: int = 24,
                   small_copy_rows: int = 4096, rowcopy_so: str | None = None):
        """TEST-ONLY: mmap files and build the table over them."""
        paths = list(paths)
        obj = cls([0] * len(paths), rows_per_shard=rows_per_shard, row_bytes=row_bytes,
                  threads=threads, small_copy_rows=small_copy_rows, rowcopy_so=rowcopy_so)
        obj._paths_for_test = paths
        obj._owns_maps = []
        obj.bases = []
        for path in obj._path_list:
            fd = os.open(path, os.O_RDONLY)
            size = os.fstat(fd).st_size
            if size < rows_per_shard * row_bytes:
                os.close(fd)
                raise ValueError(f"{path}: {size} B holds {size // row_bytes} rows, fewer than "
                                 f"rows_per_shard={rows_per_shard} x row_bytes={row_bytes}")
            base = _libc.mmap(None, size, _PROT_READ, _MAP_PRIVATE, fd, 0)
            if not base or base == ctypes.c_void_p(-1).value:
                raise OSError(f"mmap({path}) failed: {os.strerror(ctypes.get_errno())}")
            _libc.madvise(ctypes.c_void_p(base), size, _MADV_RANDOM)
            obj.bases.append(base)
            obj._owns_maps.append((fd, base, size))
        obj.num_shards = len(obj.bases)
        obj.num_rows = obj.num_shards * obj.rows_per_shard
        return obj

    @property
    def _path_list(self):                    # stashed by from_paths before __init__ runs
        return getattr(self, "_paths_for_test", [])

    def split(self, ids):
        """row ids -> (shard index, local row), matching the kernel's integer arithmetic."""
        np = self.np
        ids = np.asarray(ids, dtype=np.int64).reshape(-1)
        if ids.size and (int(ids.min()) < 0 or int(ids.max()) >= self.num_rows):
            raise ValueError(f"row id out of range: [{int(ids.min())}, {int(ids.max())}] "
                             f"vs num_rows={self.num_rows}")
        shard = ids // self.rows_per_shard
        return shard, ids - shard * self.rows_per_shard

    def gather_to(self, ids, dst, *, owned=None) -> int:
        """Gather rows into a host buffer, in id order. Returns rows written.

        `dst` is an int address or a writable ctypes buffer. `owned` (bool array, same length) marks
        rows this rank owns; unowned slots are zeroed rather than skipped.
        """
        np = self.np
        ids = np.asarray(ids, dtype=np.int64).reshape(-1)
        n = ids.size
        if n == 0:
            return 0
        dst_addr = dst if isinstance(dst, int) else ctypes.addressof(dst)
        rb = self.row_bytes
        if owned is None:
            owned = np.ones(n, dtype=bool)
        else:
            owned = np.asarray(owned, dtype=bool).reshape(-1)
            if owned.size != n:
                raise ValueError(f"owned mask {owned.size} != ids {n}")
        if (~owned).any():                       # zero FIRST: a failed copy must not leave residue
            nzero = int((~owned).sum())
            # zeroing scattered slots: build an offset list and memset via one call per run
            zero_off = np.flatnonzero(~owned).astype(np.int64) * rb
            buf = ctypes.create_string_buffer(rb)
            for off in zero_off:
                ctypes.memmove(ctypes.c_void_p(dst_addr + int(off)), buf, rb)
        rows = np.where(owned, ids, 0).astype(np.int64)
        if owned.any():
            sel_all = np.flatnonzero(owned)
            shard, local = self.split(rows[sel_all])
            order = np.argsort(shard, kind="stable")
            sel_all, shard, local = sel_all[order], shard[order], local[order]
            bounds = np.flatnonzero(np.diff(shard)) + 1
            for lo, hi in zip(np.concatenate(([0], bounds)), np.concatenate((bounds, [sel_all.size]))):
                s = int(shard[lo])
                src_off = np.ascontiguousarray(local[lo:hi] * rb, dtype=np.int64)
                dst_off = np.ascontiguousarray(sel_all[lo:hi] * rb, dtype=np.int64)
                self._copy_group(self.bases[s], dst_addr, src_off, dst_off)
        return n

    def _copy_group(self, base, dst_addr, src_off, dst_off):
        np = self.np
        n = src_off.size
        k = 1 if n <= self._small else min(self.threads, max(1, n // 256))
        bounds = [int(x) for x in np.linspace(0, n, k + 1)]

        def work(i):
            lo, hi = bounds[i], bounds[i + 1]
            if hi <= lo:
                return
            fn = self._copy.ple_copy_rows_to
            fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                           np.ctypeslib.ndpointer(dtype=np.int64, ndim=1, flags="C_CONTIGUOUS"),
                           np.ctypeslib.ndpointer(dtype=np.int64, ndim=1, flags="C_CONTIGUOUS"),
                           ctypes.c_long, ctypes.c_long]
            fn.restype = None
            fn(ctypes.c_void_p(base), ctypes.c_void_p(dst_addr),
               np.ascontiguousarray(src_off[lo:hi]), np.ascontiguousarray(dst_off[lo:hi]),
               hi - lo, self.row_bytes)

        if k == 1:
            work(0)
        else:
            if self._fanpool is None:
                self._fanpool = ThreadPoolExecutor(max_workers=self.threads,
                                                   thread_name_prefix="ple-tab")
            list(self._fanpool.map(work, range(k)))

    def close(self):
        if self._fanpool is not None:
            self._fanpool.shutdown(wait=True)
            self._fanpool = None
        for fd, base, size in getattr(self, "_owns_maps", []):
            _libc.munmap(ctypes.c_void_p(base), size)
            os.close(fd)
        self._owns_maps = []


# ---------------------------------------------------------------------------- standalone test
def _selftest(argv):
    import argparse
    import time

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table", required=True)
    ap.add_argument("--trace", default="", help="int32 row-id stream to drive the pattern")
    ap.add_argument("--row-bytes", type=int, default=160)
    ap.add_argument("--threads", type=int, default=24)
    ap.add_argument("--batch-tokens", type=int, default=2048)
    ap.add_argument("--batches", type=int, default=200)
    ap.add_argument("--compute-ms", type=float, default=205.0,
                    help="model compute time the fill hides behind")
    ap.add_argument("--rowcopy", default="")
    ap.add_argument("--buffers", type=int, default=4,
                    help="arena ring depth; 2 reproduces the H2D/CPU overwrite race for comparison")
    a = ap.parse_args(argv)

    import torch
    rows_np = None
    if a.trace:
        raw = np.fromfile(a.trace, dtype=np.int32).astype(np.int64)
        _fd = os.open(a.table, os.O_RDONLY)
        n_rows = os.fstat(_fd).st_size // a.row_bytes
        os.close(_fd)
        rows_np = np.remainder(raw, n_rows)
    else:
        raise SystemExit("--trace is required: uniform-random testing produced the wrong design twice")

    g = StreamGather(a.table, row_bytes=a.row_bytes, threads=a.threads,
                     max_rows_per_batch=a.batch_tokens * 16, rowcopy_so=a.rowcopy or None,
                     buffers=a.buffers)
    bad = g.verify()
    print(f"  gate: {bad} of 4000 randomly sampled rows mismatch a direct read -> "
          f"{'PASS' if bad == 0 else 'FAIL'}")
    if bad:
        return 1

    B = a.batch_tokens * 16
    nb = min(a.batches, len(rows_np) // B)
    fills, exposed = [], []
    # Correct order: submit k, submit k+1, consume k (which waits on the OLDEST queued fill).
    # The previous harness submitted k+1 then immediately waited, which is what made exposed equal
    # fill and looked like a slow design rather than a broken test.
    g.submit(rows_np[:B])
    for k in range(nb):
        t0 = time.perf_counter()
        if k + 1 < nb:
            g.submit(rows_np[(k + 1) * B:(k + 2) * B])
        out = g.upload_and_gather(rows_np[k * B:(k + 1) * B], np.arange(B, dtype=np.int64))
        torch.cuda.synchronize()
        exposed.append(consumed_ := (time.perf_counter() - t0))
        fills.append(g.stats["fill_s"])
    prev = 0.0
    per_fill = []
    for f in fills:
        per_fill.append(f - prev)
        prev = f
    per_fill = np.asarray(per_fill[1:]) * 1e3
    exposed = np.asarray(exposed[1:]) * 1e3
    print(f"  batches={nb} rows/batch={B} threads={a.threads}")
    ru = g.rusage
    dmin = [ru[i][2] - ru[i - 1][2] for i in range(1, len(ru))]
    dmaj = [ru[i][3] - ru[i - 1][3] for i in range(1, len(ru))]
    print(f"  fill:  p50 {np.percentile(per_fill,50):6.2f} ms  p99 {np.percentile(per_fill,99):6.2f} ms")
    q = max(2, len(per_fill) // 4)
    print(f"  fill, first {q} batches (cold):  p50 {np.percentile(per_fill[:q],50):6.2f} ms   "
          f"last {q} (warm): p50 {np.percentile(per_fill[-q:],50):6.2f} ms   "
          f"-> {'residency-bound: cold is much slower' if np.percentile(per_fill[:q],50) > 2 * max(np.percentile(per_fill[-q:],50), 1e-9) else 'no cold/warm split: cost is not page residency'}")
    print(f"  faults per batch: minor p50 {np.percentile(dmin,50):,.0f}  major p50 {np.percentile(dmaj,50):,.0f}"
          f"  (major = pages read from disk during the fill; minor = page-table work on warm pages)")
    mem = {}
    for line in open("/proc/meminfo"):
        f = line.split()
        if f[0] in ("MemAvailable:", "Cached:", "Dirty:", "Shmem:"):
            mem[f[0][:-1]] = int(f[1]) / 1048576
    print("  host at end: " + "  ".join(f"{k} {v:.1f} GiB" for k, v in mem.items()))
    cgroup = ""
    for f in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.high"):
        try:
            cgroup += f"  {f}={open(f).read().strip()}"
        except OSError:
            pass
    print(f"  cgroup:{cgroup or ' unlimited'}")
    print(f"  exposed against a {a.compute_ms:.0f} ms compute window: "
          f"p50 {np.percentile(exposed,50):6.2f} ms  p99 {np.percentile(exposed,99):6.2f} ms")
    print(f"  as a fraction of prefill: p50 {np.percentile(exposed,50)/a.compute_ms*100:.2f}%  "
          f"p99 {np.percentile(exposed,99)/a.compute_ms*100:.2f}%")
    d = np.arange(16, dtype=np.int64)
    dl = []
    for k in range(3000):
        ids = rows_np[k * 16:(k + 1) * 16]
        t0 = time.perf_counter()
        g.fetch(ids)
        torch.cuda.synchronize()
        dl.append((time.perf_counter() - t0) * 1e6)
    print(f"  decode (16 rows): p50 {np.percentile(dl,50):.1f} us  p99 {np.percentile(dl,99):.1f} us  "
          f"p99.9 {np.percentile(dl,99.9):.1f} us")
    g.close()
    return 0


if __name__ == "__main__":
    sys.exit(_selftest(sys.argv[1:]))
#!/usr/bin/env python3
"""Measure a CPU-side PLE gather read path, without touching the engine.

Stage B-i. The question this answers is which access method a streaming PLE table should
use, and it answers it as a property of the read pattern rather than of the inference
stack: given the row ids a real step would ask for, how many filesystem pages does that
cost, how fast can they be fetched, and how much of that traffic does the page cache
actually absorb?

Why it is standalone: the access-method decision does not depend on getting n-gram ids
onto the CPU, which is the part that needs engine work. Everything measured here -- page
deduplication, the amplified 4 KiB read unit, staging copies, hit rate -- is the same
either way.

Two things this deliberately refuses to do:
  * It reports a measured hit rate from /proc/diskstats rather than assuming the page
    cache helped. O_DIRECT on btrfs silently falls back to buffered on compressed
    extents, so a flag is not evidence.
  * It reports achieved-versus-target. A rate-limit miss is a result, not a rounding
    error; the submit path is itself part of what is being measured (a synchronous
    reader at qd1 does ~20k IOPS on gg, so six-plus threads of pure submit is the
    baseline cost of not using io_uring).
"""
import argparse, json, math, mmap, os, random, resource, sys, threading, time

SECTOR = 512


# /proc/diskstats field order after the three identity columns, 0-based on the split line:
#   3 reads completed | 4 reads merged | 5 sectors read | 6 ms reading | 7 writes ...
# The first version of this returned f[5] and f[6] as (reads, sectors), so it divided
# *milliseconds* by 8 to get page counts and reported a 4 GiB cold read as ~3k pages. The
# calibration that caught it: dd of exactly 1 GiB with iflag=direct moved f[3] by 8,192 and
# f[5] by 2,097,152, and f[6] by 2,568. Any counter this team trusts has to be checked
# against a quantity that is known beforehand.
DISK_READS_COMPLETED, DISK_SECTORS_READ = 3, 5


def disk_reads(dev):
    """(reads completed, sectors read) for a device, from /proc/diskstats -- the only
    instrument that works here: page-cache deltas are blind on a host with a 114 GiB
    cache, and /proc/self/io is blind under libaio."""
    for line in open("/proc/diskstats"):
        f = line.split()
        if f[2] == dev:
            return int(f[DISK_READS_COMPLETED]), int(f[DISK_SECTORS_READ])
    raise SystemExit(f"device {dev} not in /proc/diskstats")


def safetensors_tensor(path, needle):
    """Locate a tensor inside a safetensors blob: 8-byte little-endian header length, then
    JSON whose data_offsets are relative to the end of that header.

    Used so the measurement targets the real n-gram table's byte range instead of an
    arbitrary region of the file, which matters because the row stride and the table's
    alignment against 4 KiB pages are properties of that tensor."""
    with open(path, "rb") as f:
        n = int.from_bytes(f.read(8), "little")
        if not 0 < n < 2**26:
            return None, f"implausible safetensors header length {n}"
        hdr = json.loads(f.read(n))
    base = 8 + n
    hits = [(k, v) for k, v in hdr.items() if k != "__metadata__" and needle in k]
    if not hits:
        return None, f"no tensor matching {needle!r}; have {[k for k in hdr if k != '__metadata__'][:6]}"
    k, v = max(hits, key=lambda kv: kv[1]["data_offsets"][1] - kv[1]["data_offsets"][0])
    s, e = v["data_offsets"]
    return {"name": k, "abs_start": base + s, "abs_end": base + e,
            "dtype": v.get("dtype"), "shape": v.get("shape")}, None


def row_bytes_for(dtype, shape, shape_row=1):
    DT = {"F8_E4M3": 1, "F8_E5M2": 1, "BF16": 2, "F16": 2, "F32": 4, "I64": 8, "I32": 4, "U8": 1}
    if dtype not in DT:
        return None
    per_row = 1
    for d in shape[shape_row:]:
        per_row *= d
    return DT[dtype] * per_row


def _libc():
    import ctypes
    lib = ctypes.CDLL("libc.so.6", use_errno=True)
    lib.mmap.restype = ctypes.c_void_p
    lib.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, ctypes.c_longlong]
    lib.mincore.restype = ctypes.c_int
    lib.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p]
    lib.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    return lib


def mincore_resident(fd, nbytes, bs, off=0):
    """Resident page count over a window of the file.

    This exists because /proc/diskstats separates host cache from nothing else: a drive
    with several gigabytes of DRAM cache answers "direct" random reads of a small hot
    region without touching media, and the counters report that as a hit. mincore() gives
    the host contribution directly, so the residual is attributable to the drive. It is
    also the C in the C/N model, measured rather than assumed.

    Written against libc rather than Python's mmap object because ctypes.from_buffer needs
    a writable buffer, so a PROT_READ mapping cannot yield its address that way. MAP_SHARED
    is 1 on Linux; passing 0 fails EINVAL, which is how the first attempt here behaved."""
    import ctypes
    lib = _libc()
    PROT_READ, MAP_SHARED = 0x1, 0x1
    addr = lib.mmap(None, nbytes, PROT_READ, MAP_SHARED, fd, off)
    if addr in (None, ctypes.c_void_p(-1).value):
        return None, f"mmap {nbytes} bytes at {off}: errno {ctypes.get_errno()}"
    try:
        vec = ctypes.create_string_buffer((nbytes + bs - 1) // bs)
        rc = lib.mincore(addr, nbytes, vec)
        if rc != 0:
            return None, f"mincore rc={rc} errno {ctypes.get_errno()}"
        return vec.raw.count(1), None          # C-level count; a Python loop cost minutes
    finally:
        lib.munmap(addr, nbytes)


def resident_pages(fd, start, nbytes, bs, chunk=1 << 30):
    """mincore() over an arbitrary range, in chunks, totalling the resident pages. Probing a
    prefix and extrapolating is what let a 5.48 GiB warm tail pass a cold gate."""
    tot, off, end = 0, start, start + max(0, nbytes)
    while off < end:
        n = min(chunk, end - off)
        res, err = mincore_resident(fd, n, bs, off)
        if res is None:
            return None, err
        tot += res
        off += n
    return tot, None


def page_index(p):
    """Filesystem block size, queried not assumed -- a 160-byte FP8 row is not a valid
    direct-I/O unit and the read unit has to be the real one."""
    return os.stat(p).st_blksize or 4096


class Reader:
    """One access method. Subclasses own their buffers; each thread takes its own."""

    name = "base"

    def __init__(self, path, bs, threads):
        self.path, self.bs = path, bs
        self.fd = os.open(path, os.O_RDONLY)
        self.buffers = [self._alloc(bs) for _ in range(threads)]

    def _alloc(self, bs):
        return bytearray(bs)

    def read(self, tid, off):
        raise NotImplementedError

    def advise(self):
        pass

    def close(self):
        os.close(self.fd)


class PageCacheReader(Reader):
    name = "pcache"

    def read(self, tid, off):
        # os.pread(fd, length, offset) returns bytes and allocates; preadv reads into the
        # caller's buffer, which is the shape the real path needs (no per-read allocation,
        # and the same call works for O_DIRECT). Using pread here raised TypeError inside
        # the worker, which died silently and hung the run.
        b = self.buffers[tid]
        os.preadv(self.fd, [memoryview(b)[:self.bs]], off)
        return b

    def advise(self):
        # This is the line that decides whether "page-cache-backed" means sglang's design
        # or the naive-mmap penalty in a different hat: without it the kernel reads ahead
        # on random 4 KiB requests.
        try:
            os.posix_fadvise(self.fd, 0, 0, os.POSIX_FADV_RANDOM)
        except AttributeError:
            pass


class DirectReader(Reader):
    name = "odirect"

    def __init__(self, path, bs, threads):
        # Anonymous mmap is page-aligned, which is what btrfs demands: alignment against
        # the extent's own granularity, so a 512-byte buffer on an encoded extent silently
        # becomes a buffered read.
        self.fd = os.open(path, os.O_RDONLY | os.O_DIRECT)
        self.bs = bs
        self.buffers = [mmap.mmap(-1, bs) for _ in range(threads)]

    def read(self, tid, off):
        b = self.buffers[tid]
        mv = memoryview(b)[:self.bs]
        n = os.preadv(self.fd, [mv], off)
        if n != self.bs:
            raise SystemExit(f"O_DIRECT short read {n} != {self.bs} at {off}")
        return b


def row_pages(rows, row_bytes, bs, n_rows):
    """Page-deduplicated set for one step's worth of rows. Load-bearing, not an
    optimisation: 160-byte rows against 4 KiB pages means consecutive lookups routinely
    share a page, and reading a page once instead of once per row is most of what keeps
    the volume sane."""
    out = set()
    for r in rows:
        off = r * row_bytes
        p0 = off // bs
        p1 = (off + row_bytes - 1) // bs
        for p in range(p0, p1 + 1):
            if p < n_rows * row_bytes // bs:
                out.add(p)
    return out


def id_stream(rng, count, n_rows, hot_fraction, hot_share, lo=0, span=None):
    """Rows a step would ask for. Uniform is the pessimistic case for the cache; a hot
    set brackets what a Zipf-ish real n-gram distribution would do without needing a
    12.5M-entry CDF table."""
    if hot_share <= 0:
        return [lo + rng.randrange(span) for _ in range(count)]
    hot_n = max(1, int(n_rows * hot_fraction))
    hot = [rng.randrange(hot_n) for _ in range(count)]
    return [h if rng.random() < hot_share else rng.randrange(n_rows) for h in hot]


def main():
    a = sys.argv[1:]
    ap = argparse.ArgumentParser()
    ap.add_argument("--blob", required=True, help="checkpoint blob to read")
    ap.add_argument("--row-bytes", type=int, default=160, help="160 fp8, 320 bf16")
    ap.add_argument("--rows", type=int, default=0, help="rows in blob (default: derive)")
    ap.add_argument("--mode", choices=("pcache", "odirect"), required=True)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--tokens-per-step", type=int, default=2048,
                    help="tokens submitted together: a prefill chunk, not one token")
    ap.add_argument("--rows-per-token", type=int, default=16, help="16 fp8, 17.25-ish bf16")
    ap.add_argument("--steps", type=int, default=2000)
    ap.add_argument("--target-pages-per-s", type=int, default=115700,
                    help="the rate to sustain; misses are reported, not hidden")
    ap.add_argument("--hot-fraction", type=float, default=0.0)
    ap.add_argument("--hot-share", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--dev", default="nvme0n1")
    ap.add_argument("--drop-cache-between", action="store_true",
                    help="fadvise DONTNEED each interval, simulating a host with no spare RAM")
    ap.add_argument("--output", default="")
    ap.add_argument("--residency", action="store_true", help="mincore() before and after")
    ap.add_argument("--tensor", default="",
                    help="safetensors tensor name substring: targets that byte range and row size")
    ap.add_argument("--region-start-bytes", type=int, default=-1,
                    help="exact byte window; preferred over --region-start-gib")
    ap.add_argument("--region-bytes", type=int, default=-1)
    ap.add_argument("--region-start-gib", type=float, default=0.0,
                    help="confine requests to a region so runs cannot warm each other")
    ap.add_argument("--region-gib", type=float, default=0.0, help="0 = to end of file")
    ap.add_argument("--evict-timeout", type=float, default=30.0)
    ap.add_argument("--sync-first", action="store_true",
                    help="sync() before evicting: needed for a just-written file")
    ap.add_argument("--require-cold", action="store_true", help="exit 2 if eviction fails")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--baseline-seconds", type=float, default=1.0,
                    help="sample background device traffic before timing; 0 disables")
    ap.add_argument("--evict-first", action="store_true",
                    help="fadvise DONTNEED the region before timing (whole range: proven effective)")
    ap.add_argument("--evict-lag-pages", type=int, default=0,
                    help="evict a page only after this many newer pages have been read")
    ap.add_argument("--residency-gib", type=int, default=0,
                    help="cap the residency probe window; 0 covers the whole touched region")
    ap.add_argument("--min-working-set", type=int, default=8 * 2**30,
                    help="warn below this many touched bytes (drive DRAM cache)")
    args = ap.parse_args(a)

    bs = page_index(args.blob)
    size = os.stat(args.blob).st_size
    n_rows = args.rows or (size // args.row_bytes)
    n_pages = (n_rows * args.row_bytes) // bs
    # Region confinement: without it every run reads the same offsets and the second run
    # measures the first run's cache, which is how a "cold" number quietly becomes warm.
    if args.tensor:
        info, err = safetensors_tensor(args.blob, args.tensor)
        if err:
            raise SystemExit(err)
        rb = row_bytes_for(info["dtype"], info["shape"] or [0, 0]) or args.row_bytes
        if rb != args.row_bytes and args.row_bytes == 160:      # default: adopt the real one
            args.row_bytes = rb
        page_floor = ((info["abs_start"] + bs - 1) // bs) * bs
        page_ceil = (info["abs_end"] // bs) * bs
        print(f"  tensor {info['name']} dtype={info['dtype']} shape={info['shape']} "
              f"row_bytes={args.row_bytes}", file=sys.stderr)
        print(f"  byte range {info['abs_start']:,}-{info['abs_end']:,}; first usable aligned "
              f"page at {page_floor:,} ({(page_floor - info['abs_start']) % args.row_bytes} bytes "
              "into a row -- the table does not start page-aligned, which the gather must handle)",
              file=sys.stderr)
        args.region_start_bytes = page_floor
        args.region_bytes = max(0, page_ceil - page_floor)
    start_bytes = (args.region_start_bytes if args.region_start_bytes >= 0
                   else int(args.region_start_gib * 2**30))
    span_bytes = (args.region_bytes if args.region_bytes >= 0
                  else (0 if args.region_gib <= 0 else int(args.region_gib * 2**30)))
    lo = start_bytes // args.row_bytes
    hi = n_rows if span_bytes <= 0 else min(n_rows, lo + span_bytes // args.row_bytes)
    span = max(1, hi - lo)
    rng = random.Random(args.seed)

    R = (DirectReader if args.mode == "odirect" else PageCacheReader)(args.blob, bs, args.threads)
    if args.mode == "odirect":
        # Confirm the flag is real before believing any number this prints.
        try:
            import ctypes
            F_GETFL = 3      # not 2 -- 2 is F_SETFD, which mutates instead of querying
            libc.fcntl.restype = ctypes.c_int
            flags = ctypes.CDLL("libc.so.6", use_errno=True).fcntl(R.fd, F_GETFL)
            if not flags & os.O_DIRECT:
                print("  !! O_DIRECT not set on the descriptor -- refusing to label this "
                      "a direct run", file=sys.stderr)
        except Exception as e:
            print(f"  (could not verify O_DIRECT flag: {e})", file=sys.stderr)

    # A persistent pool. The first version spawned one thread per page per step, which
    # measures pthread_create rather than the read path; that version reported numbers for
    # a few seconds before I read it. Threads are created once and handed work in batches,
    # with one batch per model step so the per-step page-deduplication is real.
    lat = []
    errors = []
    cv = threading.Condition()
    job = {"offs": [], "i": 0, "gen": 0, "done": 0, "quit": False}

    def worker(tid):
        try:
            _worker(tid)
        except BaseException as e:                      # a dead thread must be loud
            with cv:
                errors.append(f"thread {tid}: {type(e).__name__}: {e}")
                cv.notify_all()

    def _worker(tid):
        from collections import deque
        local = []
        pending = deque()
        my = 0
        while True:
            with cv:
                while job["gen"] == my and not job["quit"]:
                    cv.wait()
                if job["quit"]:
                    lat.extend(local)
                    return
                my = job["gen"]
                offs = job["offs"]
            # Static partition, not a shared cursor: claiming one page at a time under a
            # single Condition made the lock the bottleneck and throughput fell as threads
            # rose (115k at 2, 23k at 16), which is an instrument artefact, not a result.
            for k in range(tid, len(offs), args.threads):
                off = offs[k]
                t0 = time.perf_counter()
                R.read(tid, off)
                local.append(time.perf_counter() - t0)
                if args.drop_cache_between:
                    pending.append(off)
                    # A page read microseconds ago is usually still in a per-CPU LRU add
                    # batch, whose elevated refcount makes invalidate_mapping_pages skip it.
                    # Evicting a lagged window is what actually bounds the cache.
                    if len(pending) > args.evict_lag_pages:
                        os.posix_fadvise(R.fd, pending.popleft(), bs, os.POSIX_FADV_DONTNEED)
            with cv:
                lat.extend(local)
                local = []
                job["done"] += 1
                cv.notify_all()

    pool = [threading.Thread(target=worker, args=(i,)) for i in range(args.threads)]
    for th in pool:
        th.start()

    # Probe the region actually touched. Measuring residency from offset 0 while reading at
    # 20-40 GiB proves nothing about coldness, which is how an earlier run here reported a
    # "verified cold" start for a region it never looked at.
    roff = lo * args.row_bytes
    rwin = (hi - lo) * args.row_bytes if args.residency_gib <= 0 \
        else min((hi - lo) * args.row_bytes, args.residency_gib * 2**30)
    if args.evict_first:
        # Whole-range DONTNEED is the form that works. Dirty folios are skipped until
        # writeback drains, and folios still in a per-CPU LRU add batch are skipped while
        # refcounts are elevated, so this is a loop with a verdict -- not a call and a hope.
        # The probe covers the ENTIRE region: a prefix probe passed a run whose remaining
        # 5.48 GiB was resident, and that run's "cold" numbers were 36% page cache.
        region_bytes = (hi - lo) * args.row_bytes
        gate_mib, deadline = None, time.perf_counter() + args.evict_timeout
        while time.perf_counter() < deadline:
            os.posix_fadvise(R.fd, lo * args.row_bytes, region_bytes, os.POSIX_FADV_DONTNEED)
            time.sleep(0.25)
            res, _ = resident_pages(R.fd, lo * args.row_bytes, region_bytes, bs)
            gate_mib = None if res is None else res * bs / 2**20
            limit = max(4.0, region_bytes / 2**20 * 0.01)
            if gate_mib is None or gate_mib <= limit:
                break
        limit = max(4.0, region_bytes / 2**20 * 0.01)
        if gate_mib is not None and gate_mib > limit:
            print(f"  !! evict-first left {gate_mib:,.1f} MiB resident of a "
                  f"{region_bytes/2**20:,.0f} MiB region (limit {limit:.1f} MiB). This run is "
                  "WARM: its hit rate describes the cache, not the device.", file=sys.stderr)
            if args.require_cold:
                raise SystemExit(2)
        elif args.verbose:
            print(f"  cold gate: {gate_mib:,.1f} MiB resident of "
                  f"{region_bytes/2**20:,.0f} MiB region", file=sys.stderr)

    res0, res_err = (mincore_resident(R.fd, rwin, bs) if args.residency else (None, None))
    touched_bytes = args.steps * args.tokens_per_step * args.rows_per_token * args.row_bytes * 1.04
    if touched_bytes < args.min_working_set:
        print(f"  !! working set ~{touched_bytes/2**30:.1f} GiB is below "
              f"{args.min_working_set/2**30:.0f} GiB: the drive's own DRAM cache can absorb "
              "this much and device counters will report hits that are not media reads",
              file=sys.stderr)

    # Background traffic baseline. Device counters are host-wide, so a cell can report more
    # device pages than page requests issued -- which happened, and silently. Measuring the
    # idle rate for a moment first makes contamination visible instead of absorbed into the
    # result; a run where the background is more than a few percent of the measured rate is
    # not a measurement of anything.
    bg_pages_s = 0.0
    if args.baseline_seconds > 0:
        br0, bs0 = disk_reads(args.dev)
        tb0 = time.perf_counter()
        time.sleep(args.baseline_seconds)
        br1, bs1 = disk_reads(args.dev)
        el_b = max(1e-6, time.perf_counter() - tb0)
        bg_pages_s = (bs1 - bs0) * SECTOR / bs / el_b

    t_start = time.perf_counter()
    r0, s0 = disk_reads(args.dev)
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    requested = 0
    rows_total = 0
    staging_copy_s = 0.0

    for step in range(args.steps):
        rows = id_stream(rng, args.tokens_per_step * args.rows_per_token, n_rows,
                         args.hot_fraction, args.hot_share, lo, span)
        rows_total += len(rows)
        pages = sorted(row_pages(rows, args.row_bytes, bs, n_rows))
        requested += len(pages)
        with cv:
            job["offs"], job["i"], job["done"] = [q * bs for q in pages], 0, 0
            job["gen"] += 1
            cv.notify_all()
        deadline = time.perf_counter() + 180
        with cv:
            while job["done"] < args.threads:
                if errors:
                    raise SystemExit("worker failed: " + "; ".join(errors[:3]))
                if not any(th.is_alive() for th in pool):
                    raise SystemExit("all workers exited before the step completed")
                if time.perf_counter() > deadline:
                    raise SystemExit(f"step {step} stalled with {job['done']}/{args.threads} "
                                     "workers reporting -- refusing to report a rate")
                cv.wait(1.0)
        # The staging copy the real path pays for: page cache -> user buffer -> pinned
        # staging -> GPU. O_DIRECT removes exactly one of these hops.
        c0 = time.perf_counter()
        _ = bytes(R.buffers[0][: args.row_bytes])
        staging_copy_s += time.perf_counter() - c0
        if args.mode == "pcache" and step == 0:
            R.advise()

    with cv:
        job["quit"] = True
        cv.notify_all()
    for th in pool:
        th.join()

    if errors:
        print("  !! worker errors: " + "; ".join(errors[:5]), file=sys.stderr)
    res1, _ = (resident_pages(R.fd, roff, rwin, bs) if args.residency else (None, None))
    el = time.perf_counter() - t_start
    ru1 = resource.getrusage(resource.RUSAGE_SELF)
    cpu_s = (ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime)
    r1, s1 = disk_reads(args.dev)
    dev_pages = (s1 - s0) * SECTOR / bs
    d = sorted(lat)          # lat holds durations, not (offset, duration)
    pct = lambda q: d[min(len(d) - 1, int(q * len(d)))] * 1e6 if d else 0
    got = requested / el

    out = {
        "mode": args.mode, "threads": args.threads, "fs_block_bytes": bs,
        "tensor": args.tensor or None,
        "row_bytes": args.row_bytes,
        "region_start_bytes": lo * args.row_bytes, "region_bytes": (hi - lo) * args.row_bytes,
        "region_gib": round(span * args.row_bytes / 2**30, 2),
        "region_start_gib": round(args.region_start_gib, 2), "evict_first": args.evict_first,
        "blob": os.path.basename(args.blob), "blob_bytes": size,
        "table_pages": n_pages, "hot_fraction": args.hot_fraction,
        "hot_share": args.hot_share, "seed": args.seed,
        "rows_requested": rows_total, "pages_requested": requested,
        # Measured, not assumed. For random row ids pages/rows comes out at ~1.03-1.05
        # because a 160-byte row straddles a 4 KiB boundary about 160/4096 of the time --
        # which is where the 16.5333 pages/token figure actually comes from. Byte
        # amplification is the separate and much larger number: bytes fetched per byte used.
        "pages_per_row": round(requested / max(1, rows_total), 4),
        "pages_per_token": round(requested / max(1, rows_total / args.rows_per_token), 4),
        "tokens_per_step": args.tokens_per_step,
        "byte_amplification": round(bs / args.row_bytes, 2),
        "elapsed_s": round(el, 2),
        "pages_per_s_achieved": round(got, 1),
        "target_pages_per_s": args.target_pages_per_s,
        "met_target": bool(got >= args.target_pages_per_s),
        "device_pages_read": int(dev_pages),
        "device_reads_completed": r1 - r0,
        "device_mib_read": round((s1 - s0) * SECTOR / 2**20, 1),
        "background_pages_per_s": round(bg_pages_s, 1),
        "contaminated": bool(bg_pages_s > 0.05 * got) if got else False,
        "device_pages_net": int(max(0, dev_pages - bg_pages_s * el)),
        "device_busy_ms": None,   # f[6], kept out of every rate below -- see the field note
        # A negative hit rate is not a probability, it is a signal: the device moved more
        # than was asked for. Surface it as amplification with its request/sector breakdown
        # so the reader can tell "the filesystem read extra clusters or metadata" apart from
        # "the counter is being misread again".
        "measured_hit_rate": round(1 - dev_pages / max(1, requested), 4),
        "fs_device_amplification": round(dev_pages / max(1, requested), 3),
        # 1 - device/requests is NOT the hit rate on a filesystem that amplifies cold
        # reads: device traffic counts ~1.16 pages per distinct page, which buries the
        # hits. The defensible figure subtracts the pages that were actually pulled into
        # the page cache (mincore growth) from the requests issued. Requires a run whose
        # region fits the cache, so nothing was evicted.
        # Residency growth equals distinct pages read ONLY when nothing was evicted. Under a
        # memory cap that fails loudly and silently at once: a capped cell reported 85.6% "hits"
        # while the device served 88% of requests, because pages evicted mid-run are simply
        # absent from the growth figure. Guard it against the birthday expectation.
        "hit_rate_from_residency": (
            round((requested - (res1 - res0)) / requested, 4)
            if (res0 is not None and res1 is not None and dev_pages > 0
                and (res1 - res0) >= 0.9 * min(n_pages, n_pages * (1 - math.exp(-requested / max(1, n_pages)))))
            else None),
        "eviction_expected": (res0 is not None and res1 is not None
                              and (res1 - res0) < 0.9 * min(n_pages, n_pages * (1 - math.exp(-requested / max(1, n_pages))))),
        "distinct_pages_pulled": (res1 - res0) if (res0 is not None and res1 is not None) else None,
        "device_sectors_per_request": round((s1 - s0) / max(1, r1 - r0), 2),
        "page_cache_growth_pages": (res1 - res0) if (res0 is not None and res1 is not None) else None,
        "residency_window_gib": round(rwin / 2**30, 1) if args.residency else None,
        "residency_offset_gib": round(roff / 2**30, 2) if args.residency else None,
        "host_resident_pages_before": res0, "host_resident_pages_after": res1,
        "host_cache_gib_used": (round((res1 - res0) * bs / 2**30, 3)
                                if res0 is not None and res1 is not None else None),
        # requested = host hits + drive hits + media reads. Only the first is mincore's;
        # the residual is what the drive's cache absorbed and the counters cannot see.
        "drive_cache_absorbed_pages": (int(requested - dev_pages - max(0, (res1 or 0) - (res0 or 0)))
                                       if res0 is not None and res1 is not None else None),
        "residency_error": res_err,
        "predicted_hit_rate_c_over_n": None,
        "latency_us": {"p50": round(pct(.50), 1), "p99": round(pct(.99), 1),
                       "p999": round(pct(.999), 1), "max": round(d[-1] * 1e6, 1) if d else 0},
        "staging_copy_s": round(staging_copy_s, 4),
        "cpu_seconds": round(cpu_s, 2),
        "cpu_ms_per_1k_pages": round(cpu_s * 1000 / max(1, requested / 1000), 2),
        "caveats": [
            "a device/page ratio above 1 means the filesystem or the device moved more than "
            "was requested (btrfs metadata walks, cluster reads, or an O_DIRECT fallback)",
            "python threads, not io_uring: the submit path is part of what is measured, so "
            "a short fall is a property of this prototype's rate as much as of the device",
            "device counters are host-wide: nothing else may be reading the disk",
        ],
    }
    print(json.dumps(out, indent=2))
    if args.output:
        json.dump(out, open(args.output, "w"), indent=2)
    R.close()
    if got < args.target_pages_per_s:
        print(f"  note: {got:,.0f} pages/s is below the {args.target_pages_per_s:,} target; "
              "that is a result about this access method at this thread count", file=sys.stderr)


if __name__ == "__main__":
    main()

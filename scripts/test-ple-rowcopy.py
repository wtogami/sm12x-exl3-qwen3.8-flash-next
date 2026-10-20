#!/usr/bin/env python3
"""Byte-exactness test for the row-copy fill path, with no GPU and no engine.

Runs StreamGather._fill directly against a scratch arena so both code paths are exercised: the
inline small-batch path (decode, 16 rows) and the thread fan-out path (prefill batches). Every
copied row is compared against a byte-computed expectation, not a sample.

Why this exists: the fan-out/inline split was introduced with the bounds list rebound AFTER the
inline call, which would have copied 1/nthreads of the batch and left the rest stale -- a bug the
GPU self-test reports as a mysterious mismatch and this file reports as a wrong row count.
"""
import argparse, concurrent.futures as cf, ctypes, os, sys, tempfile

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rowcopy", default=os.environ.get("PLE_ROWCOPY_SO", ""))
    ap.add_argument("--rows", type=int, default=20000)
    a = ap.parse_args(argv)

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "patches"))
    import numpy as np
    import qwen_stream_gather as sg

    src = bytes((i * 37 + (i // 160)) % 251 for i in range(a.rows * 160))
    blob = tempfile.NamedTemporaryFile(delete=False)
    blob.write(src); blob.close()

    g = sg.StreamGather.__new__(sg.StreamGather)          # bypass __init__: no torch, no cuda
    g.row_bytes, g.threads, g._small_copy_rows, g.max_rows = 160, 24, 4096, a.rows
    g.fd = os.open(blob.name, os.O_RDONLY)
    g.size = os.fstat(g.fd).st_size
    g.n_rows = g.size // g.row_bytes
    g.base = sg._libc.mmap(None, g.size, 1, 2, g.fd, 0)
    if not g.base:
        raise OSError("mmap failed: " + os.strerror(ctypes.get_errno()))
    g._copy = sg._load_rowcopy(a.rowcopy or None)
    g._fan = cf.ThreadPoolExecutor(max_workers=g.threads)
    arena = (ctypes.c_ubyte * (a.rows * 160))()
    g._nbuf = 4
    g._arena_base = [ctypes.addressof(arena)] * g._nbuf
    g._ev_armed = [False] * g._nbuf          # no CUDA here: buffers are always "free"

    failures = 0
    for n, label in ((16, "inline (decode-size)"), (4096, "inline boundary"),
                     (5000, "fan-out (batch-size)"), (a.rows, "whole table")):
        n = min(n, a.rows)
        ids = np.arange(n, dtype=np.int64)
        g._fill(ids * g.row_bytes, 0)
        got = bytes(bytearray(arena[: n * g.row_bytes]))
        bad = sum(1 for i in range(n)
                  if src[i * 160:(i + 1) * 160] != got[i * 160:(i + 1) * 160])
        # A count, not a sample: "0 of 4000 sampled" once passed on a build copying 1/24 of rows.
        print(f"  {label:24s} rows={n:6,d}  wrong={bad:6,d}  {'PASS' if bad == 0 else 'FAIL'}")
        failures += bad

    # Non-contiguous ids, descending, because the production indices are neither sorted nor dense.
    rng = np.random.default_rng(7)
    ids = rng.choice(g.n_rows, size=3000, replace=False)
    g._fill(np.ascontiguousarray(ids * g.row_bytes, dtype=np.int64), 0)
    got = bytes(bytearray(arena[: 3000 * 160]))
    bad = sum(1 for j, r in enumerate(ids)
              if src[int(r) * 160:(int(r) + 1) * 160] != got[j * 160:(j + 1) * 160])
    print(f"  {'scattered random ids':24s} rows= 3,000  wrong={bad:6,d}  {'PASS' if bad == 0 else 'FAIL'}")
    failures += bad

    failures += shard_test(a, sg, np)

    g._fan.shutdown(); os.close(g.fd); os.unlink(blob.name)
    print(f"  total wrong rows: {failures}")
    return 1 if failures else 0


def shard_test(args, sg, np):
    """Multi-shard geometry: rows must come from the right FILE, not just the right offset.

    The production table is 128 shards across 22 blobs, so a row id maps to (shard, local row).
    A bug here returns plausible-looking bytes from the neighbouring shard, which is the class of
    error that survives every other check in this file.
    """
    import ctypes, os, tempfile
    nshard, rps, rb = 3, 5000, 160
    paths, blobs = [], []
    try:
        for s in range(nshard):
            # distinct byte patterns per shard, so a wrong-file read can never match
            data = bytes(((s + 1) * 97 + i) % 251 for i in range(rps * rb))
            f = tempfile.NamedTemporaryFile(delete=False)
            f.write(data); f.close()
            paths.append(f.name); blobs.append(data)
        st = sg.StreamTable.from_paths(paths, rows_per_shard=rps, row_bytes=rb, threads=8,
                                       rowcopy_so=args.rowcopy or None)
        assert st.num_rows == nshard * rps, "row count must be shards x rows_per_shard"

        # split(): exact shard/local arithmetic, including boundaries
        probe = np.array([0, 1, rps - 1, rps, rps + 1, 2 * rps - 1, 2 * rps, nshard * rps - 1],
                         dtype=np.int64)
        sh, loc = st.split(probe)
        exp_sh = [0, 0, 0, 1, 1, 1, 2, 2]
        exp_loc = [0, 1, rps - 1, 0, 1, rps - 1, 0, rps - 1]
        assert list(sh) == exp_sh, f"shard split wrong: {list(sh)} != {exp_sh}"
        assert list(loc) == exp_loc, f"local split wrong: {list(loc)} != {exp_loc}"
        print(f"  {'shard/local split':24s} {len(probe)} boundary ids          PASS")

        # gather every shard's first/last rows + scattered ids across all shards
        ids = np.concatenate([np.array([0, rps - 1, rps, 2 * rps - 1, 2 * rps, nshard * rps - 1]),
                              np.sort(np.random.default_rng(5).choice(st.num_rows, 2000, replace=False))])
        buf = (ctypes.c_ubyte * (ids.size * rb))()
        got = st.gather_to(ids, buf)
        assert got == ids.size, f"gathered {got} of {ids.size} rows"
        view = bytes(bytearray(buf))
        bad = 0
        for i, r in enumerate(ids):
            s, local = int(r) // rps, int(r) % rps
            want = blobs[s][local * rb:(local + 1) * rb]
            if want != view[i * rb:(i + 1) * rb]:
                bad += 1
        print(f"  {'sharded gather':24s} rows={ids.size:6,d}  wrong={bad:6,d}  "
              f"{'PASS' if bad == 0 else 'FAIL'}")

        # ownership rule, mirroring _gather_mapped_rows_kernel: unowned rows are ZERO, and the
        # destination starts dirty so "skipped" would be visible as residue.
        dirty = (ctypes.c_ubyte * (ids.size * rb))()
        ctypes.memset(ctypes.addressof(dirty), 0xAB, ctypes.sizeof(dirty))
        owned = np.ones(ids.size, dtype=bool)
        owned[::3] = False                       # every third row belongs to another rank
        st.gather_to(ids, dirty, owned=owned)
        view = bytes(bytearray(dirty))
        bad_o = 0
        for i, r in enumerate(ids):
            slot = view[i * rb:(i + 1) * rb]
            if not owned[i]:
                if slot != bytes(rb):
                    bad_o += 1
            else:
                s, local = int(r) // rps, int(r) % rps
                if blobs[s][local * rb:(local + 1) * rb] != slot:
                    bad_o += 1
        print(f"  {'ownership zeroing':24s} rows={ids.size:6,d}  wrong={bad_o:6,d}  "
              f"{'PASS' if bad_o == 0 else 'FAIL (residue or wrong shard)'}")
        bad += bad_o

        # out-of-range must raise, not clamp
        try:
            st.split(np.array([st.num_rows], dtype=np.int64))
            print("  range guard                    FAIL (no exception)")
            bad += 1
        except ValueError:
            print("  range guard                    raises on out-of-range id   PASS")
        st.close()
        return bad
    finally:
        for f in paths:
            try: os.unlink(f)
            except OSError: pass

if __name__ == "__main__":
    sys.exit(main())

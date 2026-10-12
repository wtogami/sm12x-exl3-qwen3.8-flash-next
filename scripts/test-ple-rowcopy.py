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
    g._arena_base = [ctypes.addressof(arena), ctypes.addressof(arena)]

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

    g._fan.shutdown(); os.close(g.fd); os.unlink(blob.name)
    print(f"  total wrong rows: {failures}")
    return 1 if failures else 0

if __name__ == "__main__":
    sys.exit(main())

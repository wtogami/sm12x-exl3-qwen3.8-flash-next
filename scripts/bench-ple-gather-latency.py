#!/usr/bin/env python3
"""CPU-only: how does StreamTable.gather_to latency scale with rows per step?

The in-engine phase split said gather ~= 1.79 ms/step for ~864 rows, while the standalone prototype measured
243 us for 32,768 rows. Those differ by ~250x per row, which is a fixed-per-call signature rather than a
throughput one. This measures the curve directly, counts the shard groups each batch forms, and times the
numpy/ctypes bookkeeping separately from the memcpy, so "fixed overhead" becomes a number rather than a guess.
No CUDA: gather_to is host work, and the point is to measure it without a 4-minute engine boot in the way.
"""
import ctypes, glob, json, os, struct, sys, time
import numpy as np

libc = ctypes.CDLL("libc.so.6", use_errno=True)
libc.mmap.restype = ctypes.c_void_p                    # truncating this to c_int segfaulted an earlier attempt
libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]

snap = sorted(glob.glob(os.path.expanduser("~/.cache/huggingface/hub/models--*PLE-FP8*/snapshots/*")))[0]
wm = json.load(open(os.path.join(snap, "model.safetensors.index.json")))["weight_map"]
files = sorted({os.path.realpath(os.path.join(snap, v)) for k, v in wm.items() if "ngram_embedding.shard_" in k})
hdr = {}
for path in files:
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        meta = json.loads(f.read(n))
    base = 8 + n
    for name, ent in meta.items():
        if "ngram_embedding.shard_" in name:
            s = int(name.split("shard_")[1].split(".")[0])
            b, e = ent["data_offsets"]
            hdr[s] = (path, base + b, ent["shape"][0], ent["shape"][1])
rows_per_shard = hdr[0][2]
row_bytes = hdr[0][3]
fds, maps = {}, {}
bases = []
for s in sorted(hdr):
    path, off, _, _ = hdr[s]
    if path not in fds:
        fd = os.open(path, os.O_RDONLY); fds[path] = fd
        m = libc.mmap(None, os.fstat(fd).st_size, 1, 2, fd, 0)
        if not m:
            raise OSError("mmap failed")
        maps[path] = m
    bases.append(maps[path] + off)

sys.path.insert(0, "/usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers")
from qwen_stream_gather import StreamTable
st = StreamTable(bases, rows_per_shard=rows_per_shard, row_bytes=row_bytes, threads=24,
                 rowcopy_so="/opt/qwen38/ple_rowcopy.so")
print(f"  table {st.num_rows:,} rows x {row_bytes} B over {st.num_shards} shards; rows/shard {rows_per_shard:,}")

rng = np.random.default_rng(7)
print(f"  {'rows':>7} {'ms/call':>9} {'us/row':>8} {'shard groups':>13} {'groups>1':>9}")
for n in (16, 64, 256, 864, 2048, 8192, 32768):
    ids = rng.choice(st.num_rows, size=n, replace=False).astype(np.int64)
    dst = (ctypes.c_ubyte * (n * row_bytes))()
    shard = ids // rows_per_shard
    groups = len(np.unique(shard))
    st.gather_to(ids, ctypes.addressof(dst))          # warm the mapping and the pool
    t0 = time.perf_counter()
    reps = max(3, min(200, 2_000_000 // max(n, 1)))
    for _ in range(reps):
        st.gather_to(ids, ctypes.addressof(dst))
    dt = (time.perf_counter() - t0) / reps
    print(f"  {n:7d} {dt*1e3:9.3f} {dt*1e6/n:8.2f} {groups:13d} {1 if groups > 1 else 0:9d}")
st.close()

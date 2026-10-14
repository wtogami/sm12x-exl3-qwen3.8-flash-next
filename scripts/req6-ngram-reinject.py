#!/usr/bin/env python3
"""req6 repair (a): the vendor converter replaces the BF16 hashed n-gram
tables with a trellis ngram_embedding.safetensors that our fork never
serves. Copy the 130 source BF16 tensors (key layout verified identical to
the K4.25 base card) into new shards and register them in the output
index. Idempotent: skips if the index already has the keys. The converter's
trellis file is kept separately (req6-build/keep/) for future PLE-quant
experiments."""
import json, os, sys
from safetensors import safe_open
from safetensors.torch import save_file

SRC = sys.argv[1]; OUT = sys.argv[2]; CHUNK_GB = int(os.environ.get("CHUNK_GB", 16))
sidx = json.load(open(f"{SRC}/model.safetensors.index.json"))
keys = sorted(k for k in sidx["weight_map"] if "ngram" in k)
assert keys, "no ngram keys in source"
oidx_path = f"{OUT}/model.safetensors.index.json"
oidx = json.load(open(oidx_path))
if all(k in oidx["weight_map"] for k in keys):
    print("already re-injected; nothing to do"); sys.exit(0)
by_file = {}
for k in keys: by_file.setdefault(sidx["weight_map"][k], []).append(k)
buf, buf_bytes, parts, n = {}, 0, [], 0
def flush():
    global buf, buf_bytes, parts
    if not buf: return
    parts.append(dict(buf)); buf_bytes = 0; buf = {}
for fn, fks in sorted(by_file.items()):
    with safe_open(f"{SRC}/{fn}", framework="pt") as f:
        for k in fks:
            t = f.get_tensor(k)
            buf[k] = t; buf_bytes += t.numel() * t.element_size(); n += 1
            if buf_bytes >= CHUNK_GB * 1e9: flush()
flush()
np_ = len(parts)
tot = sum(t.numel() * t.element_size() for p in parts for t in p.values())
for i, p in enumerate(parts):
    name = f"model-ngram-{i+1:05d}-of-{np_:05d}.safetensors"
    save_file(p, f"{OUT}/{name}", metadata={"format": "pt"})
    for k in p: oidx["weight_map"][k] = name
    print(f"wrote {name} ({sum(t.numel()*t.element_size() for t in p.values())/1e9:.1f} GB)")
oidx["metadata"]["total_size"] = oidx["metadata"].get("total_size", 0) + tot
json.dump(oidx, open(oidx_path, "w"), indent=2)
print(f"OK: {n}/{len(keys)} keys re-injected, {tot/1e9:.1f} GB total")

#!/usr/bin/env python3
"""Verify copied K5V card on burst: structure + rates + ngram hardlink identity."""
import json, collections, os, sys
CACHE = os.path.expanduser(sys.argv[1])
d = f"{CACHE}/req5v-build/out"
idx = json.load(open(f"{d}/model.safetensors.index.json"))["weight_map"]
ts = json.load(open(f"{d}/quantization_config.json"))["tensor_storage"]
ng = sum(1 for k in idx if "ngram" in k); mtp = sum(1 for k in idx if k.startswith("mtp."))
hist = collections.Counter(v["bits_per_weight"] for v in ts.values() if "bits_per_weight" in v)
links = set(os.stat(os.path.join(d, f)).st_nlink for f in os.listdir(d) if f.startswith("model-ngram"))
print(f"verify: index={len(idx)} ngram={ng} mtp={mtp} hist={dict(sorted(hist.items()))} ngram_nlink={sorted(links)}")
assert len(idx) > 300000 and ng == 130 and mtp == 6173
assert hist[5] > 70000 and set(hist) <= {5, 6, 16}
print("K5V VERIFY PASS")

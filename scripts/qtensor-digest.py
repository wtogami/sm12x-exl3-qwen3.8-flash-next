#!/usr/bin/env python3
"""Per-file semantic digest of safetensors: sha256 over sorted(tensor-name,
dtype, shape, payload-hash). Layout/order-insensitive at the file level but
byte-strict per tensor payload. Pure stdlib; runs on any host python3."""
import hashlib, json, os, struct, sys
d = sys.argv[1]
for fn in sorted(os.listdir(d)):
    if not fn.endswith(".safetensors"): continue
    p = os.path.join(d, fn)
    with open(p, "rb") as f:
        n, = struct.unpack("<Q", f.read(8))
        hdr = json.loads(f.read(n))
        items = []
        for k in sorted(hdr):
            if k == "__metadata__": continue
            e = hdr[k]
            o0, o1 = e["data_offsets"]
            f.seek(8 + n + o0); payload = f.read(o1 - o0)
            items.append(k + "|" + e["dtype"] + "|" + str(e["shape"]) + "|" + hashlib.sha256(payload).hexdigest())
        agg = hashlib.sha256("\n".join(items).encode()).hexdigest()
    print(fn, len(items), agg)

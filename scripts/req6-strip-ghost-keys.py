#!/usr/bin/env python3
"""req6 repair (e): req6-mtp-repair.py replaced the MTP-dense trellis
payloads in the INDEX but left them physically inside the original shards.
vLLM's default loader walks safetensors files, not the index, so as soon
as the MTP draft is instantiated (num_speculative_tokens>0) it hits ghost
keys like fc_embedding.mcg with no target module and dies. Strip every
ledger-unknown legacy payload from the shard files in place. Idempotent."""
import json, os, struct, sys, glob, tempfile
import torch
from safetensors.torch import save_file
OUT = sys.argv[1]
idx = set(json.load(open(f"{OUT}/model.safetensors.index.json"))["weight_map"])
GHOSTS = ("trellis", "suh", "svh", "mcg", "mul1", "dum")
def header(fn):
    with open(fn, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        h = json.loads(f.read(n))
    return {k: v for k, v in h.items() if k != "__metadata__"}
for fn in sorted(glob.glob(f"{OUT}/model-0*.safetensors")):
    h = header(fn)
    ghosts = [k for k in h if k not in idx]
    if not ghosts:
        continue
    bad = [k for k in ghosts if k.rsplit(".", 1)[-1] not in GHOSTS]
    assert not bad, f"unexpected non-ghost missing keys in {fn}: {bad[:5]}"
    from safetensors import safe_open
    with safe_open(fn, framework="pt") as f:
        keep = {k: f.get_tensor(k) for k in h if k in idx}
    tmp = tempfile.NamedTemporaryFile(dir=OUT, delete=False, suffix=".safetensors").name
    save_file(keep, tmp, metadata={"format": "pt"})
    os.replace(tmp, fn)
    print(f"{os.path.basename(fn)}: stripped {len(ghosts)} ghost keys -> {len(keep)} kept")
print("done")

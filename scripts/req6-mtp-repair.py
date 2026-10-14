#!/usr/bin/env python3
"""req6 repairs (b)+(c). The vendor converter with -mb quantizes the WHOLE
MTP block and, in 1.6.1, omits every mtp.* module from the ledger.
(b) dequant the non-expert mtp tensors to BF16 (base-card topology: draft
    dense in BF16, never touches b12x's ext-dependent dense-exl3 path), and
(c) synthesize ledger entries for the 1,536 quantized mtp experts from the
    real shard headers. Idempotent."""
import json, os, sys, struct, importlib.util, glob
import torch
from safetensors.torch import save_file

OUT = sys.argv[1]; DEV = "cuda"
spec = importlib.util.spec_from_file_location("exllamav3_ext", os.environ.get("EXT", "/hf/exv3-runtime/exllamav3_ext.so"))
ext = importlib.util.module_from_spec(spec); spec.loader.exec_module(ext)
MCG = 3417055213  # 0xCBAC1FED

files = sorted(glob.glob(f"{OUT}/model-*.safetensors"))
hdr, fmap = {}, {}
for fn in files:
    with open(fn, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        for k, v in json.loads(f.read(n)).items():
            if k == "__metadata__": continue
            hdr[k] = {"shape": v["shape"], "dtype": v["dtype"], "n_bytes": v["data_offsets"][1] - v["data_offsets"][0]}
    fmap[fn] = fn
idx_path = f"{OUT}/model.safetensors.index.json"
idx = json.load(open(idx_path))["weight_map"]

trellised = sorted({k[:-8] for k in idx if k.startswith("mtp") and k.endswith(".trellis")})
dense = [p for p in trellised if ".mlp.experts." not in p]
experts = [p for p in trellised if ".mlp.experts." in p]
print(f"mtp trellised: {len(trellised)} (experts {len(experts)}, dense {len(dense)})")

def load(pfx, suf):
    from safetensors import safe_open
    with safe_open(os.path.join(OUT, idx[f"{pfx}.{suf}"]), framework="pt") as fo:
        return fo.get_tensor(f"{pfx}.{suf}")

new_w = {}
if dense and not all(f"{p}.weight" in idx for p in dense):
    for p in dense:
        tr, su, sv = (load(p, s).to(DEV) for s in ("trellis", "suh", "svh"))
        K = tr.shape[2] // 16
        n_in, n_out = tr.shape[0] * 16, tr.shape[1] * 16
        buf = torch.empty((n_in, n_out), dtype=torch.half, device=DEV)
        ext.reconstruct_had_slice(buf, tr, su, sv, float(K), True, False, 0)
        new_w[f"{p}.weight"] = buf.T.to(torch.bfloat16).cpu().contiguous()
    out_file = f"{OUT}/model-mtp-bf16.safetensors"
    save_file(new_w, out_file, metadata={"format": "pt"})
    for p in dense:
        for s in ("trellis", "suh", "svh", "mcg"):
            idx.pop(f"{p}.{s}", None)
        idx[f"{p}.weight"] = "model-mtp-bf16.safetensors"
    json.dump({"metadata": {"total_size": sum(hdr.get(k, {}).get("n_bytes", 0) for k in idx)}, "weight_map": idx},
              open(idx_path, "w"), indent=2)
    print(f"(b) dequantized {len(dense)} dense mtp tensors -> {out_file}")

cfg_path = f"{OUT}/quantization_config.json"
q = json.load(open(cfg_path)); ts = q["tensor_storage"]
if len(experts) == sum(1 for k in ts if k.startswith("mtp")):
    print("(c) ledger already complete"); sys.exit(0)
for p in experts:
    st = {f"{p}.{s}": hdr[f"{p}.{s}"] for s in ("trellis", "suh", "svh", "mcg") if f"{p}.{s}" in hdr}
    K = st[f"{p}.trellis"]["shape"][2] // 16
    ts[p] = {"quant_format": "exl3", "bits_per_weight": K, "mcg_multiplier": MCG, "stored_tensors": st}
json.dump(q, open(cfg_path, "w"))
print(f"(c) ledger now {len(ts)} entries; mtp entries {sum(1 for k in ts if k.startswith('mtp'))}")

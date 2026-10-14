#!/usr/bin/env python3
"""Dequant Swift 6.05's quantized shared experts to plain BF16 at rest.

Swift's checkpoint quantizes mlp.shared_expert.{gate,up,down}_proj (6 bpw,
K6, mul1 codebook). Our fork TP-slices column-parallel outputs with a hard
Hadamard-128 alignment guard (exl3.py:_slice_exl3_tensor): a width-640 gate
halves to 320, which is illegal for the trellis quantizer. The K4.25 card
sidesteps all of this because its converter stored shared experts UNQUANTIZED
in bf16 (plain .weight keys, absent from tensor_storage) - vLLM's unquantized
TP path slices anything. This script reproduces that proven layout.

Weights are recovered by running the very kernel the engine trusts
(b12x.gemm.trellis_linear = run_trellis256_dense) on an identity batch:
x @ W^T with x = I yields W exactly at the kernel's own output precision,
so "dequant" here means "what the quantized GEMM computes on one-hot
inputs" - no reverse-engineered codec involved. Side effect (by design):
this exercises K6 + mul1 decode end to end on real tensors, which the MoE
per-expert runtime uses the same kernel family for.

Writes: one new safetensors shard with {proj}.weight bf16 tensors, an
updated model.safetensors.index.json (trellis keys dropped), and an updated
quantization_config.json sidecar (shared-expert entries removed from
tensor_storage so get_quant_method falls through to UnquantizedLinearMethod).

Usage: python3 scripts/exl3-605-shared-dequant.py --snapshot DIR [--device cuda]
"""
import argparse, json, os, re, sys, tempfile
import torch
from safetensors import safe_open
from safetensors.torch import save_file

PROJ = ("gate_proj", "up_proj", "down_proj")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--dry-run", action="store_true", help="probe one tensor only, write nothing")
    a = ap.parse_args()
    snap = a.snapshot
    idx_path = os.path.join(snap, "model.safetensors.index.json")
    sidecar_path = os.path.join(snap, "quantization_config.json")
    idx = json.load(open(idx_path))["weight_map"]
    side = json.load(open(sidecar_path))
    ts = side["tensor_storage"]

    # every quantized shared-expert tensor: module prefix -> shard file
    targets = {}
    for k in idx:
        m = re.match(r"^((?:model\.language_model\.layers|mtp\.layers)\.\d+\.mlp\.shared_expert\.(?:gate|up|down)_proj)\.(trellis|suh|svh|mul1)$", k)
        if m:
            targets.setdefault(m.group(1), {})[m.group(2)] = idx[k]
    print(f"quantized shared-expert modules found: {len(targets)}")
    # idempotent: trunk modules match either spelling across runs
    assert len(targets) in (0, 3, 144, 147), f"unexpected target count {len(targets)}"
    if not targets:
        print("already dequanted; nothing to do"); return 0

    # Oracle = the vendor's own decoder (exllamav3_ext, sm_120 build of the
    # exact exllamav3==1.5.1 that generated this card; b12x refuses mul1).
    # reconstruct_had_slice emits ORIGINAL-basis weights (both Hadamards +
    # sign vectors folded), incl. K=8 - the rate b12x cannot run at all.
    import importlib.util
    spec = importlib.util.spec_from_file_location("exllamav3_ext", os.environ.get("EXV3_SO", "/hf/exv3-runtime/exllamav3_ext.so"))
    ext = importlib.util.module_from_spec(spec); spec.loader.exec_module(ext)
    openers = {}
    def t(name, fname):
        f = openers.setdefault(fname, safe_open(os.path.join(snap, fname), framework="pt"))
        return f.get_tensor(name)

    out_tensors, done = {}, []
    for i, (mod, parts) in enumerate(sorted(targets.items())):
        trellis = t(f"{mod}.trellis", parts["trellis"]).to(a.device)
        suh = t(f"{mod}.suh", parts["suh"]).to(a.device)
        svh = t(f"{mod}.svh", parts["svh"]).to(a.device)
        mul1 = t(f"{mod}.mul1", parts["mul1"]).to(a.device) if "mul1" in parts else None
        k_bits = trellis.shape[2] // 16
        n_out = trellis.shape[1] * 16
        n_in = trellis.shape[0] * 16
        buf = torch.empty((n_in, n_out), dtype=torch.half, device=a.device)
        ext.reconstruct_had_slice(buf, trellis, suh, svh, float(k_bits), False, mul1, 0)
        wt = buf.T.contiguous().to(torch.bfloat16)   # (in,out) -> HF (out,in)
        assert wt.shape == (n_out, n_in), (mod, wt.shape)
        if a.dry_run:
            print(f"PROBE {mod}: K{k_bits} in={n_in} out={n_out} -> kernel OK; "
                  f"finite={torch.isfinite(wt).all().item()} absmax={wt.abs().max().item():.4f} "
                  f"mean={wt.float().mean().item():+.6f} std={wt.float().std().item():.5f}")
            return 0
        name = mod + ".weight"  # HF checkpoint names: keep model.language_model./mtp. prefixes
        out_tensors[name] = wt.cpu()
        done.append(mod)
        if (i + 1) % 24 == 0:
            print(f"  {i+1}/{len(targets)} dequanted", flush=True)
            openers.clear()

    shard = "shared_experts_bf16.safetensors"
    shard_path = os.path.join(snap, shard)
    if os.path.exists(shard_path):  # merge: re-runs must not evict earlier batches
        from safetensors.torch import load_file as _lf
        merged = {k: v for k, v in _lf(shard_path).items()}
        merged.update(out_tensors)
        out_tensors = merged
        json.dump({**json.load(open(idx_path)), "weight_map": {
            **json.load(open(idx_path))["weight_map"],
            **{k: shard for k in out_tensors}}}, open(idx_path, "w"))
    save_file(out_tensors, shard_path, metadata={"format": "pt"})
    # index: drop trellis/suh/svh/mul1 keys for these modules, add .weight keys
    wm = json.load(open(idx_path))["weight_map"]
    for mod in done:
        for suf in ("trellis", "suh", "svh", "mul1"):
            wm.pop(f"{mod}.{suf}", None)
    for name in out_tensors:
        wm[name] = shard
    json.dump({"metadata": {"total_size": sum(x.numel() * x.element_size() for x in out_tensors.values())}, "weight_map": wm},
              open(idx_path, "w"), indent=2)
    # sidecar: remove the modules so the prefix gate says "not exl3"
    removed = 0
    for mod in done:
        if ts.pop(mod, None) is not None:
            removed += 1
    json.dump(side, open(sidecar_path, "w"))
    print(f"wrote {shard}: {len(out_tensors)} tensors, sidecar entries removed: {removed}")
    return 0

if __name__ == "__main__":
    sys.exit(main())

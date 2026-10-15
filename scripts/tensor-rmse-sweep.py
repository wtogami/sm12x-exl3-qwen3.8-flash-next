#!/usr/bin/env python3
"""Per-tensor dequant fidelity sweep over raw converter qtensors (pre-repair work dir).
For expert tensors: dequantize via trellis_linear identity-run, compare vs BF16 source.
Deterministic, offline (no engine), GPU-assisted. Emits jsonl: per-module aggregates + summary.
Usage: python3 tensor-rmse-sweep.py --qt <qtensors dir> --src <bf16 source dir> --out <jsonl> [--stride-experts N]"""
import argparse, glob, importlib.util, json, os, re
import torch
from safetensors import safe_open
spec = importlib.util.spec_from_file_location("exllamav3_ext", "/hf/exv3-runtime/exllamav3_ext.so")
ext = importlib.util.module_from_spec(spec); spec.loader.exec_module(ext)
from b12x.gemm import trellis_linear as tl
ap = argparse.ArgumentParser()
ap.add_argument("--qt", required=True); ap.add_argument("--src", required=True)
ap.add_argument("--out", required=True); ap.add_argument("--stride", type=int, default=32)
a = ap.parse_args()
dev = "cuda" if torch.cuda.is_available() else "cpu"
sidx = json.load(open(f"{a.src}/model.safetensors.index.json"))["weight_map"]
def src_get(key):
    fn = sidx.get(key)
    if fn is None: return None
    with safe_open(f"{a.src}/{fn}", framework="pt") as f: return f.get_tensor(key)
expert_re = re.compile(r"^(model\.language_model\.layers\.\d+|mtp\.layers\.\d+)\.mlp\.experts\.(\d+)\.(gate_proj|up_proj|down_proj)\.trellis$")
gu_cache = {}
def expert_src(lb, e, which):
    if which == "down":
        t = src_get(f"{lb}.mlp.experts.down_proj")
        if t is None:
            return src_get(f"{lb}.mlp.experts.{e}.down_proj.weight")
        return t[e].to(dev).float()
    if lb not in gu_cache: gu_cache[lb] = src_get(f"{lb}.mlp.experts.gate_up_proj")
    gu = gu_cache[lb]
    if gu is None: return src_get(f"{lb}.mlp.experts.{e}.{which}_proj.weight").float()
    half = gu.shape[1]//2
    return (gu[e,:half,:] if which=="gate" else gu[e,half:,:]).to(dev).float()
files = sorted(glob.glob(f"{a.qt}/*.safetensors"))
tot_t = 0; tot_sq = 0.0; tot_ssnr = []
per_mod = {}
with open(a.out, "w") as fo:
    for fp in files:
        with safe_open(fp, framework="pt") as h: keys = set(h.keys())
        mods = {}
        for k in keys:
            m = expert_re.match(k)
            if not m: continue
            lb, e, which = m.group(1), int(m.group(2)), m.group(3)
            if e % a.stride: continue
            mods.setdefault(lb, []).append((e, which, k[:-len(".trellis")]))
        for lb, lst in sorted(mods.items()):
            n=rmse=ssum=0.0; vals=[]
            for e, which, pfx in lst:
                tr = None
                with safe_open(fp, framework="pt") as h:
                    tr = h.get_tensor(pfx+".trellis").to(dev)
                    su = h.get_tensor(pfx+".suh").to(dev); sv = h.get_tensor(pfx+".svh").to(dev)
                    mk = h.get_tensor(pfx+".mcg").to(dev) if pfx+".mcg" in keys else None
                K = tr.shape[2]//16; n_in, n_out = tr.shape[0]*16, tr.shape[1]*16
                w = tl.prepare_weight(tr, su, sv, mcg=mk, codebook="mcg")
                w_b = tl.run(torch.eye(n_in, device=dev, dtype=torch.bfloat16), w).reshape(n_in, n_out).to(torch.bfloat16).T.float()
                w_src = expert_src(lb, e, which)
                if w_src is None: continue
                if tuple(w_src.shape[-2:]) != tuple(w_b.shape[-2:]) and w_src.shape[-2:] != w_b.shape[:2]:
                    continue  # layout guard
                ws = w_src.reshape(w_b.shape)
                d = (w_b - ws)
                rm = float(d.pow(2).mean().sqrt())
                cos = float(torch.nn.functional.cosine_similarity(d.reshape(1,-1), ws.reshape(1,-1)).item())
                sq = float(ws.pow(2).sum()); ssq = sq / (float(d.pow(2).sum())+1e-12)
                vals.append({"tensor": pfx, "rmse": rm, "rel_rmse": rm/(float(ws.pow(2).mean().sqrt())+1e-12),
                             "cos": cos, "sqnr_db": 10*__import__("math").log10(max(ssq,1e-12))})
                n += 1; rmse += rm; ssum += 10*__import__("math").log10(max(ssq,1e-12))
                tot_t += 1; tot_sq += float(d.pow(2).sum()); 
                tot_ssnr.append(10*__import__("math").log10(max(ssq,1e-12)))
                del tr, su, sv, mk, w, w_b, w_src, d, ws
            if n:
                row = {"record":"module","module":lb,"tensors":n,"mean_rmse":rmse/n,"mean_sqnr_db":ssum/n}
                per_mod[lb] = row; fo.write(json.dumps(row)+"\n"); fo.flush()
    fo.write(json.dumps({"record":"summary","tensors":tot_t,"gmean_sqnr_db": sum(tot_ssnr)/len(tot_ssnr) if tot_ssnr else 0,
          "modules": len(per_mod), "stride": a.stride})+"\n")
print("SWEEP DONE", a.qt, "tensors:", tot_t, "mean sqnr:", round(sum(tot_ssnr)/max(len(tot_ssnr),1),2))

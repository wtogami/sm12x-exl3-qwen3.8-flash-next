import json, importlib.util, torch
from safetensors import safe_open
spec = importlib.util.spec_from_file_location("exllamav3_ext", "/hf/exv3-runtime/exllamav3_ext.so")
ext = importlib.util.module_from_spec(spec); spec.loader.exec_module(ext)
from b12x.gemm import trellis_linear as tl
OUT = "/hf/req6-build/out"; S = "/hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540"
idx = json.load(open(f"{OUT}/model.safetensors.index.json"))["weight_map"]
sidx = json.load(open(f"{S}/model.safetensors.index.json"))["weight_map"]
def get(base, i, key):
    with safe_open(f"{base}/{i[key]}", framework="pt") as f: return f.get_tensor(key)
dev = "cuda"
def decoded(p):
    tr, su, sv, mk = (get(OUT, idx, f"{p}.{s}").to(dev) for s in ("trellis","suh","svh","mcg"))
    K = tr.shape[2]//16; n_in, n_out = tr.shape[0]*16, tr.shape[1]*16
    buf = torch.empty((n_in, n_out), dtype=torch.half, device=dev)
    ext.reconstruct_had_slice(buf, tr, su, sv, float(K), True, False, 0)
    w_v = buf.T.to(torch.bfloat16).float()
    w = tl.prepare_weight(tr, su, sv, mcg=mk, codebook="mcg")
    w_b = tl.run(torch.eye(n_in, device=dev, dtype=torch.bfloat16), w).reshape(n_in, n_out).to(torch.bfloat16).T.float()
    return K, w_v, w_b
def src_slice(layerbase, e, which):
    if which == "down":
        return get(S, sidx, f"{layerbase}.mlp.experts.down_proj")[e].to(dev).float()
    gu = get(S, sidx, f"{layerbase}.mlp.experts.gate_up_proj").to(dev).float()
    half = gu.shape[1] // 2
    a, b = gu[e, :half, :], gu[e, half:, :]
    return (a, b)
cases = [("model.language_model.layers.0", 7, "gate"), ("model.language_model.layers.0", 7, "up"),
         ("model.language_model.layers.24", 100, "down"), ("model.language_model.layers.47", 511, "up"),
         ("mtp.layers.0", 3, "gate")]
for lb, e, which in cases:
    proj = {"gate":"gate_proj","up":"up_proj","down":"down_proj"}[which]
    p = f"{lb}.mlp.experts.{e}.{proj}"
    K, w_v, w_b = decoded(p)
    d = (w_v - w_b).abs()
    s = src_slice(lb, e, which)
    if isinstance(s, tuple):  # gate/up: report cos against both halves, order-agnostic
        rel = min(((s[i] - w_b).pow(2).mean().sqrt() / s[i].std()).item() for i in (0,1))
    else:
        rel = ((s - w_b).pow(2).mean().sqrt() / s.std()).item()
    tag = lb.replace("model.language_model.","") + f".experts.{e}.{proj}"
    print(f"K{K} {tag:44s} crossstack_maxabs={d.max().item():.2e}  vs_source_relRMSE={rel:.3%}")

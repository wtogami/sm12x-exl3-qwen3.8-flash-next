import json, glob, torch, importlib.util
from safetensors import safe_open
spec = importlib.util.spec_from_file_location("exllamav3_ext", "/hf/exv3-runtime/exllamav3_ext.so")
ext = importlib.util.module_from_spec(spec); spec.loader.exec_module(ext)
from b12x.gemm import trellis_linear as tl
B = glob.glob("/hf/hub/models--wrldsuksgo2mars--Qwen3.8-Flash-Next-EXL3-K4.25-v1/snapshots/*/")[0]
wm = json.load(open(B + "model.safetensors.index.json"))["weight_map"]
side = json.load(open(B + "quantize_config.json"))["tensor_storage"]
dev = "cuda"
def get(mod, suf): return safe_open(B + wm[f"{mod}.{suf}"], framework="pt").get_tensor(f"{mod}.{suf}").to(dev)

def cross_check(mod):
    tr, su, sv, mk = (get(mod, s) for s in ("trellis", "suh", "svh", "mcg"))
    K = tr.shape[2] // 16; n_in, n_out = tr.shape[0] * 16, tr.shape[1] * 16
    buf = torch.empty((n_in, n_out), dtype=torch.half, device=dev)
    ext.reconstruct_had_slice(buf, tr, su, sv, float(K), True, False, 0)   # mcg=True, mul1=False
    w_v = buf.T.to(torch.bfloat16).float()
    w = tl.prepare_weight(tr, su, sv, mcg=mk, codebook="mcg")
    w_b = tl.run(torch.eye(n_in, device=dev, dtype=torch.bfloat16), w).reshape(n_in, n_out).to(torch.bfloat16).T.float()
    d = (w_v - w_b).abs()
    print(f"[cross-stack {K}bpw] {mod.rsplit('.',3)[-3:]}: maxabs={d.max().item():.3e} relmax={(d/(w_b.abs()+1e-6)).max().item():.3e} std={w_b.std().item():.5f}")

# (c) methodology check on real tensors: one K4 and one K5 expert if present
import collections
by = collections.defaultdict(list)
for k, e in side.items():
    if k.endswith("experts.7.gate_proj") and e.get("bits_per_weight") in (4, 5):
        by[e["bits_per_weight"]].append(k)
for b in sorted(by):
    cross_check(by[b][0])

# (a) K6-mcg ACCEPTANCE probe: synthetic but format-legal K6 expert tensor (640x2560 like gate_proj)
tr6 = torch.zeros(160, 40, 96, dtype=torch.int16, device=dev)     # 96 = 16*6 bits
su6 = torch.ones(2560, dtype=torch.float16, device=dev)
sv6 = torch.ones(640, dtype=torch.float16, device=dev)
mk6 = torch.tensor(0xCBAC1FED - 2**32, dtype=torch.int32, device=dev)   # signed wrap, matches card markers
try:
    w6 = tl.prepare_weight(tr6, su6, sv6, mcg=mk6, codebook="mcg")
    x = torch.randn(16, 2560, dtype=torch.bfloat16, device=dev)
    y = tl.run(x, w6)
    print(f"[acceptance K6-mcg] prepare+run OK; output shape {tuple(y.shape)}, finite={torch.isfinite(y).all().item()}")
except Exception as e:
    print(f"[acceptance K6-mcg] FAILED: {type(e).__name__}: {e}")

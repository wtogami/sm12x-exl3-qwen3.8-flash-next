import time, json, torch
EXT_SRC = "/tmp/p/exv3-slim/extsrc"
t0 = time.time()
from torch.utils.cpp_extension import load
slim = load(name="exl3_recon_slim",
            sources=[EXT_SRC + "/quant/reconstruct.cu", "/tmp/p/exv3-slim/slim_bindings.cpp"],
            extra_include_paths=[EXT_SRC, "/tmp/p/exv3-slim/extra-inc"],
            extra_cuda_cflags=["-O3", "-gencode=arch=compute_120a,code=sm_120a"],
            verbose=False)
print(f"slim ext ready in {time.time()-t0:.0f}s")
from safetensors import safe_open
from b12x.gemm import trellis_linear as tl
snap = "/hf/hub/models--thelastspark--Swift-Qwen3.8-Flash-Next-exl3/snapshots/swift605-bf16ple"
wm = json.load(open(snap + "/model.safetensors.index.json"))["weight_map"]
def get(mod, s):
    return safe_open(f"{snap}/{wm[f'{mod}.{s}']}", framework="pt").get_tensor(f"{mod}.{s}")
dev = "cuda"
mod = "lm_head"   # K6: vendor vs b12x cross-validation
tr = get(mod, "trellis").to(dev); suh = get(mod, "suh").to(dev); svh = get(mod, "svh").to(dev); m1 = get(mod, "mul1").to(dev)
K = tr.shape[2] // 16; n_out = tr.shape[1] * 16; n_in = tr.shape[0] * 16
w = torch.empty((n_in, n_out), dtype=torch.half, device=dev)
slim.reconstruct_had_slice(w, tr, suh, svh, float(K), False, m1, 0)
w1 = w.to(torch.bfloat16)
wp = tl.prepare_weight(tr, suh, svh, mul1_e4m3=m1, codebook="mul1")
w2 = tl.run(torch.eye(n_in, device=dev, dtype=torch.bfloat16), wp).reshape(n_in, n_out).to(torch.bfloat16)
d = (w1.float() - w2.float()).abs()
print(f"{mod} K{K} {n_out}x{n_in}: maxabs={d.max().item():.3e} absmax={w1.float().abs().max().item():.4f} std={w1.float().std().item():.5f} finite={torch.isfinite(w1).all().item()}")

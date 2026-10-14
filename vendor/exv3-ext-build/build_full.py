import os, time, glob
EXT_SRC = "/tmp/p/exv3-slim/extsrc"
OVERLAY = "/tmp/p/exv3-slim/extra-inc"
t0 = time.time()
from torch.utils.cpp_extension import load
cu = sorted(f for f in glob.glob(EXT_SRC + "/**/*.cu", recursive=True) if "/rocm/" not in f)
cpp = sorted(f for f in glob.glob(EXT_SRC + "/**/*.cpp", recursive=True) if "/rocm/" not in f)
srcs = list(dict.fromkeys([EXT_SRC + "/bindings.cpp"] + cu + cpp))
mod = load(name="exllamav3_ext",
           sources=srcs,
           extra_include_paths=[EXT_SRC, OVERLAY],
           extra_cuda_cflags=["-O3", "-gencode=arch=compute_120a,code=sm_120a", "--expt-relaxed-constexpr"],
           extra_cflags=["-O3"],
           verbose=False)
print(f"full ext built in {time.time()-t0:.0f}s")
need = ["exl3_gemm", "exl3_moe", "reconstruct", "reconstruct_had_slice", "had_r_128"]
missing = [n for n in need if not hasattr(mod, n)]
print("symbols:", "ALL PRESENT" if not missing else f"MISSING {missing}")
import shutil
os.makedirs("/hf/exv3-runtime", exist_ok=True)
so = glob.glob("/hf/exv3-jit/exllamav3_ext/*.so")
for f in so: shutil.copy(f, "/hf/exv3-runtime/exllamav3_ext.so"); print("installed:", f)

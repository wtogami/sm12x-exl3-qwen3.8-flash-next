# exv3-ext-build — vendor exllamav3_ext rebuild recipe (CUDA 13 images)

Copied from volatile /tmp/opencode on 2026-10-14 before a reboot could eat it.
Recipe for building `exllamav3_ext` against the CUDA-13 serving image, where
pip-installed `nvidia/cu13` headers shadow the devel toolkit's
`host_runtime.h` (`__cudaLaunch(fun, isTileKernel)` macro collision):
`extra-inc/` overlay headers + `patch_v2.py` + `build_full.py` (180 TUs,
gencode sm_75..sm_120). Output lives persistently at
`~/.cache/huggingface/exv3-runtime/exllamav3_ext.so` (binding surface
matches exllamav3 1.6.1; see requant-plan version-pairing lesson).

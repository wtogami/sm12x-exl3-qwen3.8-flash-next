#!/usr/bin/env bash
case "${QUANT:-exl3}" in
  exl3)
    MODEL_REPO=wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-v1
    MODEL_REVISION=73a050c27b8c488c65acd6d1c74e45ff02be5fab
    ;;
  exl3-ple8)
    MODEL_REPO=wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-PLE-FP8-v1
    MODEL_REVISION=888306bd3996d6317758c07df50622829259ad17
    ;;
  nvfp4)
    MODEL_REPO=nvidia/Qwen3.8-Flash-Next-NVFP4
    # 2026-10-07: re-pinned from 2061e0b0 (unreachable after the repo's
    # super-squash; content verified identical) to the current main tip.
    MODEL_REVISION=fc694b54fb0174e0913e6adf86691ef85a4ead47
    ;;
  *) echo "QUANT must be exl3, exl3-ple8, or nvfp4" >&2; exit 2 ;;
esac
MODEL_CACHE_NAME="models--${MODEL_REPO//\//--}"

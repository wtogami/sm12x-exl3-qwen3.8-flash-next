#!/usr/bin/env bash
case "${QUANT:-exl3}" in
  exl3)
    MODEL_REPO=wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-v1
    MODEL_REVISION=73a050c27b8c488c65acd6d1c74e45ff02be5fab
    PLE_BYTES_PER_ELEMENT=2
    ;;
  exl3-ple8)
    MODEL_REPO=wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-PLE-FP8-v1
    MODEL_REVISION=888306bd3996d6317758c07df50622829259ad17
    PLE_BYTES_PER_ELEMENT=1
    ;;
  nvfp4)
    MODEL_REPO=nvidia/Qwen3.8-Flash-Next-NVFP4
    # 2026-10-07: re-pinned from 2061e0b0 (unreachable after the repo's
    # super-squash; content verified identical) to the current main tip.
    MODEL_REVISION=fc694b54fb0174e0913e6adf86691ef85a4ead47
    PLE_BYTES_PER_ELEMENT=1
    ;;
  # nvfp4-redhatai retired 2026-10-18: the C16 scaling collapse (see
  # docs/qualification.md and docs/performance-comparison.md) de-recommended
  # the profile, the checkpoint was deleted from the model cache, and v0.32
  # upstream absorbed the startup port it needed. Oct-7 receipts remain the
  # historical record.
  *) echo "QUANT must be exl3, exl3-ple8 or nvfp4" >&2; exit 2 ;;
esac
MODEL_CACHE_NAME="models--${MODEL_REPO//\//--}"

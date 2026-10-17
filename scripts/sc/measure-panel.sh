#!/usr/bin/env bash
# Pilot measure with the ovr-fixed runtime (patches/port-exl3-measure-ovr.py) on the
# AUTHOR'S calibration corpus (agentic-quant-panel-v1 packed, sha 6cf142dc...644b).
# Pool = the same dirs as the 2026-10-16 degenerate run (base K3, alts K4/K5/K6b, -l 2)
# so results are a clean before/after. The patched tool self-checks ovr effectiveness at
# the first target layer and aborts on an all-zero result, so a completed file means the
# measurement is live.
set -euo pipefail
CACHE=${HF_CACHE:-$HOME/.cache/huggingface}
SRC=/hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540
POOL="/hf/req3-build/out /hf/req4-build/out /hf/req5-build/out /hf/req6b-build/out"
ENVP='export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras; export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:$LD_LIBRARY_PATH;'
mkdir -p "$CACHE/sc-measure"
docker run --rm --device nvidia.com/gpu=0 -v "$CACHE:/hf:z" --entrypoint sh localhost/qwen38-rtx:local \
  -c "$ENVP python3 -m exllamav3.conversion.measure_model -i $POOL -r $SRC -l 2 -cc 512 \
      -cd /hf/calibration/agentic-quant-panel-v1/panel-qwen-packed-2048.safetensors \
      -o /hf/sc-measure/measurement-panel.json" >> "$CACHE/sc-measure/build.log" 2>&1
rc=$?
echo "$(date -Is) MEASURE-PANEL rc=$rc" >> "$CACHE/sc-measure/build.log"
exit $rc

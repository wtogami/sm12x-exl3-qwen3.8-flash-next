#!/usr/bin/env bash
# Tuned final card: measure over ladder cards -> optimize to 5.65 avg -> convert (vanilla cal).
set -euo pipefail
CACHE=${HF_CACHE:-$HOME/.cache/huggingface}; R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
mkdir -p "$CACHE/tuned-build/work" "$CACHE/tuned-build/out"
ENVP="export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras; export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:\$LD_LIBRARY_PATH;"
SRC=/hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540
if [[ ! -f "$CACHE/tuned-build/measurement.json" ]]; then
  docker run --rm --device nvidia.com/gpu=0 --device nvidia.com/gpu=1 -v "$CACHE:/hf:z" --entrypoint sh localhost/qwen38-rtx:local -c "$ENVP python3 -m exllamav3.conversion.measure_model -i /hf/req6-build/out /hf/req6b-build/out /hf/req5-build/out /hf/req4-build/out /hf/req3-build/out -r $SRC -l 2 -o /hf/tuned-build/measurement.json"
fi
docker run --rm -v "$CACHE:/hf:z" --entrypoint sh localhost/qwen38-rtx:local -c "$ENVP python3 -m exllamav3.conversion.optimize_model -m /hf/tuned-build/measurement.json -b 5.65 -i /hf/req6-build/out /hf/req6b-build/out /hf/req5-build/out -o /hf/tuned-build/recipe 2>&1 | tail -5; ls /hf/tuned-build/recipe"
echo "RECIPE READY -> run convert_model with -rcp (vanilla cal: omit -cd)"

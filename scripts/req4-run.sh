#!/usr/bin/env bash
# Burst-side uniform-4 reference quant launcher (panel calibration, TP2).
# Stop/reboot-safe: rerun to resume from last completed module checkpoint.
set -euo pipefail
CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
mkdir -p "$CACHE/req4-build/out" "$CACHE/req4-build/work"
RESUME=(); [[ -f "$CACHE/req4-build/work/ckpt/job.json" ]] && RESUME=(-r)
docker rm -f req4-convert 2>/dev/null || true
docker run -d --name req4-convert --device nvidia.com/gpu=0 --device nvidia.com/gpu=1 \
  -v "$CACHE:/hf:z" -v "$PWD:/s:z" --entrypoint sh localhost/qwen38-rtx:local -c "
export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras
export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:\$LD_LIBRARY_PATH
exec python3 -m exllamav3.conversion.convert_model \
  -i /hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540 \
  -o /hf/req4-build/out -w /hf/req4-build/work \
  -rcp /hf/req6-build/recipe-uniform4.yaml -b 4 -hb 16 -mb 6 -vb 16 \
  -cd /hf/calibration/agentic-quant-panel-v1/panel-qwen-packed-2048.safetensors \
  -cr 1426 -cc 2048 -cb mcg -d 0,1 ${RESUME[*]:-}"
echo "req4-convert launched ${RESUME[*]:-fresh}"

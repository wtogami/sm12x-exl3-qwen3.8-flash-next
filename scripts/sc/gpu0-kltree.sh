#!/usr/bin/env bash
set -u
CACHE=$HOME/.cache/huggingface; LOG=$CACHE/sc-measure/build.log
say(){ echo "[$(date +%m-%d_%H:%M)] KL2: $*" >>"$LOG"; }
ENVP='export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras; export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:;'
say "GPU0 tree jobs start"
docker run --rm --device nvidia.com/gpu=0 -v "$CACHE:/hf:z" --entrypoint sh localhost/qwen38-rtx:local \
  -c "$ENVP python3 /hf/kl-tree/bf16-logitdump.py" >>"$LOG" 2>&1 && say "bf16 logit dump done" || say "FAIL bf16 dump"
docker run --rm --device nvidia.com/gpu=0 -v "$CACHE:/hf:z" --entrypoint sh localhost/qwen38-rtx:local \
  -c "$ENVP python3 -m exllamav3.conversion.measure_model -i /hf/req4-build/out /hf/req5-build/out /hf/req6b-build/out -r /hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540 -l 0 -cc 512 -cd /hf/calibration/agentic-quant-panel-v1/panel-qwen-packed-2048.safetensors -o /hf/sc-measure/basekld-k4.json" >>"$LOG" 2>&1 \
  && grep -ao '"base_kld": [0-9.]*' "$CACHE/sc-measure/basekld-k4.json" | sed 's/^/KL2: k4 /' >>"$LOG" && say "k4 tree-kld done" || say "FAIL k4 tree"
say "GPU0 tree jobs complete"

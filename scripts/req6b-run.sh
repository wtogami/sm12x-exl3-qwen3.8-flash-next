#!/usr/bin/env bash
# Panel-calibrated uniform-K6 conversion (experiment B of the calibration
# A/B). Stop/reboot-safe by construction: rerun this exact script any time -
# it resumes the checkpointed job (-r) if one exists. Same recipe as A,
# single variable = calibration corpus. Reboot recipe: none; just rerun this.
set -euo pipefail
CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
SRC="$CACHE/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540"
mkdir -p "$CACHE/req6b-build/out"
RESUME=()
[[ -f "$CACHE/req6b-build/work/ckpt/job.json" ]] && RESUME=(-r)
# Seed the converter's throwaway ngram trellis from experiment A (byte-identical
# inputs => identical output); it is resumable and will skip re-quantizing it.
[[ -f "$CACHE/req6b-build/out/ngram_embedding.safetensors" ]] || \
  cp "$CACHE/req6-build/keep/ngram_embedding.K6-mcg.safetensors" \
     "$CACHE/req6b-build/out/ngram_embedding.safetensors"
docker rm -f req6b-convert 2>/dev/null || true
docker run -d --name req6b-convert --device nvidia.com/gpu=0 --device nvidia.com/gpu=1 \
  -v "$CACHE:/hf:z" -v "$PWD:/s:z" --entrypoint sh localhost/qwen38-rtx:local -c "
export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras
export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:\$LD_LIBRARY_PATH
exec python3 -m exllamav3.conversion.convert_model \
  -i /hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540 \
  -o /hf/req6b-build/out -w /hf/req6b-build/work \
  -rcp /hf/req6-build/recipe-uniform6.yaml -b 6 -hb 16 -mb 6 -vb 16 \
  -cd /hf/calibration/agentic-quant-panel-v1/panel-qwen-packed-2048.safetensors \
  -cr 1426 -cc 2048 -cb mcg -d 0,1 ${RESUME[*]:-}"
echo "req6b-convert launched ${RESUME[*]:-fresh job}; follow: docker logs -f req6b-convert"

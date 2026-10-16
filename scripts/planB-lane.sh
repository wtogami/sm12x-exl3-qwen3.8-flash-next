#!/usr/bin/env bash
# Plan B lane: vanilla-calibrated candidate conversion + repair chain + verify.
# usage: planB-lane.sh <rate 3|4|5>   (GPU1, single conversion at a time)
# Vanilla semantics = mirror of K6V: NO -cd (bundled corpus mix, defaults 250x2048).
# Detached-safe: watches its own container then repairs. Log: ~/.cache/huggingface/planB-<dir>.log
set -u
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
RATE=${1:?usage: planB-lane.sh 3|4|5}
DIR="req${RATE}v-build"; NAME="req${RATE}v-convert"
SRC=/hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540
LOG="$CACHE/planB-$DIR.log"; say(){ echo "[$(date +%m-%d_%H:%M)] $*" >>"$LOG"; }
[[ -f "$CACHE/$DIR/REPAIR-OK" ]] && { echo "already done"; exit 0; }
mkdir -p "$CACHE/$DIR/out" "$CACHE/$DIR/work"
RESUME=(); [[ -f "$CACHE/$DIR/work/ckpt/job.json" ]] && RESUME=(-r)
podman rm -f "$NAME" 2>/dev/null
podman run -d --name "$NAME" --device nvidia.com/gpu=1 \
  -v "$CACHE:/hf:z" --entrypoint sh localhost/qwen38-rtx:local -c "
export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras
export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:\$LD_LIBRARY_PATH
exec python3 -m exllamav3.conversion.convert_model \
  -i $SRC -o /hf/$DIR/out -w /hf/$DIR/work \
  -rcp /hf/req6-build/recipe-uniform${RATE}.yaml -b ${RATE} -hb 16 -mb 6 -vb 16 \
  -cb mcg -d 0 ${RESUME[*]:-}"
say "$NAME launched (fresh=${#RESUME[@]})"
setsid nohup bash -c '
'"$R"'/scripts/planB-repair.sh '"$RATE"' >> "'"$LOG"'" 2>&1
' >/dev/null 2>&1 &
echo "$NAME launched on GPU1; repair watcher armed (log: $LOG)"

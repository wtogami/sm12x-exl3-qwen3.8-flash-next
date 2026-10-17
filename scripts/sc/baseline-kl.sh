#!/usr/bin/env bash
# True base-KLD runs (no engine needed): vendor 4.25, measured425, measured459.
# Same calibration as the l3 curve (panel corpus, cc512, cr10, level 0) so base_kld
# is directly comparable with the K3 base_kld=0.03592 and the additive predictions.
set -u
CACHE=$HOME/.cache/huggingface; LOG=$CACHE/sc-measure/build.log
say(){ echo "[$(date +%m-%d_%H:%M)] KL: $*" >>"$LOG"; }
ENVP='export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras; export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:;'
SRC=/hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540
ALT="/hf/req5-build/out /hf/req6b-build/out"
CD=/hf/calibration/agentic-quant-panel-v1/panel-qwen-packed-2048.safetensors
S=$CACHE/hub/models--wrldsuksgo2mars--Qwen3.8-Flash-Next-EXL3-K4.25-v1/snapshots/73a050c27b8c488c65acd6d1c74e45ff02be5fab
run(){ # name basedir
  [[ -d $2 ]] || { say "SKIP $1 (missing $2)"; return; }
  docker run --rm --device nvidia.com/gpu=0 -v "$CACHE:/hf:z" --entrypoint sh localhost/qwen38-rtx:local \
    -c "$ENVP python3 -m exllamav3.conversion.measure_model -i $2 $ALT -r $SRC -l 0 -cc 512 -cd $CD -o /hf/sc-measure/basekld-$1.json" >>"$LOG" 2>&1 \
    && grep -ao '"base_kld": [0-9.]*' "$CACHE/sc-measure/basekld-$1.json" | tail -1 | sed 's/^/KL: /' >>"$LOG" \
    && say "done $1" || say "FAIL $1"; }
# wait for RB4 to finish its doomed boot polls so GPU0 is un-squatted
for i in $(seq 1 240); do grep -qa "RB4 COMPLETE" "$LOG" && break; sleep 30; done
say "starting base-KLD trio"
run vendor425 "$S"
run measured425 "$CACHE/measured425-build/out"
if [[ -f "$CACHE/measured459-build/REPAIR-OK" ]]; then run measured459 "$CACHE/measured459-build/out"; else say "WARN measured459 no REPAIR-OK; measuring out dir as-is"; run measured459 "$CACHE/measured459-build/out"; fi
say "BASE-KLD TRIO COMPLETE"

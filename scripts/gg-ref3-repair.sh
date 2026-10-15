#!/usr/bin/env bash
# Runs ON GG: wait for gg-ref3 conversion to finish, then apply the four-step
# repair chain to /hf/gg-ref3-build/out in one container pass. Detached-safe.
# Receipts log: ~/.cache/huggingface/gg-ref3-repair.log
set -u
LOG="$HOME/.cache/huggingface/gg-ref3-repair.log"
say(){ echo "[$(date +%m-%d_%H:%M)] $*" >>"$LOG"; }
say "ref3 repair watcher start"
for i in $(seq 1 360); do
  podman logs gg-ref3 2>&1 | grep -qa "Finished compiling" && break
  sleep 30
done
podman logs gg-ref3 2>&1 | grep -qa "Finished compiling" || { say "WARN: no completion marker within 3h"; exit 1; }
say "gg-ref3 compiled; starting repair chain"
C="$HOME/.cache/huggingface"
podman run --rm --device nvidia.com/gpu=0 -v "$C:/hf:z" -v "$HOME/qscripts:/s:z" --entrypoint sh localhost/qwen38-rtx:local -c '
export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras
export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:$LD_LIBRARY_PATH
SRC=/hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540
OUT=/hf/gg-ref3-build/out
set -e
python3 /s/req6-ngram-reinject.py "$SRC" "$OUT" | tail -1
python3 /s/req6-mtp-repair.py "$OUT" | tail -1
python3 /s/req6-ledger-filter.py "$OUT/quantization_config.json"
python3 /s/req6-strip-ghost-keys.py "$OUT" | tail -1' >>"$LOG" 2>&1 \
  && say "repair chain complete" || say "FAIL repair chain (see above)"

#!/usr/bin/env bash
# Enforce GPU power cap from /tmp/power-cap (watts). Re-applies via sudo if anything drifts above.
# Log one line/min: cap, actual limit, draw per GPU. Kill-by-name: power-guard.sh.
set -u
CAPF=/tmp/power-cap
while true; do
  cap=$(cat "$CAPF" 2>/dev/null || echo 450)
  for i in 0 1; do
    lim=$(nvidia-smi -i $i --query-gpu=power.limit --format=csv,noheader,nounits 2>/dev/null | cut -d. -f1)
    if [[ -n $lim && $lim -gt $cap ]]; then sudo -n nvidia-smi -pl "$cap" -i $i >/dev/null 2>&1 && echo "[$(date +%H:%M)] gpu$i was ${lim}W -> re-capped ${cap}W" >> ~/.cache/huggingface/power.log; fi
  done
  echo "[$(date +%H:%M)] cap=${cap}W $(nvidia-smi --query-gpu=power.limit,power.draw --format=csv,noheader,nounits | tr '\n' '|')" >> ~/.cache/huggingface/power.log
  sleep 60
done

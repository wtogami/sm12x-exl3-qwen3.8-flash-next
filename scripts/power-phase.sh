#!/usr/bin/env bash
# Power-phase helper. OWNER RULE: builds/quality at 450W; perf tests at EXACTLY 300W.
#   power-phase.sh set 300     -> marker + both GPUs to 300W (verified)
#   power-phase.sh restore     -> marker + both GPUs back to 450W (DEFAULT STATE ON EXIT)
# The perf ladder MUST run under:  trap 'scripts/power-phase.sh restore' EXIT INT TERM
# so a crashed/killed ladder can never strand the box at 300W for the next job.
set -euo pipefail
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
MODE=${1:?usage: set <watts>|restore}
if [[ $MODE == restore ]]; then W=${POWER_RESTORE_W:-450}; else W=${2:?watts}; fi
echo "$W" > /tmp/power-cap                    # guard enforces limit <= marker
for i in 0 1; do nvidia-smi -i $i -pl "$W" >/dev/null; done
sleep 1
got=$(nvidia-smi --query-gpu=power.limit --format=csv,noheader,nounits | tr '\n' ' ')
echo "[$(date +%m-%d_%H:%M)] power-phase $MODE -> ${W}W (marker=$(cat /tmp/power-cap), limits: $got)" \
  >> "$HOME/.cache/huggingface/power-gov.log"
for w in $got; do [[ $(python3 -c "print(abs($w-$W)<1)") == True ]] || { echo "FAILED to apply $W: got $got" >&2; exit 1; }; done
echo "power-phase $MODE OK: ${W}W on both GPUs"

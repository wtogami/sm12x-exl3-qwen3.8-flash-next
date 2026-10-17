#!/usr/bin/env bash
set -u
LOG=$HOME/.cache/huggingface/sc-measure/build.log
for i in $(seq 1 360); do grep -qa "RB4 COMPLETE" "$LOG" && break; sleep 30; done
sleep 60
cd /home/opencode/sm12x-exl3-qwen3.8-flash-next
git add benchmarks/measured425-20261017/MEASURED-425-KEPT.md scripts/sc/keep425-commit.sh 2>/dev/null
git commit -q "measured425 preserved: provenance note (unserveable on current loader; oracle artifact for the 4.25 budget)" && git push -q gg quant-exp1 2>/dev/null && echo "[$(date +%m-%d_%H:%M)] KP425: committed" >> "$LOG"

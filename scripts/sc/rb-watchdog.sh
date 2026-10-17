#!/usr/bin/env bash
# Watchdog for the RB4/RB5 chain (armed 2026-10-17 after audit). Prefix RBD.
#  1) ladder-power guard: if a ladder phase is active but caps != 300W, kill the
#     runbook (trap restores) so no 450W data enters a 300W receipt.
#  2) RB5 lifeline: if RB5 dies/exits before doing the 425 suite catch-up, run that
#     catch-up here (boot measured425 -> quality-suite -> stop -> commit).
#  3) advisory: note when a build watcher declared FAIL while its convert container
#     was still running (deadline too tight) -> manual repair-chain hint, no action.
set -u
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next; cd "$R"
CACHE=$HOME/.cache/huggingface; LOG=$CACHE/sc-measure/build.log
say(){ echo "[$(date +%m-%d_%H:%M)] RBD: $*" >>"$LOG"; }
booted_by_rbd=0; ladder_checked=0; lifeline=0
ladder_active(){ grep -qaE "running 300W ladder|300W ladder for measured459" "$LOG" && ! grep -qaE "ladder done|ladder-459 done" "$LOG"; }
rb5_dead(){ ! pgrep -f "run45[9].sh" >/dev/null; }
suite_exists(){ ls benchmarks/measured425-quality-v2/*.jsonl benchmarks/measured425-quality-v2/*.json >/dev/null 2>&1; }
say "watchdog armed"
for i in $(seq 1 2880); do
  # 1) ladder power guard
  if ladder_active; then
    lim=$(nvidia-smi --query-gpu=power.limit --format=csv,noheader,nounits | sort -n | head -1)
    if [[ "${lim:-450}" -gt 320 ]]; then
      say "ALERT: ladder active but power.limit=${lim}W -> killing runbook to protect data validity"
      pkill -f "runbook42[5].sh" 2>/dev/null; pkill -f "run45[9].sh" 2>/dev/null
      sleep 30; scripts/power-phase.sh restore >>"$LOG" 2>&1
    fi
  fi
  # 2) RB5 lifeline: 425 suite catch-up
  if [[ $lifeline == 0 ]] && grep -qa "RB4 COMPLETE" "$LOG" && rb5_dead && ! suite_exists; then
    say "LIFELINE: RB5 gone without 425 suite -> running it here"
    ./stop.sh >/dev/null 2>&1; sleep 8
    QUANT=exl3 TP_SIZE=2 GPU_MEMORY_UTILIZATION=0.88 REPLACE=1 MODEL_SNAPSHOT=measured425 \
      EXTRA_MOUNTS="$CACHE/engine-overlay/exl3.py:/usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/quantization/exl3.py:ro,z" \
      ./start.sh >>"$LOG" 2>&1
    i2=0; until curl -sf --max-time 5 http://127.0.0.1:8001/v1/models >/dev/null; do sleep 10; i2=$((i2+1)); [[ $i2 -gt 120 ]] && break; done
    if [[ $i2 -le 120 ]]; then
      rm -rf benchmarks/measured425-quality-v2
      MODEL=qwen38-exl3 RESULT_DIR=benchmarks/measured425-quality-v2 BASE_URL=http://127.0.0.1:8001 bash scripts/quality-suite.sh >>"$LOG" 2>&1 && say "lifeline 425 suite done" || say "WARN lifeline suite"
      booted_by_rbd=1
    else say "LIFELINE: engine did not come up"; fi
    ./stop.sh >/dev/null 2>&1
    git add -A >/dev/null 2>&1 && git commit -q "measured425: quality suite via watchdog lifeline" && git push -q gg quant-exp1 2>/dev/null && say "lifeline committed"
    lifeline=1
  fi
  # 3) advisory: watcher FAIL while convert still up
  if grep -qa "FAIL: no completion marker" "$CACHE/planB-measured459-build.log" 2>/dev/null && podman ps --format '{{.Names}}' | grep -qa measured459-build-convert; then
    say "NOTE: 459 watcher deadline hit while convert runs; after it finishes run repair chain manually (see build-measured459.sh watcher body) then touch REPAIR-OK"
  fi
  grep -qa "RB5 COMPLETE" "$LOG" && { say "RB5 complete -> watchdog retiring"; break; }
  sleep 60
done
say "watchdog exit"

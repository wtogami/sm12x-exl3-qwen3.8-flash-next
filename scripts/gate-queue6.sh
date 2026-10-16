#!/usr/bin/env bash
# FINAL matrix completion: K6-panel-v2 then base-v2, sequential, no inter-queue handshakes.
set -u
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
LOG=~/.cache/huggingface/gate-queue.log; say(){ echo "[$(date +%H:%M)] $*" >>"$LOG"; }
cd "$R"
run_lane(){
  ./stop.sh >/dev/null 2>&1; sleep 5
  rm -rf "benchmarks/$4"
  QUANT=exl3 TP_SIZE="$2" MODEL_SNAPSHOT="$1" GPU_MEMORY_UTILIZATION="$3" ./start.sh >>"$LOG" 2>&1
  local i=0
  until curl -s --max-time 5 http://127.0.0.1:8001/v1/models >/dev/null; do
    sleep 5; i=$((i+1)); [[ $i -gt 90 ]] && { say "WARN $1 engine not ready"; return 1; }
  done
  say "$1 ready (q6)"
  MODEL=qwen38-exl3 RESULT_DIR="benchmarks/$4" BASE_URL=http://127.0.0.1:8001 bash scripts/quality-suite.sh >>"$LOG" 2>&1 \
    && say "$4 done (q6)" || say "WARN $4 (q6)"
}
say "queue6 start"
run_lane req6b-panel6 2 0.85 req6b-quality-v2
run_lane 73a050c27b8c488c65acd6d1c74e45ff02be5fab 1 0.88 base-quality-v2
./stop.sh >/dev/null 2>&1
say "QUEUE6 DONE - MATRIX COMPLETE"

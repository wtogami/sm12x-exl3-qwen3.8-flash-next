#!/usr/bin/env bash
# TAIL queue: base 4.25bpw v2 (same-corpus profile anchor). Waits for queue2b; performs NO engine action before the wait.
set -u
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
LOG=~/.cache/huggingface/gate-queue.log; say(){ echo "[$(date +%H:%M)] $*" >>"$LOG"; }
cd "$R"
run_suite(){ MODEL=qwen38-exl3 RESULT_DIR="benchmarks/$1" BASE_URL=http://127.0.0.1:8001 bash scripts/quality-suite.sh >>"$LOG" 2>&1 && say "$1 done" || say "WARN $1 suite"; }
swap_engine(){ QUANT=exl3 TP_SIZE="$2" MODEL_SNAPSHOT="$1" GPU_MEMORY_UTILIZATION="$3" ./start.sh >>"$LOG" 2>&1; local i; for i in $(seq 1 90); do curl -s --max-time 5 http://127.0.0.1:8001/v1/models >/dev/null && { say "$1 ready"; return 0; }; sleep 5; done; say "WARN $1 not ready"; }
while pgrep -f "gate-queue2b.sh" >/dev/null; do sleep 60; done
say "queue3: queue2b finished, starting base-v2"
./stop.sh >/dev/null 2>&1; sleep 5
swap_engine 73a050c27b8c488c65acd6d1c74e45ff02be5fab 1 0.88 && run_suite base-quality-v2
./stop.sh >/dev/null 2>&1
say "BASE-V2 DONE — full matrix incl. profile anchor"

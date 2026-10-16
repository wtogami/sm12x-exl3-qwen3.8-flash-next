#!/usr/bin/env bash
# TAIL queue v2 (after queue2b): retry K6-v2 (engine died mid-suite 19:13, no HW errors logged), then base-v2.
set -u
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
LOG=~/.cache/huggingface/gate-queue.log; say(){ echo "[$(date +%H:%M)] $*" >>"$LOG"; }
cd "$R"
run_suite(){ MODEL=qwen38-exl3 RESULT_DIR="benchmarks/$1" BASE_URL=http://127.0.0.1:8001 bash scripts/quality-suite.sh >>"$LOG" 2>&1 && say "$1 done" || say "WARN $1 suite"; }
swap_engine(){ QUANT=exl3 TP_SIZE="$2" MODEL_SNAPSHOT="$1" GPU_MEMORY_UTILIZATION="$3" ./start.sh >>"$LOG" 2>&1; local i; for i in $(seq 1 90); do curl -s --max-time 5 http://127.0.0.1:8001/v1/models >/dev/null && { say "$1 ready"; return 0; }; sleep 5; done; say "WARN $1 not ready"; }
while pgrep -f "gate-queue2b.sh" >/dev/null; do sleep 60; done
say "queue3b: queue2b finished; retrying K6-v2"
./stop.sh >/dev/null 2>&1; sleep 5
rm -rf benchmarks/req6b-quality-v2
swap_engine req6b-panel6 2 0.85 && run_suite req6b-quality-v2
./stop.sh >/dev/null 2>&1; sleep 5
swap_engine 73a050c27b8c488c65acd6d1c74e45ff02be5fab 1 0.88 && run_suite base-quality-v2
./stop.sh >/dev/null 2>&1
say "QUEUE3B DONE — full matrix incl. profile anchor"

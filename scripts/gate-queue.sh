#!/usr/bin/env bash
# Serial quality-suite queue for remaining ladder cards on burst (GPU0 engine lane).
set -u
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
LOG=~/.cache/huggingface/gate-queue.log; say(){ echo "[$(date +%H:%M)] $*" >>"$LOG"; }
cd "$R"
SNAP=/home/opencode/.cache/huggingface/hub/models--wrldsuksgo2mars--Qwen3.8-Flash-Next-EXL3-K4.25-v1/snapshots
run_suite(){ MODEL=qwen38-exl3 RESULT_DIR="benchmarks/$1" BASE_URL=http://127.0.0.1:8001 bash scripts/quality-suite.sh >>"$LOG" 2>&1 && say "$1 done" || say "WARN $1 suite"; }
swap_engine(){ QUANT=exl3 TP_SIZE="$2" MODEL_SNAPSHOT="$1" GPU_MEMORY_UTILIZATION="$3" ./start.sh >>"$LOG" 2>&1; local i; for i in $(seq 1 90); do curl -s --max-time 5 http://127.0.0.1:8001/v1/models >/dev/null && { say "$1 ready"; return 0; }; sleep 5; done; say "WARN $1 not ready"; }
# K3 engine is already up
run_suite req3-quality-v2
./stop.sh >/dev/null 2>&1; sleep 5
swap_engine req4-uniform4 1 0.85 && run_suite req4-quality-v2
./stop.sh >/dev/null 2>&1; sleep 5
swap_engine req6-uniform6 2 0.85 && run_suite req6-quality-v2
./stop.sh >/dev/null 2>&1; sleep 5
swap_engine req6b-panel6 2 0.85 && run_suite req6b-quality-v2
./stop.sh >/dev/null 2>&1; sleep 5
ln -sfn ../../../req5-build/out "$SNAP/req5-panel5"
while [[ ! -f ~/.cache/huggingface/req5-build/.transfer-complete ]]; do sleep 60; done
swap_engine req5-panel5 2 0.85 && run_suite req5-quality-v2
./stop.sh >/dev/null 2>&1
say "GATE-QUEUE ALL DONE"

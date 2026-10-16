#!/usr/bin/env bash
# base-v2 retry after queue4 (K6 retry) — waits on queue4's PID, not pgrep strings.
set -u
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
LOG=~/.cache/huggingface/gate-queue.log; say(){ echo "[$(date +%H:%M)] $*" >>"$LOG"; }
cd "$R"
Q4PID=$(cat /tmp/queue4.pid 2>/dev/null || echo 527277)
while kill -0 "$Q4PID" 2>/dev/null; do sleep 60; done
say "queue5: queue4 gone; base-v2 retry"
./stop.sh >/dev/null 2>&1; sleep 5
rm -rf benchmarks/base-quality-v2
QUANT=exl3 TP_SIZE=1 MODEL_SNAPSHOT=73a050c27b8c488c65acd6d1c74e45ff02be5fab GPU_MEMORY_UTILIZATION=0.88 ./start.sh >>"$LOG" 2>&1
i=0; until curl -s --max-time 5 http://127.0.0.1:8001/v1/models >/dev/null || (( i++ > 90 )); do sleep 5; done
say "base ready (queue5)"; MODEL=qwen38-exl3 RESULT_DIR=benchmarks/base-quality-v2 BASE_URL=http://127.0.0.1:8001 bash scripts/quality-suite.sh >>"$LOG" 2>&1 && say "base-quality-v2 done (queue5)" || say "WARN base v2 (queue5)"
./stop.sh >/dev/null 2>&1
say "QUEUE5 DONE — matrix complete"

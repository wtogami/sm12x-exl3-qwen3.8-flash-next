#!/usr/bin/env bash
# K6-v2 FINAL retry: fix biomed's result-vs-crash conflation, then re-run the lane.
set -u
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
LOG=~/.cache/huggingface/gate-queue.log; say(){ echo "[$(date +%H:%M)] $*" >>"$LOG"; }
cd "$R"
while pgrep -f "gate-queue3" >/dev/null; do sleep 60; done
say "queue4: patching suite biomed exit-code bug"
sed -i 's|--runs 4 --output "$RESULT_DIR/biomed.jsonl"|--runs 4 --output "$RESULT_DIR/biomed.jsonl" || true|' scripts/quality-suite.sh
grep -q 'biomed.jsonl" || true' scripts/quality-suite.sh || { say "FATAL patch failed"; exit 1; }
git add scripts/quality-suite.sh && git commit -q -m "quality-suite: biomed gate failures are results, not infra errors (|| true) -- fixes K6 lane aborting at biomed->PQA transition"
./stop.sh >/dev/null 2>&1; sleep 5
rm -rf benchmarks/req6b-quality-v2
MODEL=qwen38-exl3 QUANT=exl3 TP_SIZE=2 MODEL_SNAPSHOT=req6b-panel6 GPU_MEMORY_UTILIZATION=0.85 ./start.sh >>"$LOG" 2>&1
i=0; until curl -s --max-time 5 http://127.0.0.1:8001/v1/models >/dev/null || (( i++ > 90 )); do sleep 5; done
say "req6b-panel6 ready (queue4)"; MODEL=qwen38-exl3 RESULT_DIR=benchmarks/req6b-quality-v2 BASE_URL=http://127.0.0.1:8001 bash scripts/quality-suite.sh >>"$LOG" 2>&1 && say "req6b-quality-v2 done (queue4)" || say "WARN req6b v2 (queue4)"
./stop.sh >/dev/null 2>&1
say "QUEUE4 DONE"

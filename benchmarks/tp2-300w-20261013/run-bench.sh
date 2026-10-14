#!/usr/bin/env bash
# Same-day A/B at matched 300 W limits, same image, same harness:
# EXL3 TP=2+EP vs TP=1. Receipts under /tmp/opencode/bench-300w-20261013.
set -uxo pipefail
cd /home/opencode/sm12x-exl3-qwen3.8-flash-next
B=/tmp/opencode/bench-300w-20261013
PORT=8125
sample() { nvidia-smi --query-gpu=timestamp,index,power.draw,clocks.sm,utilization.gpu \
  --format=csv,noheader -lms 500 > "$B/$1.csv" 2>/dev/null & echo $!; }
run_decode() { local tag=$1 cont=$2
  local sp; sp=$(sample "$tag-decode-power")
  python3 scripts/benchmark-decode.py --base-url "http://127.0.0.1:$PORT/v1" \
    --model qwen38-exl3 --profile exl3 --mtp-tokens 3 --request-mode clients \
    --output-tokens 256 --concurrency 1 2 4 8 16 --runs 3 \
    --server-container "$cont" --output "$B/$tag-decode.json" 2>&1 | tee "$B/$tag-decode.log"
  kill "$sp" 2>/dev/null || true; }
run_prefill() { local tag=$1 cont=$2
  local sp; sp=$(sample "$tag-prefill-power")
  python3 scripts/benchmark-prefill.py --base-url "http://127.0.0.1:$PORT/v1" \
    --model qwen38-exl3 --profile exl3 --prompt-tokens 131072 261632 523264 --runs 3 \
    --server-container "$cont" --output "$B/$tag-prefill.json" 2>&1 | tee "$B/$tag-prefill.log"
  kill "$sp" 2>/dev/null || true; }
wait_ready() { for _ in $(seq 1 90); do curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null && return 0; sleep 10; done; return 1; }

echo "=== leg 1: TP=2+EP $(date -Is)"
run_decode tp2-ep qwen38-tp2-test
run_prefill tp2-ep qwen38-tp2-test

echo "=== swap to TP=1 GPU0 $(date -Is)"
docker stop qwen38-tp2-test >/dev/null 2>&1
QUANT=exl3 TP_SIZE=1 GPU=0 CONTAINER_NAME=qwen38-tp1-bench PORT=$PORT ./start.sh
wait_ready || { echo "TP1 boot FAILED"; exit 1; }
nvidia-smi --query-gpu=index,enforced.power.limit --format=csv,noheader

echo "=== leg 2: TP=1 $(date -Is)"
run_decode tp1 qwen38-tp1-bench
run_prefill tp1 qwen38-tp1-bench
echo "BENCH_DONE $(date -Is)"

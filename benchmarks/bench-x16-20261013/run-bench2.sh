#!/usr/bin/env bash
# A/B re-run at matched 300 W with the x16 links restored, plus a context
# ladder (TTFT + post-prefill decode per depth) on each engine.
# Engine: canonical qwen38-exl3 on :8001. Leg1 = live TP2+EP instance;
# swap to TP1; restore TP2 at the end.
set -uxo pipefail
cd /home/opencode/sm12x-exl3-qwen3.8-flash-next
B=/tmp/opencode/bench-x16-20261013
URL=http://127.0.0.1:8001
CONT=qwen38-exl3
ts() { date -Is >> "$B/manifest.txt"; }
log_link() { echo "$(date -Is) links: $(nvidia-smi --query-gpu=pcie.link.width.current --format=csv,noheader | tr '\n' ' ') replays: $(nvidia-smi -q | grep 'Replays Since Reset' | grep -oE '[0-9]+' | tr '\n' ' ')" >> "$B/links.txt"; }
sample() { nvidia-smi --query-gpu=timestamp,index,power.draw,clocks.sm,utilization.gpu \
  --format=csv,noheader -lms 500 > "$B/$1.csv" 2>/dev/null & echo $!; }
run_decode() { local tag=$1; local sp; sp=$(sample "$tag-decode-power")
  python3 scripts/benchmark-decode.py --base-url "$URL/v1" --model qwen38-exl3 --profile exl3 \
    --mtp-tokens 3 --request-mode clients --output-tokens 256 \
    --concurrency 1 2 4 8 16 --runs 3 --server-container "$CONT" \
    --output "$B/$tag-decode.json" 2>&1 | tee "$B/$tag-decode.log"; kill "$sp" 2>/dev/null || true; ts >> "$B/manifest.txt"; }
run_prefill() { local tag=$1; local sp; sp=$(sample "$tag-prefill-power")
  python3 scripts/benchmark-prefill.py --base-url "$URL/v1" --model qwen38-exl3 --profile exl3 \
    --prompt-tokens 131072 261632 523264 --runs 3 --server-container "$CONT" \
    --output "$B/$tag-prefill.json" 2>&1 | tee "$B/$tag-prefill.log"; kill "$sp" 2>/dev/null || true; ts >> "$B/manifest.txt"; }
run_context() { local tag=$1; local sp; sp=$(sample "$tag-context-power")
  python3 scripts/benchmark-context.py --base-url "$URL/v1" --model qwen38-exl3 \
    --depths 16384 65536 131072 261632 523264 --runs 2 --warmups 1 --output-tokens 256 \
    --output "$B/$tag-context.jsonl" 2>&1 | tee "$B/$tag-context.log"; kill "$sp" 2>/dev/null || true; ts >> "$B/manifest.txt"; }
wait_ready() { for _ in $(seq 1 60); do curl -sf "$URL/v1/models" >/dev/null && return 0; sleep 15; done; return 1; }

echo "=== leg 1: TP=2+EP (live container) $(date -Is)" > "$B/manifest.txt"
log_link
run_decode tp2-ep; run_prefill tp2-ep; run_context tp2-ep

echo "=== swap to TP=1 GPU0 $(date -Is)" >> "$B/manifest.txt"
QUANT=exl3 TP_SIZE=1 GPU=0 REPLACE=1 ./start.sh >/dev/null
wait_ready || { echo "TP1 boot FAILED" >> "$B/manifest.txt"; exit 1; }
log_link

echo "=== leg 2: TP=1 $(date -Is)" >> "$B/manifest.txt"
run_decode tp1; run_prefill tp1; run_context tp1

echo "=== restoring TP=2+EP $(date -Is)" >> "$B/manifest.txt"
QUANT=exl3 TP_SIZE=2 REPLACE=1 ./start.sh >/dev/null
wait_ready && echo "RESTORED $(date -Is)" >> "$B/manifest.txt" || echo "RESTORE FAILED" >> "$B/manifest.txt"
log_link
echo "BENCH2_DONE $(date -Is)" >> "$B/manifest.txt"

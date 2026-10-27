#!/usr/bin/env bash
# Phase 2: runtime-growth gate. For each util that booted in Phase 1, run the
# memory-riskiest runtime shapes (vision batch, structured output, deep prefill,
# 16-way concurrency) while sampling VRAM every 2 s; record peak used.
set -uo pipefail
# Repo-relative, not a hardcoded /home/rtx path: this driver runs on gg and on burst.
cd "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Same override surface as util-ladder.sh, and the same requirement that OUT
# match: this script derives its cell list from that directory's results.tsv.
QUANT="${QUANT:-exl3-ple8}"
MODEL="${MODEL:-qwen38-$QUANT}"
NAME="${CONTAINER_NAME:-qwen38-$QUANT}"
PORT="${PORT:-8199}"
OUT="${OUT:-/home/rtx/util-ladder}"; B=http://127.0.0.1:"$PORT"; IMG="${IMAGE:-qwen38-rtx:utiltest}"
SROW="$OUT/stress.tsv"; [[ -e "$SROW" ]] || printf "util\tstress\tpeak_used_MiB\tfinal_free_MiB\toom_lines\tnote\n" > "$SROW"

# wait for phase 1 to finish
for i in $(seq 1 240); do grep -q "LADDER DONE" "$OUT/results.tsv" 2>/dev/null && break; sleep 30; done

stop_engine() { QUANT=$QUANT IMAGE=$IMG PORT=$PORT bash stop.sh >>"$OUT/ops.log" 2>&1 || docker rm -f "$NAME" >/dev/null 2>&1; sleep 5; }

for u in $(awk -F'\t' '$2=="OK" && $1!="0.94" {print $1}' "$OUT/results.tsv"); do
  grep -q "^$u\b" "$SROW" && continue
  stop_engine
  QUANT=$QUANT IMAGE=$IMG GPU=0 PORT=$PORT GPU_MEMORY_UTILIZATION=$u bash start.sh >>"$OUT/ops.log" 2>&1 &
  ready=0
  for i in $(seq 1 100); do curl -sf $B/v1/models >/dev/null 2>&1 && { ready=1; break; }; sleep 10; done
  [[ $ready == 1 ]] || { printf "%s\tBOOT-REFAIL\t\t\t\t\n" "$u" >> "$SROW"; stop_engine; continue; }
  # VRAM sampler
  : > "$OUT/samples-$u.txt"
  ( for s in $(seq 1 900); do nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits >> "$OUT/samples-$u.txt"; sleep 2; done ) & samp=$!
  fail=""
  burst_pids=()
  # Note the asymmetry, because it is a trap: test-vision-vllm.py takes --base-url
  # WITHOUT /v1 and appends its own, the other two legs take the /v1 root. "Tidying"
  # these three to match silently breaks the vision leg.
  python3 scripts/test-vision-vllm.py --base-url $B --model "$MODEL" --image-counts 16 --output "$OUT/stress-vision-$u.json" >>"$OUT/stress-$u.log" 2>&1 || fail="$fail vision"
  python3 scripts/verify-structured-output-live.py --base-url $B/v1 --model "$MODEL" --output "$OUT/stress-structured-$u.json" >>"$OUT/stress-$u.log" 2>&1 || fail="$fail structured"
  python3 scripts/test-context-retrieval.py --base-url $B/v1 --model "$MODEL" --filler-tokens 240000 --output "$OUT/stress-retr-$u.json" >>"$OUT/stress-$u.log" 2>&1 || fail="$fail prefill240k"
  for c in $(seq 1 16); do
    curl -s $B/v1/chat/completions -H 'Content-Type: application/json' -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Count 1-200 plainly.\"}],\"max_tokens\":200,\"temperature\":0}" -o /dev/null &
    burst_pids+=("$!")
  done
  # `wait` with no arguments also waits for the sampler subshell, and that loop is
  # a 900-iteration/30-minute cap -- so every cell idles out the remainder of the
  # sampler window with the engine already finished, and the `kill $samp` below is
  # unreachable. On the Oct-23 ladder the 0.98 cell completed all four risk shapes
  # at 19:11 and cannot record its row until the sampler's 30-minute window closes.
  # Wait on the requests only.
  for p in "${burst_pids[@]}"; do wait "$p"; done
  sleep 5; kill $samp 2>/dev/null; wait $samp 2>/dev/null
  peak=$(sort -n "$OUT/samples-$u.txt" | tail -1); final_free=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -1)
  docker logs "$NAME" 2>&1 | grep -icE "out of memory|CUDA error" > "$OUT/oom-$u.txt" || true
  printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$u" "${fail:-PASS}" "$peak" "$final_free" "$(cat "$OUT/oom-$u.txt")" "$(tail -c 80 "$OUT/stress-$u.log" | tr -d '\n')" >> "$SROW"
  stop_engine
done
echo "STRESS DONE $(date -u +%FT%TZ)" >> "$SROW"

#!/usr/bin/env bash
# Remaining ladder legs: K6V + 4.25bpw @300W. Owner ruling 20:05: single in-window
# replay event = noise; monitor + log, DO NOT halt. Trap-restores 450W.
set -u
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next; cd "$R"
CACHE="$HOME/.cache/huggingface"; LOG="$CACHE/tuned-build/build.log"
say(){ echo "[$(date +%m-%d_%H:%M)] QL2: $*" >>"$LOG"; }
B="$R/benchmarks/ladder-20261016"; URL=http://127.0.0.1:8001
replay(){ timeout 20 nvidia-smi -q 2>/dev/null | grep -a "Replays Since Reset" | awk -F: '{gsub(/ /,"",$2); printf "%s",$2}'; }
boot(){ ./stop.sh >/dev/null 2>&1; sleep 8
  QUANT=exl3 TP_SIZE="$2" GPU_MEMORY_UTILIZATION=0.88 REPLACE=1 MODEL_SNAPSHOT="$1" \
    EXTRA_MOUNTS="$CACHE/engine-overlay/exl3.py:/usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/quantization/exl3.py:ro,z" \
    ./start.sh >>"$LOG" 2>&1
  local i=0; until curl -sf --max-time 5 "$URL/v1/models" >/dev/null; do sleep 10; i=$((i+1)); [[ $i -gt 150 ]] && return 1; done; }
ladder(){ local tag=$1 sp
  sp=$(nvidia-smi --query-gpu=timestamp,index,power.draw,clocks.sm,utilization.gpu --format=csv,noheader -lms 500 > "$B/$tag-power.csv" 2>/dev/null & echo $!)
  python3 scripts/benchmark-decode.py --base-url "$URL/v1" --model qwen38-exl3 --profile exl3 --mtp-tokens 3 \
    --request-mode clients --output-tokens 256 --concurrency 1 2 4 8 16 --runs 3 --server-container qwen38-exl3 \
    --output "$B/$tag-decode.json" >>"$LOG" 2>&1 || say "WARN decode $tag"
  python3 scripts/benchmark-prefill.py --base-url "$URL/v1" --model qwen38-exl3 --profile exl3 \
    --prompt-tokens 131072 261632 523264 --runs 3 --server-container qwen38-exl3 \
    --output "$B/$tag-prefill.json" >>"$LOG" 2>&1 || say "WARN prefill $tag"
  python3 scripts/benchmark-context.py --base-url "$URL/v1" --model qwen38-exl3 \
    --depths 16384 65536 131072 261632 523264 --runs 2 --warmups 1 --output-tokens 256 \
    --output "$B/$tag-context.jsonl" >>"$LOG" 2>&1 || say "WARN context $tag"
  kill "$sp" 2>/dev/null || true; }
trap 'scripts/power-phase.sh restore >>"$LOG" 2>&1; say "power restored (trap)"' EXIT INT TERM
scripts/power-phase.sh set 300 >>"$LOG" 2>&1 || { say "FATAL set 300W"; exit 1; }
for spec in "k6v req6-uniform6 2" "k425 73a050c27b8c488c65acd6d1c74e45ff02be5fab 2"; do
  set -- $spec
  [[ -f "$B/$1-decode.json" ]] && { say "$1 already done; skip"; continue; }
  pre=$(replay); say "ladder boot $1 (replays before: $pre)"
  boot "$2" "$3" || { say "BOOT FAIL $1 (TP$3) - continuing"; continue; }
  ladder "$1"
  post=$(replay)
  [[ "$post" == "$pre" ]] && say "ladder done: $1 (replays flat at $post)" || say "ladder done: $1 (replays $pre -> $post, LOGGED per owner ruling, not halting)"
done
./stop.sh >/dev/null 2>&1
scripts/power-phase.sh restore >>"$LOG" 2>&1; trap - EXIT
echo "$(date -Is) links: $(nvidia-smi --query-gpu=pcie.link.width.current --format=csv,noheader | tr '\n' ' ') replays: $(nvidia-smi -q | grep -a 'Replays Since Reset' | grep -aoE '[0-9]+' | tr '\n' ' ')" > "$B/links.txt"
say "LADDER COMPLETE (4/4)"

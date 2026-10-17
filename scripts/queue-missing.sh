#!/usr/bin/env bash
# OWNER ASK 17:50: run the missing results.
# Phase 1 (450W): biomed-x20 defect probes for K6V (TP2) and 4.25bpw (TP2+EP; TP1 fallback).
# Phase 2 (300W): matched perf ladder - decode(C1..16,MTP3) + prefill(131/261/523K) + context
#   ladder(16K..523K) for tuned-panel, tuned-vanilla, K6V, 4.25bpw. Trap-restores 450W.
# Replay circuit-breaker: per-card in-window baseline; any increase => halt + restore.
set -u
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next; cd "$R"
CACHE="$HOME/.cache/huggingface"
LOG="$CACHE/tuned-build/build.log"; say(){ echo "[$(date +%m-%d_%H:%M)] QM: $*" >>"$LOG"; }
B="$R/benchmarks/ladder-20261016"; mkdir -p "$B"
SNAP="$CACHE/hub/models--wrldsuksgo2mars--Qwen3.8-Flash-Next-EXL3-K4.25-v1/snapshots"
URL=http://127.0.0.1:8001
replay0(){ timeout 20 nvidia-smi -q 2>/dev/null | grep -a "Replays Since Reset" | head -1 | awk -F: '{gsub(/ /,"",$2); print $2}'; }
boot(){ # snapshot tp [probe-only]
  ./stop.sh >/dev/null 2>&1; sleep 8
  QUANT=exl3 TP_SIZE="$2" GPU_MEMORY_UTILIZATION=0.88 REPLACE=1 \
    MODEL_SNAPSHOT="$1" EXTRA_MOUNTS="$CACHE/engine-overlay/exl3.py:/usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/quantization/exl3.py:ro,z" \
    ./start.sh >>"$LOG" 2>&1
  local i=0
  until curl -sf --max-time 5 "$URL/v1/models" >/dev/null; do sleep 10; i=$((i+1)); [[ $i -gt 150 ]] && return 1; done
  return 0; }
wait_ready(){ curl -sf --max-time 5 "$URL/v1/models" >/dev/null; }
probe(){ # tag
  local out="$R/benchmarks/$1-probe-biomed"; rm -rf "$out"; mkdir -p "$out"
  python3 scripts/test-biomed-analysis.py --base-url "$URL" --model qwen38-exl3 --runs 20 \
    --output "$out/biomed20.jsonl" >>"$LOG" 2>&1 || true
  say "probe done: $1"; }
ladder(){ # tag
  local sp; sp=$(nvidia-smi --query-gpu=timestamp,index,power.draw,clocks.sm,utilization.gpu --format=csv,noheader -lms 500 > "$B/$1-power.csv" 2>/dev/null & echo $!)
  python3 scripts/benchmark-decode.py --base-url "$URL/v1" --model qwen38-exl3 --profile exl3 --mtp-tokens 3 \
    --request-mode clients --output-tokens 256 --concurrency 1 2 4 8 16 --runs 3 --server-container qwen38-exl3 \
    --output "$B/$1-decode.json" >>"$LOG" 2>&1 || say "WARN decode $1"
  python3 scripts/benchmark-prefill.py --base-url "$URL/v1" --model qwen38-exl3 --profile exl3 \
    --prompt-tokens 131072 261632 523264 --runs 3 --server-container qwen38-exl3 \
    --output "$B/$1-prefill.json" >>"$LOG" 2>&1 || say "WARN prefill $1"
  python3 scripts/benchmark-context.py --base-url "$URL/v1" --model qwen38-exl3 \
    --depths 16384 65536 131072 261632 523264 --runs 2 --warmups 1 --output-tokens 256 \
    --output "$B/$1-context.jsonl" >>"$LOG" 2>&1 || say "WARN context $1"
  kill "$sp" 2>/dev/null || true
  say "ladder done: $1"; }

say "== PHASE 1: x20 defect probes at 450W"
if [[ -f benchmarks/k6v-probe20-probe-biomed/biomed20.jsonl && -f benchmarks/k425-probe20-probe-biomed/biomed20.jsonl ]]; then say "phase 1 already complete; skipping"; SKIP1=1; else SKIP1=0; fi
for spec in "k6v req6-uniform6 2" "k425 73a050c27b8c488c65acd6d1c74e45ff02be5fab 2"; do
  [[ $SKIP1 == 1 ]] && break
  set -- $spec; local_lb=$(replay0)
  boot "$2" "$3" || { [[ $3 == 2 ]] && { say "$1 TP2 boot failed, TP1 fallback"; boot "$2" 1 || { say "HALT: $1 boot failed both"; exit 1; }; } || { say "HALT: $1 boot failed"; exit 1; }; }
  r=$(replay0); [[ "$r" == "$local_lb" ]] || { say "HALT-BY-REPLAY during $1 boot ($local_lb->$r)"; ./stop.sh >/dev/null 2>&1; exit 2; }
  probe "$1-probe20"
  r=$(replay0); [[ "$r" == "$local_lb" ]] || { say "HALT-BY-REPLAY during $1 probe ($local_lb->$r)"; ./stop.sh >/dev/null 2>&1; exit 2; }
done
./stop.sh >/dev/null 2>&1

say "== PHASE 2: perf ladder at 300W (trap-restores 450W)"
trap 'scripts/power-phase.sh restore >>"$LOG" 2>&1; say "power restored to 450W (trap)"' EXIT INT TERM
scripts/power-phase.sh set 300 >>"$LOG" 2>&1 || { say "FATAL: could not set 300W"; exit 1; }
for spec in "tuned565-panel tuned565-panel 2" "tuned565-vanilla tuned565-vanilla 2" "k6v req6-uniform6 2" "k425 73a050c27b8c488c65acd6d1c74e45ff02be5fab 2"; do
  set -- $spec
  say "ladder boot $1"
  boot "$2" "$3" || { say "HALT: $1 ladder boot failed"; exit 1; }
  local_lb=$(replay0)
  ladder "$1"
  r=$(replay0); [[ "$r" == "$local_lb" ]] || { say "HALT-BY-REPLAY during $1 ladder window ($local_lb->$r)"; ./stop.sh >/dev/null 2>&1; exit 2; }
done
./stop.sh >/dev/null 2>&1
scripts/power-phase.sh restore >>"$LOG" 2>&1; trap - EXIT
echo "$(date -Is) links: $(nvidia-smi --query-gpu=pcie.link.width.current --format=csv,noheader | tr '\n' ' ') replays: $(nvidia-smi -q | grep -a 'Replays Since Reset' | grep -aoE '[0-9]+' | tr '\n' ' ')" > "$B/links.txt"
say "MISSING RESULTS COMPLETE: probes under benchmarks/*-probe20-probe-biomed, ladder under benchmarks/ladder-20261016/"

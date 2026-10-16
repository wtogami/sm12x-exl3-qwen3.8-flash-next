#!/usr/bin/env bash
# Overnight autonomous queue (owner-approved pipeline): wait for both tuned cards,
# then TP2-boot each and run full v2 suite + biomed-x20 probe; ngram-dedup at the end.
# OWNER STOP-RULE: any increase in GPU0 "Replays Since Reset" during inference windows
# => halt everything, no further engine starts, loud log marker.
set -u
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
LOG="$CACHE/tuned-build/build.log"; say(){ echo "[$(date +%m-%d_%H:%M)] QT: $*" >>"$LOG"; }
cd "$R"
# single-instance guard (triple-race 14:18 stalled all lanes)
if [[ -f /tmp/qt.pid ]] && kill -0 "$(cat /tmp/qt.pid)" 2>/dev/null; then
  echo "[$(date +%m-%d_%H:%M)] QT: already running ($(cat /tmp/qt.pid)); refusing second instance" >> "$HOME/.cache/huggingface/tuned-build/build.log"
  exit 1
fi
echo $$ > /tmp/qt.pid
replay0(){ timeout 20 nvidia-smi -q 2>/dev/null | grep -a "Replays Since Reset" | head -1 | awk -F: '{gsub(/ /,"",$2); print $2}'; }
BASE=$(replay0); say "queue start; replay baseline GPU0=$BASE"
# Owner ruling 2026-10-16 08:30: a single replay during conversion DMA = noise, proceed.
# Semantics now: halt only on NEW increases beyond this captured baseline during inference.
[[ "$BASE" =~ ^[0-9]+$ ]] || { say "STOP: unreadable replay baseline"; exit 1; }

# 1) wait for both REPAIR-OK sentinels (max ~10 h)
for i in $(seq 1 1200); do
  [[ -f "$CACHE/tuned565-panel-build/REPAIR-OK" && -f "$CACHE/tuned565-vanilla-build/REPAIR-OK" ]] && break
  sleep 30
done
[[ -f "$CACHE/tuned565-panel-build/REPAIR-OK" && -f "$CACHE/tuned565-vanilla-build/REPAIR-OK" ]] || { say "STOP: tuned cards not REPAIR-OK within budget — investigate before running"; exit 1; }
say "both tuned cards REPAIR-OK; qualification gate"
# wait for GPU lanes to be free of converters (K4V etc) so TP2 @0.88 has full VRAM
for i in $(seq 1 480); do
  BUSY=$(podman ps --format '{{.Names}}' | grep -ac "convert" || true)
  [[ "$BUSY" == "0" ]] && break; sleep 30
done
[[ "$(podman ps --format '{{.Names}}' | grep -ac convert || true)" == "0" ]] || { say "STOP: converters still running after 4h"; exit 1; }
say "GPUs quiet; starting qualification"

# 2) snapshot links
SNAP="$CACHE/hub/models--wrldsuksgo2mars--Qwen3.8-Flash-Next-EXL3-K4.25-v1/snapshots"
ln -sfn ../../../tuned565-panel-build/out   "$SNAP/tuned565-panel"
ln -sfn ../../../tuned565-vanilla-build/out "$SNAP/tuned565-vanilla"

run_lane(){ # snapshot_dir result_dir
  ./stop.sh >/dev/null 2>&1; sleep 8
  rm -rf "benchmarks/$2"
  QUANT=exl3 TP_SIZE=2 MODEL_SNAPSHOT="$1" GPU_MEMORY_UTILIZATION=0.88 ./start.sh >>"$LOG" 2>&1
  local i=0
  until curl -s --max-time 5 http://127.0.0.1:8001/v1/models >/dev/null; do
    sleep 10; i=$((i+1)); [[ $i -gt 60 ]] && { say "STOP: $1 engine not ready in 10min"; return 1; }
  done
  say "$1 engine ready"
  LB=$(replay0); say "lane baseline GPU0 replays=$LB (engine window opens)"
  MODEL=qwen38-exl3 RESULT_DIR="benchmarks/$2" BASE_URL=http://127.0.0.1:8001 bash scripts/quality-suite.sh >>"$LOG" 2>&1 \
    && say "SUITE OK: $2" || say "WARN suite nonzero: $2 (read receipts)"
  rm -rf "benchmarks/$2-probe-biomed"; mkdir -p "benchmarks/$2-probe-biomed"
  MODEL=qwen38-exl3 BASE_URL=http://127.0.0.1:8001 timeout 3000 python3 scripts/test-biomed-analysis.py \
    --base-url http://127.0.0.1:8001 --model qwen38-exl3 --runs 20 --output "benchmarks/$2-probe-biomed/biomed20.jsonl" \
    >>"$LOG" 2>&1 || true
  say "biomed20 done: $2"
  local r; r=$(replay0)
  say "post-lane GPU0 replays=$r (lane baseline $LB; global $BASE)"
  [[ "$r" == "$LB" ]] || { say "HALT-BY-REPLAY: increase INSIDE inference window ($LB -> $r); queue stopping"; ./stop.sh >/dev/null 2>&1; exit 2; }
}

run_lane tuned565-panel   tuned565-panel-quality-v2   || { say "queue aborted lane1"; exit 3; }
run_lane tuned565-vanilla tuned565-vanilla-quality-v2 || { say "queue aborted lane2"; exit 4; }
./stop.sh >/dev/null 2>&1; sleep 10

# 3) ngram dedup across new cards (safe, offline)
python3 scripts/dedup-ngrams.py "$CACHE/tuned565-panel-build/out" "$CACHE/tuned565-vanilla-build/out" >>"$LOG" 2>&1 \
  && say "dedup done on tuned cards" || say "WARN dedup failed"
say "QUEUE-TUNED COMPLETE — all receipts under benchmarks/tuned565-*-quality-v2*"

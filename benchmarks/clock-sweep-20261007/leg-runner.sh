#!/usr/bin/env bash
# Clock-sweep leg driver: EXL3 sweep -> swap to nvidia NVFP4 -> nvidia sweep.
# The EXIT trap restores the EXL3 engine and resets GPU clocks/power limit,
# so the host ends in the known-good state even on mid-leg failure.
set -euo pipefail
cd /home/opencode/sm12x-exl3-qwen3.8-flash-next
OUT_ROOT=benchmarks/clock-sweep-20261007
LOG=/tmp/opencode/clock-sweep-leg.log

wait_gone() {  # $1 = container name
  for _ in $(seq 1 90); do
    docker ps -a --format '{{.Names}}' | grep -qx "$1" || return 0
    sleep 5
  done
  return 1
}

stop_container() {  # $1 = container name
  docker stop -t 120 "$1" >/dev/null 2>&1 || true
  wait_gone "$1" || docker rm -f "$1" >/dev/null 2>&1 || true
}

restore_exl3() {
  {
    echo "=== restore: tearing down both engines ==="
    stop_container qwen38-nvfp4
    stop_container qwen38-exl3
    sudo nvidia-smi -rgc >/dev/null 2>&1 || true
    sudo nvidia-smi -pl 600 >/dev/null 2>&1 || true
    echo "=== restore: starting EXL3 ==="
    ./start.sh
    for _ in $(seq 1 120); do
      curl -sf http://127.0.0.1:8001/v1/models >/dev/null 2>&1 && break
      sleep 5
    done
    curl -sf http://127.0.0.1:8001/v1/models >/dev/null
    echo "=== restore complete: $(date -u +%FT%TZ) ==="
  } >> "$LOG" 2>&1
}
trap restore_exl3 EXIT

echo "=== leg start: $(date -u +%FT%TZ) ===" >> "$LOG"

echo "=== EXL3 sweep ===" >> "$LOG"
QUANT=exl3 MODEL=qwen38-exl3 OUT="$OUT_ROOT/exl3" \
  python3 scripts/clock-sweep.py >> "$LOG" 2>&1

echo "=== swap to nvidia ===" >> "$LOG"
stop_container qwen38-exl3
stop_container qwen38-nvfp4
QUANT=nvfp4 MTP_TOKENS=3 LONGCTX=1 ./start.sh >> "$LOG" 2>&1
for _ in $(seq 1 120); do
  curl -sf http://127.0.0.1:8001/v1/models >/dev/null 2>&1 && break
  sleep 5
done
curl -sf http://127.0.0.1:8001/v1/models >/dev/null
echo "=== nvidia engine up: $(date -u +%FT%TZ); settling 120 s ===" >> "$LOG"
sleep 120

echo "=== nvidia sweep ===" >> "$LOG"
QUANT=nvfp4 MODEL=qwen38-nvfp4 OUT="$OUT_ROOT/nvfp4" \
  python3 scripts/clock-sweep.py >> "$LOG" 2>&1

echo "=== leg done: $(date -u +%FT%TZ) ===" >> "$LOG"
#!/usr/bin/env bash
# Single-instance launcher for guard/governor/telemetry via pidfiles. Idempotent.
set -u
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next; cd "$R"
mkdir -p /tmp
start(){ # name cmd
  local pidf=/tmp/$1.pid
  if [[ -f $pidf ]] && kill -0 "$(cat "$pidf")" 2>/dev/null; then echo "$1 already running ($(cat "$pidf"))"; return; fi
  setsid bash -c "$2" >/dev/null 2>&1 </dev/null & echo $! > "$pidf"; echo "$1 started ($!)";
}
start power-guard "bash scripts/power-guard.sh"
start power-govern "bash scripts/power-govern.sh"
start telemetry 'while true; do v=$(timeout 20 nvidia-smi -q 2>/dev/null | grep -aA0 "Replays Since Reset" | awk -F: "{gsub(/ /,\"\",\$2); printf \"%s \",\$2}"); p=$(nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits | tr "\n" "/"); echo "[$(date +%m-%d_%H:%M)] telemetry replays:$v draw:${p}W" >> ~/.cache/huggingface/gg-watch.log; sleep 300; done'

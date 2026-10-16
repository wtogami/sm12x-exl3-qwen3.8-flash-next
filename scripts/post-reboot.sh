#!/usr/bin/env bash
# Run on burst after the GPU0 reseat boot. Idempotent.
set -u
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next; cd "$R"
echo 300 > /tmp/power-cap; rm -f /tmp/power-govern   # governor armed, conservative seed
pgrep -f "power-guar"".d.sh" >/dev/null || setsid bash scripts/power-guard.sh >/dev/null 2>&1 </dev/null &
pgrep -f "power-gove"".rn.sh" >/dev/null || setsid bash scripts/power-govern.sh >/dev/null 2>&1 </dev/null &
pgrep -f "gg-watch.telem"",try" >/dev/null || setsid bash -c 'while true; do v=$(timeout 20 nvidia-smi -q 2>/dev/null | grep -aA0 "Replays Since Reset" | awk -F: "{gsub(/ /,\"\",\$2); printf \"%s \",\$2}"); p=$(nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits | tr "\n" "/"); echo "[$(date +%m-%d_%H:%M)] telemetry replays:$v draw:${p}W" >> ~/.cache/huggingface/gg-watch.log; sleep 300; done' >/dev/null 2>&1 </dev/null &
echo "post-reboot baseline:"; nvidia-smi -q 2>/dev/null | grep -a "Replays Since Reset"; nvidia-smi --query-gpu=index,power.limit --format=csv,noheader
echo "now: measure -> optimize -> convert (tuned@5.65, vanilla cal). Launch with:"
echo "  setsid bash scripts/tuned-build.sh >/dev/null 2>&1 </dev/null &"

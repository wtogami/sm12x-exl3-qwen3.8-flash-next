#!/usr/bin/env bash
# Run on burst after a reboot. Idempotent. BOOT_CAP=<watts> to seed the ceiling (default 300).
set -u
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next; cd "$R"
echo "${BOOT_CAP:-300}" > /tmp/power-cap
[[ -f /tmp/power-adjust ]] || echo off > /tmp/power-adjust
bash scripts/start-daemons.sh
echo "post-reboot baseline:"; nvidia-smi -q 2>/dev/null | grep -a "Replays Since Reset"
nvidia-smi --query-gpu=index,power.limit --format=csv,noheader

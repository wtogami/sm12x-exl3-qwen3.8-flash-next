#!/usr/bin/env bash
# Completion watcher v5 — fully autonomous; no chat session needed.
# Layout: one converter per GPU on burst (req4 on GPU0, req3 on GPU1, launched together),
#         gg chains gg-ref5 -> gg-ref3 on its own card. No serving engines during quant.
# Burst phase also relaunches a dead container via its launcher script (ckpt resume).
# PCIe replay telemetry (burst GPUs, GPU0 first) every 5 ticks (~5 min).
set -u
LOG=~/.cache/huggingface/gg-watch.log
OPT="-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30"
cd "$HOME/sm12x-exl3-qwen3.8-flash-next" || exit 1
say(){ echo "[$(date +%m-%d_%H:%M)] $*" >>"$LOG"; }
replays(){ timeout 20 nvidia-smi -q 2>/dev/null | grep -aA0 "Replays Since Reset" | awk -F: '{gsub(/ /,"",$2); printf "%s ",$2}'; echo; }
waitfor(){ # container_name deadline_s host(burst|gg) launcher-script(or empty)
  local n=$1 dl=$2 host=$3 ls=$4; SECONDS=0; local i=0
  while (( SECONDS < dl )); do
    if [[ "$host" == gg ]]; then
      ssh $OPT rtx@gg "podman logs $n 2>&1 | tail -6" 2>/dev/null | grep -qa "Finished compiling" && { say "$n finished (${SECONDS}s)"; return 0; }
    else
      docker logs "$n" 2>&1 | grep -qa "Finished compiling" && { say "$n finished (${SECONDS}s)"; return 0; }
      if [[ -n "$ls" ]] && ! docker ps --format '{{.Names}}' | grep -qx "$n"; then bash "$ls" >>"$LOG" 2>&1 && say "$n relaunched"; fi
    fi
    (( ++i % 5 == 0 )) && say "tick($host:$n) replays:$(replays)"
    sleep 60
  done
  say "WARN $n deadline ${dl}s reached without completion marker"; return 1
}
launch_gg_ref3(){ ssh $OPT rtx@gg bash -s <<'EOF' >>"$LOG" 2>&1
C=~/.cache/huggingface
mkdir -p $C/gg-ref3-build/out $C/gg-ref3-build/work
podman rm -f gg-ref3 >/dev/null 2>&1
podman run -d --name gg-ref3 --device nvidia.com/gpu=0 -v $C:/hf:z --entrypoint sh localhost/qwen38-rtx:local -c 'export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras; export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:$LD_LIBRARY_PATH; exec python3 -m exllamav3.conversion.convert_model -i /hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540 -o /hf/gg-ref3-build/out -w /hf/gg-ref3-build/work -rcp /hf/req6-build/recipe-uniform3.yaml -b 3 -hb 16 -mb 6 -vb 16 -cd /hf/calibration/agentic-quant-panel-v1/panel-qwen-packed-2048.safetensors -cr 1426 -cc 2048 -cb mcg -d 0'
EOF
}
say "watch v5 start"
waitfor gg-ref5 36000 gg "" && launch_gg_ref3 && say "gg-ref3 launched on gg"
waitfor req4-convert 28800 burst scripts/req4-run.sh
waitfor req3-convert 28800 burst scripts/req3-run.sh
say "watch end: all reference quants done"

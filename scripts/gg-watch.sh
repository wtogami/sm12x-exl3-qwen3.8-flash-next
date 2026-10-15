#!/usr/bin/env bash
# Completion watcher: gg-ref5 -> gg engine restore; req4 -> req3 on burst.
# PCIe replay telemetry: two numbers per tick = GPU0 then GPU1 (nvidia-smi order).
# Signal-integrity watch focuses on GPU0; GPU1 values are context only.
set -u
LOG=~/.cache/huggingface/gg-watch.log
OPT="-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30"
say(){ echo "[$(date +%m-%d_%H:%M)] $*" >>"$LOG"; }
replays(){ # first number = GPU0 (the card under signal-integrity watch)
 awk '/GPU 0000/{g=$0} /Replays Since Reset/{split($0,a,":"); gsub(/ /,"",a[2]); printf "%s", a[2] " "}' <<<"$(timeout 20 nvidia-smi -q)"; echo; }
wait_done(){ # name
  local n=$1
  for i in $(seq 1 480); do
    ssh $OPT rtx@gg "podman logs $n 2>&1 | tail -4" 2>/dev/null | grep -qa "Finished compiling" && { say "$n finished"; return 0; }
    [[ $((i % 6)) -eq 0 ]] && say "tick gg=$n blocks=$(ssh $OPT rtx@gg 'ls ~/.cache/huggingface/'$n'-build/work/qtensors 2>/dev/null | wc -l' 2>/dev/null) replays:$(replays)"
    sleep 30
  done
  say "WARN $n wait window exhausted"; return 1
}
say "watch start"
wait_done gg-ref5 || exit 1
ssh $OPT rtx@gg bash -s <<'EOF' >>"$LOG" 2>&1
C=~/.cache/huggingface
mkdir -p $C/gg-ref3-build/out $C/gg-ref3-build/work
podman rm -f gg-ref3 >/dev/null 2>&1
podman run -d --name gg-ref3 --device nvidia.com/gpu=0 -v $C:/hf:z --entrypoint sh localhost/qwen38-rtx:local -c 'export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras; export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:$LD_LIBRARY_PATH; exec python3 -m exllamav3.conversion.convert_model -i /hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540 -o /hf/gg-ref3-build/out -w /hf/gg-ref3-build/work -rcp /hf/req6-build/recipe-uniform3.yaml -b 3 -hb 16 -mb 6 -vb 16 -cd /hf/calibration/agentic-quant-panel-v1/panel-qwen-packed-2048.safetensors -cr 1426 -cc 2048 -cb mcg -d 0'
EOF
say "gg-ref3 launched"
wait_done gg-ref3 || exit 1
ssh $OPT rtx@gg 'podman start qwen38-exl3-ple8' >>"$LOG" 2>&1 && say "gg engine restored" || say "WARN gg engine restore failed"
say "watch end: chain complete"

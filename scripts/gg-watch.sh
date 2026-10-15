#!/usr/bin/env bash
# Completion watcher v3 (no engine serving; GPUs fully assigned to quantization).
# gg-ref5 done -> launch gg-ref3 on gg. burst req4 done -> req3 via scripts/req3-run.sh. PCIe replay telemetry per tick (GPU0 first).
set -u
LOG=~/.cache/huggingface/gg-watch.log
OPT="-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30"
cd "$HOME/sm12x-exl3-qwen3.8-flash-next" || exit 1
say(){ echo "[$(date +%m-%d_%H:%M)] $*" >>"$LOG"; }
replays(){ timeout 20 nvidia-smi -q 2>/dev/null | grep -aA0 "Replays Since Reset" | awk -F: '{gsub(/ /,"",$2); printf "%s ",$2}'; echo; }
tick(){ [[ $(( $1 % 6 )) -eq 0 ]] && say "tick($2) replays:$(replays)"; }
say "watch v3 start"
for i in $(seq 1 400); do ssh $OPT rtx@gg "podman logs gg-ref5 2>&1 | tail -4" 2>/dev/null | grep -qa "Finished compiling" && { say "gg-ref5 finished"; break; }; tick $i gg; sleep 30; done
ssh $OPT rtx@gg bash -s <<'EOF' >>"$LOG" 2>&1
C=~/.cache/huggingface
mkdir -p $C/gg-ref3-build/out $C/gg-ref3-build/work
podman rm -f gg-ref3 >/dev/null 2>&1
podman run -d --name gg-ref3 --device nvidia.com/gpu=0 -v $C:/hf:z --entrypoint sh localhost/qwen38-rtx:local -c 'export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras; export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:$LD_LIBRARY_PATH; exec python3 -m exllamav3.conversion.convert_model -i /hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540 -o /hf/gg-ref3-build/out -w /hf/gg-ref3-build/work -rcp /hf/req6-build/recipe-uniform3.yaml -b 3 -hb 16 -mb 6 -vb 16 -cd /hf/calibration/agentic-quant-panel-v1/panel-qwen-packed-2048.safetensors -cr 1426 -cc 2048 -cb mcg -d 0'
EOF
say "gg-ref3 launched on gg"
for i in $(seq 1 400); do docker logs req4-convert 2>&1 | grep -qa "Finished compiling" && { say "req4 finished"; break; }; tick $i burst; sleep 30; done
bash scripts/req3-run.sh >>"$LOG" 2>&1 || say "WARN req3 launch"; say "req3 launched on burst"
for i in $(seq 1 400); do docker logs req3-convert 2>&1 | grep -qa "Finished compiling" && { say "req3 finished"; break; }; tick $i req3; sleep 30; done
say "watch end: all reference quants done"

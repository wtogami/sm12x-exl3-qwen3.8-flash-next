#!/usr/bin/env bash
# gg lane v2: --entrypoint sh everywhere (image ENTRYPOINT=vllm serve otherwise eats our cmds).
set -u
OPT="-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30"
LOG=/tmp/opencode/gg-lane2.log
say(){ echo "[$(date +%H:%M:%S)] $*" >>"$LOG"; }
say "v2 start"
rsync -aL --partial /home/opencode/.cache/huggingface/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540/ rtx@gg:.cache/huggingface/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540/ >>"$LOG" 2>&1 && say "source sync ok"
ssh $OPT rtx@gg 'ls ~/.cache/huggingface/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540 | wc -l' >>"$LOG" 2>&1
ssh $OPT rtx@gg bash -s <<'EOF' >>"$LOG" 2>&1
C=~/.cache/huggingface
mkdir -p $C/gg-smoke-build/out $C/gg-smoke-build/work $C/gg-ref4-build/out $C/gg-ref4-build/work
cp $C/req6-build/keep/ngram_embedding.K6-mcg.safetensors $C/gg-smoke-build/out/ngram_embedding.safetensors 2>/dev/null
podman rm -f gg-smoke >/dev/null 2>&1
podman run -d --name gg-smoke --device nvidia.com/gpu=0 -v $C:/hf:z --entrypoint sh localhost/qwen38-rtx:local -c '
export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras
export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:$LD_LIBRARY_PATH
exec python3 -m exllamav3.conversion.convert_model \
  -i /hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540 \
  -o /hf/gg-smoke-build/out -w /hf/gg-smoke-build/work \
  -rcp /hf/req6-build/recipe-uniform6.yaml -b 6 -hb 16 -mb 6 -vb 16 \
  -cd /hf/calibration/agentic-quant-panel-v1/panel-qwen-packed-2048.safetensors \
  -cr 1426 -cc 2048 -cb mcg -d 0'
EOF
say "smoke launched"
for i in $(seq 1 70); do ssh $OPT rtx@gg 'podman logs gg-smoke 2>&1 | grep -a "Quantized: model.language_model.layers.0 "' >>"$LOG" 2>&1 && break; sleep 15; done
ssh $OPT rtx@gg 'podman stop gg-smoke' >>"$LOG" 2>&1
scp -q $OPT rtx@gg:.cache/huggingface/gg-smoke-build/work/qtensors/model.language_model.layers.0.safetensors /tmp/opencode/gg-l0.safetensors && say "qtensor pulled: $(ls -l /tmp/opencode/gg-l0.safetensors | awk "{print \$5}") bytes"
podman run --rm -i -v /home/opencode/.cache/huggingface:/hf:z -v /tmp/opencode:/t:z --entrypoint python3 localhost/qwen38-rtx:local - <<'PY' >>"$LOG" 2>&1
import hashlib, json
from safetensors.torch import load_file
a = load_file("/hf/req6b-build/work/qtensors/model.language_model.layers.0.safetensors")
b = load_file("/t/gg-l0.safetensors")
ha = hashlib.sha256(open("/hf/req6b-build/work/qtensors/model.language_model.layers.0.safetensors","rb").read()).hexdigest()
hb = hashlib.sha256(open("/t/gg-l0.safetensors","rb").read()).hexdigest()
ks = [k for k in b if k in a][:8]
rel = sum(((a[k].float()-b[k].float()).norm()/a[k].float().norm()).item() for k in ks)/max(len(ks),1)
print(json.dumps({"bit_identical": ha==hb, "keys_a": len(a), "keys_b": len(b), "mean_rel_rmse_8t": round(rel,6)}))
PY
say "compare recorded"
ssh $OPT rtx@gg bash -s <<'EOF' >>"$LOG" 2>&1
C=~/.cache/huggingface
cp $C/req6-build/keep/ngram_embedding.K6-mcg.safetensors $C/gg-ref4-build/out/ngram_embedding.safetensors 2>/dev/null
podman rm -f gg-ref4 >/dev/null 2>&1
podman run -d --name gg-ref4 --device nvidia.com/gpu=0 -v $C:/hf:z --entrypoint sh localhost/qwen38-rtx:local -c '
export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras
export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:$LD_LIBRARY_PATH
exec python3 -m exllamav3.conversion.convert_model \
  -i /hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540 \
  -o /hf/gg-ref4-build/out -w /hf/gg-ref4-build/work \
  -rcp /hf/req6-build/recipe-uniform4.yaml -b 4 -hb 16 -mb 6 -vb 16 \
  -cd /hf/calibration/agentic-quant-panel-v1/panel-qwen-packed-2048.safetensors \
  -cr 1426 -cc 2048 -cb mcg -d 0'
EOF
say "gg-ref4 launched"
DONE=0
for i in $(seq 1 1500); do ssh $OPT rtx@gg 'podman logs gg-ref4 2>&1 | tail -3' | grep -a "Finished compiling" >>"$LOG" 2>&1 && { DONE=1; break; }; sleep 30; done
say "ref4 wait done=$DONE"
ssh $OPT rtx@gg 'podman start qwen38-exl3-ple8' >>"$LOG" 2>&1 && say "gg engine restored" || say "WARN gg restore failed"
say "v2 end"

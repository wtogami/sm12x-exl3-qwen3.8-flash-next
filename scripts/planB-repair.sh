#!/usr/bin/env bash
# waits for req<rate>v conversion, then repair chain + structural verify.
set -u
RATE=${1:?}; DIR="req${RATE}v-build"; NAME="req${RATE}v-convert"
CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
SRC=/nvme1/src/qwen38-bf16   # ngram re-injection reads source: use isolated disk
echo "[$(date +%m-%d_%H:%M)] repair watcher start for $NAME"
for i in $(seq 1 600); do
  podman logs "$NAME" 2>&1 | grep -qa "Finished compiling" && break
  sleep 30
done
podman logs "$NAME" 2>&1 | grep -qa "Finished compiling" || { echo "FAIL: no completion marker in 5h"; exit 1; }
echo "[$(date +%m-%d_%H:%M)] compiled; repair chain"
podman run --rm -v "$CACHE:/hf:z" -v /nvme1:/nvme1:z -v "$R/scripts:/s:z" --entrypoint sh localhost/qwen38-rtx:local -c "
export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras
export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:\$LD_LIBRARY_PATH
set -e
python3 /s/req6-ngram-reinject.py $SRC /hf/$DIR/out | tail -1
python3 /s/req6-mtp-repair.py /hf/$DIR/out | tail -1
python3 /s/req6-ledger-filter.py /hf/$DIR/out/quantization_config.json
python3 /s/req6-strip-ghost-keys.py /hf/$DIR/out | tail -1
python3 - <<'PY'
import json
idx = json.load(open('/hf/$DIR/out/model.safetensors.index.json'))['weight_map']
ng = sum(1 for k in idx if 'ngram' in k); mtp = sum(1 for k in idx if k.startswith('mtp.'))
assert len(idx) > 300000 and ng == 130 and mtp == 6173, (len(idx), ng, mtp)
print(f'structural verify PASS: index={len(idx)} ngram={ng} mtp={mtp}')
PY
" && touch "$CACHE/$DIR/REPAIR-OK" && echo "[$(date +%m-%d_%H:%M)] $DIR REPAIR-OK" \
  || echo "[$(date +%m-%d_%H:%M)] $DIR repair FAIL"

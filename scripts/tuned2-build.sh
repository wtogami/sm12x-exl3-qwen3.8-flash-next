#!/usr/bin/env bash
# RECIPE-BASED tuned@5.65 build (plan A/B, post pivot from broken measure/optimize assembly).
# usage: tuned2-build.sh panel|vanilla   (panel = -cd corpus; vanilla = K6V-mirror defaults)
# work/ on /nvme1 (isolated), out/ on root cache (dedup+serve target). GPU arg fixed below.
set -euo pipefail
CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"; R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
CORPUS=${1:?panel|vanilla}
case $CORPUS in
  panel)   DIR=tuned565-panel-build;   GPU=0; CAL=(-cd /hf/calibration/agentic-quant-panel-v1/panel-qwen-packed-2048.safetensors -cr 1426 -cc 2048);;
  vanilla) DIR=tuned565-vanilla-build; GPU=1; CAL=();;  # bundled corpus, defaults 250x2048 (K6V protocol)
esac
NAME="${DIR}-convert"; SRC=/nvme1/src/qwen38-bf16
[[ -d /nvme1/src/qwen38-bf16 ]] || { echo "FATAL: no source on /nvme1" >&2; exit 1; }
[[ -f /nvme1/sc/recipe-tuned565.yaml ]] || { echo "FATAL: no recipe" >&2; exit 1; }
mkdir -p "$CACHE/$DIR/out" "/nvme1/$DIR/work"
RESUME=(); [[ -f "/nvme1/$DIR/work/ckpt/job.json" ]] && RESUME=(-r)
podman rm -f "$NAME" 2>/dev/null || true
podman run -d --name "$NAME" --device "nvidia.com/gpu=$GPU" \
  -v "$CACHE:/hf:z" -v /nvme1:/nvme1:z --entrypoint sh localhost/qwen38-rtx:local -c "
export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras
export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:\$LD_LIBRARY_PATH
exec python3 -m exllamav3.conversion.convert_model \
  -i $SRC -o /hf/$DIR/out -w /nvme1/$DIR/work \
  -rcp /nvme1/sc/recipe-tuned565.yaml -b 5.65 -hb 16 -mb 6 -vb 16 \
  ${CAL[*]:-} -cb mcg -d 0 ${RESUME[*]:-}"
echo "$NAME launched on GPU$GPU ($CORPUS cal), resume=${RESUME[*]:-fresh}"
setsid nohup bash -c '
  set -u; DIR='"$DIR"'; CACHE="'"$CACHE"'"
  LOG="$CACHE/planB-$DIR.log"; say(){ echo "[$(date +%m-%d_%H:%M)] $*" >>"$LOG"; }
  say "watcher start"
  for i in $(seq 1 600); do podman logs "$DIR-convert" 2>&1 | grep -qa "Finished compiling" && break; sleep 30; done
  podman logs "$DIR-convert" 2>&1 | grep -qa "Finished compiling" || { say "FAIL: no completion marker"; exit 1; }
  say "compiled; repair chain"
  podman run --rm --device nvidia.com/gpu='"$GPU"' -v "$CACHE:/hf:z" -v /nvme1:/nvme1:z -v /home/opencode/sm12x-exl3-qwen3.8-flash-next/scripts:/s:z --entrypoint sh localhost/qwen38-rtx:local -c "
export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras
export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:\$LD_LIBRARY_PATH
set -e
python3 /s/req6-ngram-reinject.py /nvme1/src/qwen38-bf16 /hf/$DIR/out | tail -1
python3 /s/req6-mtp-repair.py /hf/$DIR/out | tail -1
python3 /s/req6-ledger-filter.py /hf/$DIR/out/quantization_config.json
python3 /s/req6-strip-ghost-keys.py /hf/$DIR/out | tail -1
python3 - <<PY
import json, collections
idx = json.load(open(\"/hf/$DIR/out/model.safetensors.index.json\"))[\"weight_map\"]
ts = json.load(open(\"/hf/$DIR/out/quantization_config.json\"))[\"tensor_storage\"]
ng = sum(1 for k in idx if \"ngram\" in k); mtp = sum(1 for k in idx if k.startswith(\"mtp.\"))
hist = collections.Counter(v[\"bits_per_weight\"] for v in ts.values() if \"bits_per_weight\" in v)
avg = sum(b*n for b,n in hist.items())/sum(hist.values())
print(f\"verify: index={len(idx)} ngram={ng} mtp={mtp} hist={dict(sorted(hist.items()))} avg={avg:.3f}\")
assert len(idx) > 300000 and ng == 130 and mtp == 6173
assert hist[6] > 40000 and hist[5] > 20000, \"expected 6/5 expert bands\"
print(\"VERIFY PASS\")
PY
" && touch "$CACHE/$DIR/REPAIR-OK" && say "REPAIR-OK" || say "repair FAIL"
' >/dev/null 2>&1 &
echo "repair watcher armed (log: $CACHE/planB-$DIR.log)"

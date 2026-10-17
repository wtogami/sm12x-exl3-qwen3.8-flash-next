#!/usr/bin/env bash
# measured@4.5868 card: converged knapsack optimum (budgets >= 5.0 all land here).
# Same proven lane as build-measured425; GPU1 so it can build in parallel.
set -euo pipefail
CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"; R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
DIR=measured459-build; GPU=1
NAME="${DIR}-convert"; SRC=/nvme1/src/qwen38-bf16
RECIPE=/nvme1/sc/recipe-measured459.yaml
[[ -d /nvme1/src/qwen38-bf16 ]] || { echo "FATAL: no source on /nvme1" >&2; exit 1; }
[[ -f $RECIPE ]] || { echo "FATAL: no recipe ($RECIPE)" >&2; exit 1; }
mkdir -p "$CACHE/$DIR/out" "/nvme1/$DIR/work"
RESUME=(); [[ -f "/nvme1/$DIR/work/ckpt/job.json" ]] && RESUME=(-r)
podman rm -f "$NAME" 2>/dev/null || true
podman run -d --name "$NAME" --device "nvidia.com/gpu=$GPU" \
  -v "$CACHE:/hf:z" -v /nvme1:/nvme1:z --entrypoint sh localhost/qwen38-rtx:local -c "
export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras
export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:\$LD_LIBRARY_PATH
exec python3 -m exllamav3.conversion.convert_model \
  -i $SRC -o /hf/$DIR/out -w /nvme1/$DIR/work \
  -rcp $RECIPE -b 4.59 -hb 16 -mb 6 -vb 16 \
  -cd /hf/calibration/agentic-quant-panel-v1/panel-qwen-packed-2048.safetensors -cr 1426 -cc 2048 \
  -cb mcg -d 0 ${RESUME[*]:-}"
echo "$NAME launched on GPU$GPU"
setsid nohup bash -c '
  set -u; DIR='"$DIR"'; CACHE="'"$CACHE"'"
  LOG="$CACHE/planB-$DIR.log"; say(){ echo "[$(date +%m-%d_%H:%M)] $*" >>"$LOG"; }
  say "watcher start"
  for i in $(seq 1 900); do
    podman logs "$DIR-convert" 2>&1 | grep -qa "Finished compiling" && break
    st=$(podman inspect -f '{{.State.Status}}' "$DIR-convert" 2>/dev/null || echo gone)
    [[ $st == exited || $st == dead || $st == gone ]] && { say "FAIL: convert exited without marker"; podman logs "$DIR-convert" 2>&1 | tail -20 >> "$LOG"; exit 1; }
    sleep 30
  done
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
exp = {k: v[\"bits_per_weight\"] for k, v in ts.items() if \"bits_per_weight\" in v and \".experts.\" in k and \"shared\" not in k and not k.startswith(\"mtp.\")}
avg = sum(exp.values()) / len(exp)
print(f\"verify: index={len(idx)} ngram={ng} mtp={mtp} expert-avg-bw={avg:.3f} n_expert={len(exp)}\")
assert len(idx) > 300000 and ng == 130 and mtp == 6173
assert 4.15 < avg < 4.35, f\"expert rate mean {avg} off target 4.25\"
print(\"VERIFY PASS\")
PY
" && touch "$CACHE/$DIR/REPAIR-OK" && say "REPAIR-OK" || say "repair FAIL"
' >/dev/null 2>&1 &
echo "repair watcher armed (log: $CACHE/planB-$DIR.log)"

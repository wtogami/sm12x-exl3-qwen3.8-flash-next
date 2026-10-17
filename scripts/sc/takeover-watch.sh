#!/usr/bin/env bash
# Deadline-takeover for the two build watchers (audit follow-up 08:35):
# If a watcher's 900x30s poll expires while its convert is still healthy (ETA drift
# made both deadlines marginal), take over: wait for "Finished compiling", then run
# the identical repair chain + REPAIR-OK. Triple-guarded against racing the original.
set -u
CACHE=$HOME/.cache/huggingface; LOG=$CACHE/sc-measure/build.log
say(){ echo "[$(date +%m-%d_%H:%M)] TKO: $*" >>"$LOG"; }
takeover(){ # dir gpu bplow bphi
  local DIR=$1 GPU=$2 LO=$3 HI=$4
  local W="$CACHE/planB-$DIR.log"
  say "takeover watching $DIR (after its watcher FAILs)"
  for i in $(seq 1 1200); do
    if grep -qa "REPAIR-OK" "$W" 2>/dev/null; then say "$DIR: original watcher won the race"; return 0; fi
    grep -qaE "FAIL: (no completion|convert exited)" "$W" 2>/dev/null && break
    sleep 30
  done
  grep -qaE "FAIL: (no completion|convert exited)" "$W" 2>/dev/null || { say "WARN $DIR: watcher failed early; deferring to manual"; return 1; }
  podman ps --format '{{.Names}}' | grep -qa "$DIR-convert" || { say "$DIR: convert gone, cannot take over"; return 1; }
  say "$DIR: original watcher expired, convert alive -> waiting for compile marker"
  for i in $(seq 1 480); do podman logs "$DIR-convert" 2>&1 | grep -qa "Finished compiling" && break; sleep 30; done
  podman logs "$DIR-convert" 2>&1 | grep -qa "Finished compiling" || { say "FAIL $DIR: no compile marker in 4h"; return 1; }
  grep -qa "compiled; repair chain" "$W" && { say "$DIR: original watcher is running the chain, standing down"; return 0; }
  say "$DIR: running takeover repair chain"
  podman run --rm --device "nvidia.com/gpu=$GPU" -v "$CACHE:/hf:z" -v /nvme1:/nvme1:z -v /home/opencode/sm12x-exl3-qwen3.8-flash-next/scripts:/s:z --entrypoint sh localhost/qwen38-rtx:local -c "
export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras
export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:\$LD_LIBRARY_PATH
set -e
python3 /s/req6-ngram-reinject.py /nvme1/src/qwen38-bf16 /hf/$DIR/out | tail -1
python3 /s/req6-mtp-repair.py /hf/$DIR/out | tail -1
python3 /s/req6-ledger-filter.py /hf/$DIR/out/quantization_config.json
python3 /s/req6-strip-ghost-keys.py /hf/$DIR/out | tail -1
python3 - <<PY
import json
idx = json.load(open(\"/hf/$DIR/out/model.safetensors.index.json\"))[\"weight_map\"]
ts = json.load(open(\"/hf/$DIR/out/quantization_config.json\"))[\"tensor_storage\"]
ng = sum(1 for k in idx if \"ngram\" in k); mtp = sum(1 for k in idx if k.startswith(\"mtp.\"))
exp = {k: v[\"bits_per_weight\"] for k, v in ts.items() if \"bits_per_weight\" in v and \".experts.\" in k and \"shared\" not in k and not k.startswith(\"mtp.\")}
avg = sum(exp.values()) / len(exp)
print(f\"verify: index={len(idx)} ngram={ng} mtp={mtp} expert-avg-bw={avg:.3f}\")
assert len(idx) > 300000 and ng == 130 and mtp == 6173
assert $LO < avg < $HI, f\"expert mean {avg} outside [$LO,$HI]\"
print(\"VERIFY PASS\")
PY
" && touch "$CACHE/$DIR/REPAIR-OK" && say "$DIR: takeover REPAIR-OK set (runbook pollers will proceed)" || say "FAIL $DIR: takeover repair chain failed"
}
takeover measured425-build 0 4.15 4.35
takeover measured459-build 1 4.45 4.72
say "takeover watch done"

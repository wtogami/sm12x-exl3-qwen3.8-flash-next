#!/usr/bin/env bash
# TUNED BUILD (plan A): assemble tuned@5.65 from the existing card pool.
# AUDIT FACTS: optimize_model.py performs NO calibration (corpus enters only via the
# candidate cards) and assembles from -i: [0]=base card, [1:]=alts in measurement
# order. Order MUST be ascending bitrate in both calls. VERIFY gates each phase.
set -euo pipefail
CACHE=${HF_CACHE:-$HOME/.cache/huggingface}; R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
SRC=/hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540
# ascending bitrate alts: K3, K4, K5, K6(panel), K6V(vanilla)
ALTS="/hf/req3-build/out /hf/req4-build/out /hf/req5-build/out /hf/req6b-build/out /hf/req6-build/out"
ENVP='export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras; export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:$LD_LIBRARY_PATH;'
mkdir -p "$CACHE/tuned-build"
if [[ ! -f "$CACHE/tuned-build/measurement.json" ]]; then
  echo "phase1: measure (KLD vs BF16 ref, per tensor-group)..."
  docker run --rm --device nvidia.com/gpu=0 --device nvidia.com/gpu=1 -v "$CACHE:/hf:z" --entrypoint sh localhost/qwen38-rtx:local \
    -c "$ENVP python3 -m exllamav3.conversion.measure_model -i $ALTS -r $SRC -l 2 -o /hf/tuned-build/measurement.json"
fi
python3 - "$CACHE/tuned-build/measurement.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
alts = [a["dir"] for a in m["alts"]]
order = ["req3","req4","req5","req6b","req6"]
assert len(alts) == 5, alts
for o, a in zip(order, alts):
    tail = a.rstrip("/").split("/")[-2] if "/out" in a else a
    assert o.rstrip("0123456789b") in tail or o in a, f"order mismatch: expected {o} in {alts}"
print("measurement OK: groups=%d cand_counts=%s alts=%s" % (len(m["groups"]), sorted({len(g["candidates"]) for g in m["groups"]}), alts))
PY
echo "phase2: optimize to 5.65 (base=K3 floor, alts ascending, SAME order as measure)..."
docker run --rm --device nvidia.com/gpu=0 --device nvidia.com/gpu=1 -v "$CACHE:/hf:z" -v "$R:/s:z" --entrypoint sh localhost/qwen38-rtx:local \
  -c "$ENVP python3 -m exllamav3.conversion.optimize_model -m /hf/tuned-build/measurement.json -b 5.65 -i /hf/req3-build/out $ALTS -o /hf/tuned-build/out"
echo "phase3: verify (index/ledger/ngram presence)..."
docker run --rm -v "$CACHE:/hf:z" --entrypoint python3 localhost/qwen38-rtx:local -c '
import json
idx = json.load(open("/hf/tuned-build/out/model.safetensors.index.json"))["weight_map"]
cfg = json.load(open("/hf/tuned-build/out/quantization_config.json"))
ts = cfg["tensor_storage"]
exp = sum(1 for k in ts if ".mlp.experts." in k and any(s in k for s in ("gate_proj","up_proj","down_proj")))
ng = sum(1 for k in idx if "ngram" in k)
print("index tensors:", len(idx), "| expert ledger:", exp, "(ref 75264) | ngram keys:", ng, "(ref 130)")
assert len(idx) > 1000 and exp > 70000 and ng >= 130
print("VERIFY PASS")
'
echo "TUNED CARD READY: $CACHE/tuned-build/out -> next: snapshot symlink tuned-565 + boot probe + v2 suite"

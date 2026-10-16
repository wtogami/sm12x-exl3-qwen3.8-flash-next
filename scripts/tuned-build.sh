#!/usr/bin/env bash
# PLAN A: tuned@5.65 assembled from the PURE PANEL pool {K3,K4,K5,K6}.
# CONTRACT (from conversion/{measure,optimize}_model.py source, 2026-10-16 audit):
#  - measure_model -i list: FIRST entry = BASE card (meas.base.dir), rest = alts in
#    ascending bitrate; cand[j] corresponds to alts[j] = in_dir[j+1]. >=2 dirs required.
#  - optimize_model WITHOUT -i inherits base+alt dirs positionally FROM measurement.json
#    -> cannot desync. Never pass -i unless it exactly repeats measure's list.
#  - optimize does NO calibration/requantization: it assembles existing qtensors
#    (VariantSafetensorsCollection) + recompiles. Corpus enters only via candidate cards.
# PLAN B pool (pure vanilla): K3V K4V K5V + req6(=K6V)  [needs 3 conversions first].
# PLAN C pool (mixed, all integer cards): req3 req4 req5 req6b + K3V K4V K5V req6.
# 4.25bpw vendor card EXCLUDED by owner decision (unreproducible provenance; mean-KLD
# blind to its knife-edge defect class; confounds corpus readout). See decision doc.
set -euo pipefail
CACHE=${HF_CACHE:-$HOME/.cache/huggingface}; R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
SRC=/hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540
POOL="/hf/req3-build/out /hf/req4-build/out /hf/req5-build/out /hf/req6b-build/out"   # base=K3, ascending
ENVP='export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras; export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:$LD_LIBRARY_PATH;'
mkdir -p "$CACHE/tuned-build"
if [[ ! -f "$CACHE/tuned-build/measurement.json" ]]; then
  echo "phase1: measure (per-group KLD deltas vs BF16 ref; base=K3, 3 alts)..."
  docker run --rm --device nvidia.com/gpu=0 --device nvidia.com/gpu=1 -v "$CACHE:/hf:z" --entrypoint sh localhost/qwen38-rtx:local \
    -c "$ENVP python3 -m exllamav3.conversion.measure_model -i $POOL -r $SRC -l 2 -o /hf/tuned-build/measurement.json"
fi
python3 - "$CACHE/tuned-build/measurement.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
assert "req3-build" in m["base"]["dir"], m["base"]["dir"]
alts = [a["dir"] for a in m["alts"]]
expect = ["req4-build", "req5-build", "req6b-build"]
assert [e in a for e, a in zip(expect, alts)] == [True]*3 and len(alts) == 3, alts
counts = {len(g["candidates"]) for g in m["groups"]}
assert counts == {3}, counts
print(f"measurement OK: base={m['base']['dir']} bpw={m['base']['bpw']} alts={alts} groups={len(m['groups'])} base_kld={m['base_kld']:.5f}")
PY
echo "phase2: optimize to 5.65 (dirs inherited from measurement - no -i)..."
# GPU0 ONLY: K3V convert may still hold GPU1 (Plan B overlap added post-writing);
# optimize has no -d flag and defaults to cuda:0 -> never grant it more than it uses.
docker run --rm --device nvidia.com/gpu=0 -v "$CACHE:/hf:z" -v "$R:/s:z" --entrypoint sh localhost/qwen38-rtx:local \
  -c "$ENVP python3 -m exllamav3.conversion.optimize_model -m /hf/tuned-build/measurement.json -b 5.65 -o /hf/tuned-build/out"
echo "phase3: verify assembled card (structure + rate histogram from ledger)..."
docker run --rm -v "$CACHE:/hf:z" --entrypoint python3 localhost/qwen38-rtx:local -c '
import json, collections
idx = json.load(open("/hf/tuned-build/out/model.safetensors.index.json"))["weight_map"]
cfg = json.load(open("/hf/tuned-build/out/quantization_config.json"))
ts = cfg["tensor_storage"]
ng = sum(1 for k in idx if "ngram" in k); mtp = sum(1 for k in idx if k.startswith("mtp."))
hist = collections.Counter(v["bits_per_weight"] for v in ts.values() if "bits_per_weight" in v)
tot = sum(hist.values()); avg = sum(b*n for b,n in hist.items())/tot
print(f"index={len(idx)} (ref ~302615) ngram={ng} (ref 130) mtp={mtp} (ref 6173)")
print(f"ledger entries={tot} avg_bpw={avg:.3f} hist={dict(sorted(hist.items()))}")
assert len(idx) > 300000 and ng == 130 and mtp == 6173
assert set(hist) <= {3,4,5,6}, "rates outside candidate pool!"
assert len(hist) > 1, "assembly collapsed to single rate"
assert 5.4 < avg < 5.9, avg
print("VERIFY PASS")
'
echo "TUNED CARD READY: $CACHE/tuned-build/out -> snapshot symlink tuned-a565 + boot probe + v2 suite + dedup rerun"

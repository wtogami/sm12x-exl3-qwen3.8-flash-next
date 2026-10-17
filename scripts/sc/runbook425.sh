#!/usr/bin/env bash
# RUNBOOK (owner-approved 2026-10-17 02:30): measured@4.25 lane.
# emit recipe -> await measurement queue -> build -> snapshot -> qualify TP2 -> probe
# TP2+TP1 -> [gate: clean sheets both] -> 300W ladder -> report+receipts committed.
# Oracle gate was evaluated manually from analysis-panel-l3.json before arming (recorded
# in the report). Replays: log-only per owner ruling. Power: 450W work; 300W only in
# ladder phase via power-phase (trap restores).
set -u
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next; cd "$R"
CACHE=$HOME/.cache/huggingface; SM=$CACHE/sc-measure; LOG=$SM/build.log
BENCH=$R/benchmarks/measured425-20261017; SNAP=$CACHE/hub/models--wrldsuksgo2mars--Qwen3.8-Flash-Next-EXL3-K4.25-v1/snapshots
URL=http://127.0.0.1:8001
say(){ echo "[$(date +%m-%d_%H:%M)] RB4: $*" >>"$LOG"; }
replay(){ timeout 20 nvidia-smi -q 2>/dev/null | grep -a "Replays Since Reset" | awk -F: '{gsub(/ /,"",$2); printf "%s ",$2}'; }
commit(){ git add -A >/dev/null 2>&1 && git commit -q -m "$1" && git push -q gg quant-exp1 2>/dev/null; say "committed: $1"; }
mkdir -p "$BENCH"
ENVP='export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras; export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:;'
boot(){ # name tp util
  ./stop.sh >/dev/null 2>&1; sleep 8
  QUANT=exl3 TP_SIZE="$2" GPU_MEMORY_UTILIZATION="$3" REPLACE=1 MODEL_SNAPSHOT="$1" \
    EXTRA_MOUNTS="$CACHE/engine-overlay/exl3.py:/usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/quantization/exl3.py:ro,z" \
    ./start.sh >>"$LOG" 2>&1
  local i=0
  until curl -sf --max-time 5 "$URL/v1/models" >/dev/null; do sleep 10; i=$((i+1)); if [[ $i -gt 150 ]]; then return 1; fi; done
  return 0; }
probe(){ local out="$R/benchmarks/$1-probe-biomed"; rm -rf "$out"; mkdir -p "$out"
  python3 scripts/test-biomed-analysis.py --base-url "$URL" --model qwen38-exl3 --runs 20 \
    --output "$out/biomed20.jsonl" >>"$LOG" 2>&1 || say "WARN probe $1"; }
clean_sheet(){ # <probe-dir-name> -> 0 if every task majority-passes and none is 0/20
  python3 - "$R/benchmarks/$1-probe-biomed/biomed20.jsonl" <<'PY'
import json, sys, collections
c = collections.defaultdict(lambda: [0, 0])
for line in open(sys.argv[1]):
    r = json.loads(line)
    if "id" not in r: continue
    c[r["id"]][1] += 1; c[r["id"]][0] += 1 if r.get("pass") else 0
bad = {k: f"{p}/{n}" for k, (p, n) in c.items() if p == 0 or p * 2 < n}
sys.exit(0 if len(c) >= 13 and not bad else 1)
PY
}
# ---- step 1: recipe from the panel-l3 measurement (CPU; oracle pre-verified) ----
say "RB4 armed; replays: $(replay)"
docker run --rm -v "$CACHE:/hf:z" --entrypoint sh localhost/qwen38-rtx:local -c \
  "$ENVP python3 /hf/quant-emit.py /hf/sc-measure/measurement-panel-l3.json \
   /hf/req3-build/out/model.safetensors.index.json \
   /hf/sc-measure/recipe-measured425.yaml" >>"$LOG" 2>&1 && say "recipe emitted" || { say "FATAL recipe emit failed"; exit 1; }
cp "$SM/recipe-measured425.yaml" /nvme1/sc/ && gzip -c "$SM/recipe-measured425.yaml" > "$R/quant/recipe-measured425.yaml.gz" \
  && say "recipe staged (/nvme1/sc + repo quant/)" || say "WARN recipe staging"
# ---- step 2: await measurement queue completion (frees GPU, lands vanilla analyses) ----
for i in $(seq 1 480); do grep -qa "QM2: QUEUE COMPLETE" "$LOG" && break; sleep 60; done
grep -qa "QM2: QUEUE COMPLETE" "$LOG" && say "QM2 queue complete; proceeding" || say "WARN: QM2 not complete after 8h; proceeding anyway (build needs the GPU)"
# ---- step 3: build ----
bash scripts/build-measured425.sh >>"$LOG" 2>&1 && say "build launched" || { say "FATAL build launch"; exit 1; }
for i in $(seq 1 1440); do [[ -f "$CACHE/measured425-build/REPAIR-OK" ]] && break; sleep 60; done
if [[ ! -f "$CACHE/measured425-build/REPAIR-OK" ]]; then say "FATAL: build/repair did not complete in 24h"; exit 1; fi
say "REPAIR-OK observed"
ln -sfn ../../../measured425-build/out "$SNAP/measured425"
# ---- step 4: qualify ----
MODEL=qwen38-exl3 RESULT_DIR="benchmarks/measured425-quality-v2" BASE_URL=$URL bash scripts/quality-suite.sh >>"$LOG" 2>&1 && say "quality-suite done" || say "WARN quality-suite"
# (suite script may boot its own engine lanes; for probes boot explicitly TP2)
boot measured425 2 0.88 && probe measured425-probe20-tp2 && say "probe TP2 done" || say "FATAL TP2 boot"
./stop.sh >/dev/null 2>&1; sleep 8
boot measured425 1 0.90 && probe measured425-probe20-tp1 && say "probe TP1 done" || say "WARN TP1 boot/probe"
./stop.sh >/dev/null 2>&1; sleep 8
# ---- step 5: conditional 300W ladder ----
if clean_sheet measured425-probe20-tp2 && clean_sheet measured425-probe20-tp1; then
  say "clean sheets TP2+TP1 -> running 300W ladder"
  scripts/power-phase.sh set 300 >>"$LOG" 2>&1
  trap 'scripts/power-phase.sh restore >>"$LOG" 2>&1; say "power restored (trap)"' EXIT INT TERM
  boot measured425 2 0.88 || say "WARN ladder boot"
  B=$R/benchmarks/ladder-20261016
  sp=$(nvidia-smi --query-gpu=timestamp,index,power.draw,clocks.sm,utilization.gpu --format=csv,noheader -lms 500 > "$B/measured425-power.csv" 2>/dev/null & echo $!)
  python3 scripts/benchmark-decode.py --base-url "$URL/v1" --model qwen38-exl3 --profile exl3 --mtp-tokens 3 \
    --request-mode clients --output-tokens 256 --concurrency 1 2 4 8 16 --runs 3 --server-container qwen38-exl3 \
    --output "$B/measured425-decode.json" >>"$LOG" 2>&1 || say "WARN decode"
  python3 scripts/benchmark-prefill.py --base-url "$URL/v1" --model qwen38-exl3 --profile exl3 \
    --prompt-tokens 131072 261632 523264 --runs 3 --server-container qwen38-exl3 \
    --output "$B/measured425-prefill.json" >>"$LOG" 2>&1 || say "WARN prefill"
  python3 scripts/benchmark-context.py --base-url "$URL/v1" --model qwen38-exl3 \
    --depths 16384 65536 131072 261632 523264 --runs 2 --warmups 1 --output-tokens 256 \
    --output "$B/measured425-context.jsonl" >>"$LOG" 2>&1 || say "WARN context"
  kill "$sp" 2>/dev/null || true
  ./stop.sh >/dev/null 2>&1
  scripts/power-phase.sh restore >>"$LOG" 2>&1; trap - EXIT
  say "ladder done"
else
  say "DEFECT GATE FAILED (see probe dirs) -> NO ladder; morning review required"
fi
# ---- step 6: report ----
python3 - "$SM/analysis-panel-l3.json" "$SM/analysis-vanilla-l3.json" "$BENCH/REPORT.md" <<'PY'
import json, sys, os
a = json.load(open(sys.argv[1])); rows = ["# measured@425 runbook report (auto)", "",
 "oracle + frontier (panel-l3, base K3, alts K4/K5/K6, 5120 panel rows):", ""]
rows.append("| budget | achieved | pred_kld | rate hist (0-based alts) | top-rate groups |")
rows.append("|---|---|---|---|---|")
for b, d in a["budgets"].items():
    rows.append(f"| {b} | {d['achieved_bpw']:.4f} | {d['pred_kld']:.5f} | {d['rate_hist']} | {len(d['top_rate_layers'])} |")
rows += ["", "Oracle reading: budget 4.25 lands on the lattice (4.2465) with a mixed 4/5/6",
 "allocation; budgets >= 5.0 stall at the same 4.5868 solution because the upstream",
 "knapsack refuses candidates with non-positive marginal KLD — the measured frontier",
 "flattens by ~4.6 bits (behavioral saturation, as the gates predicted).", ""]
try:
    v = json.load(open(sys.argv[2]))
    rows += ["vanilla-l3 oracle check (same budgets):", ""]
    for b, d in v["budgets"].items():
        rows.append(f"- {b}: achieved {d['achieved_bpw']:.4f}, kld {d['pred_kld']:.5f}, hist {d['rate_hist']}")
except Exception: rows += ["(vanilla analysis unavailable)"]
open(sys.argv[3], "w").write("\n".join(rows) + "\n")
PY
say "REPORT.md written"; commit "measured425: recipe + qualification/probe/ladder receipts + report" ; say "RB4 COMPLETE; replays: $(replay)"

#!/usr/bin/env bash
# measured@459 lane (converged optimum 4.5868 bpw) + catch-up 425 suite. GPU1 build runs
# parallel to runbook425; ALL engine phases serialize on RB4 COMPLETE. Prefix RB5.
set -u
R=/home/opencode/sm12x-exl3-qwen3.8-flash-next; cd "$R"
CACHE=$HOME/.cache/huggingface; SM=$CACHE/sc-measure; LOG=$SM/build.log
URL=http://127.0.0.1:8001
SNAP=$CACHE/hub/models--wrldsuksgo2mars--Qwen3.8-Flash-Next-EXL3-K4.25-v1/snapshots
say(){ echo "[$(date +%m-%d_%H:%M)] RB5: $*" >>"$LOG"; }
commit(){ git add -A >/dev/null 2>&1 && git commit -q -m "$1" && git push -q gg quant-exp1 2>/dev/null; say "committed: $1"; }
clean_sheet(){ python3 - "$R/benchmarks/$1-probe-biomed/biomed20.jsonl" <<'PY'
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
boot(){ ./stop.sh >/dev/null 2>&1; sleep 8
  QUANT=exl3 TP_SIZE="$2" GPU_MEMORY_UTILIZATION="$3" REPLACE=1 MODEL_SNAPSHOT="$1" \
    EXTRA_MOUNTS="$CACHE/engine-overlay/exl3.py:/usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/quantization/exl3.py:ro,z" \
    ./start.sh >>"$LOG" 2>&1
  local i=0
  until curl -sf --max-time 5 "$URL/v1/models" >/dev/null; do sleep 10; i=$((i+1)); if [[ $i -gt 150 ]]; then return 1; fi; done
  return 0; }
probe(){ local out="$R/benchmarks/$1-probe-biomed"; rm -rf "$out"; mkdir -p "$out"
  python3 scripts/test-biomed-analysis.py --base-url "$URL" --model qwen38-exl3 --runs 20 \
    --output "$out/biomed20.jsonl" >>"$LOG" 2>&1 || say "WARN probe $1"; }
ENVP='export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras; export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:;'
# ---- phase 1 (now): emit converged recipe @ budget 6.0 + build on GPU1 ----
say "RB5 armed"
docker run --rm -v "$CACHE:/hf:z" --entrypoint sh localhost/qwen38-rtx:local -c \
  "$ENVP python3 /hf/quant-emit.py /hf/sc-measure/measurement-panel-l3.json /hf/req3-build/out/model.safetensors.index.json /hf/sc-measure/recipe-measured459.yaml 6.0" >>"$LOG" 2>&1 && say "recipe-459 emitted" || { say "FATAL recipe-459"; exit 1; }
cp "$SM/recipe-measured459.yaml" /nvme1/sc/ && gzip -c "$SM/recipe-measured459.yaml" > "$R/quant/recipe-measured459.yaml.gz" && say "recipe-459 staged"
bash scripts/build-measured459.sh >>"$LOG" 2>&1 && say "build-459 launched (GPU1)" || { say "FATAL build-459 launch"; exit 1; }
for i in $(seq 1 1440); do [[ -f "$CACHE/measured459-build/REPAIR-OK" ]] && break; sleep 60; done
[[ -f "$CACHE/measured459-build/REPAIR-OK" ]] && say "REPAIR-OK 459" || { say "FATAL: 459 build/repair incomplete"; exit 1; }
ln -sfn ../../../measured459-build/out "$SNAP/measured459"
# ---- phase 2: serialize on the engine: 425 suite catch-up, then 459 qualification ----
for i in $(seq 1 1440); do grep -qa "RB4 COMPLETE" "$LOG" && break; sleep 60; done
grep -qa "RB4 COMPLETE" "$LOG" || { say "WARN: RB4 not complete after 24h; proceeding"; }
say "RB4 done; 10-min settle for its final commits before touching the engine"; sleep 600
say "engine free -> 425 suite catch-up"
rm -rf benchmarks/measured425-quality-v2
boot measured425 2 0.88 && MODEL=qwen38-exl3 RESULT_DIR=benchmarks/measured425-quality-v2 BASE_URL=$URL bash scripts/quality-suite.sh >>"$LOG" 2>&1 \
  && say "425 suite done" || say "WARN 425 suite"
./stop.sh >/dev/null 2>&1; sleep 8
# ---- phase 3: 459 quality + probes + TP1 512K capacity probe ----
boot measured459 2 0.88 && MODEL=qwen38-exl3 RESULT_DIR=benchmarks/measured459-quality-v2 BASE_URL=$URL bash scripts/quality-suite.sh >>"$LOG" 2>&1 \
  && say "459 suite done" || say "WARN 459 suite"
probe measured459-probe20-tp2 && say "459 probe TP2 done"
./stop.sh >/dev/null 2>&1; sleep 8
if boot measured459 1 0.95; then
  say "459 TP1 up at util 0.95 -> capacity test incl. 512K"
  python3 scripts/benchmark-context.py --base-url "$URL/v1" --model qwen38-exl3 \
    --depths 16384 261632 523264 --runs 1 --warmups 0 --output-tokens 64 \
    --output "$R/benchmarks/measured459-tp1-context.jsonl" >>"$LOG" 2>&1 && say "459 TP1 512K context PASS" || say "459 TP1 context FAIL (see log tail)"
  probe measured459-probe20-tp1 && say "459 probe TP1 done"
else say "459 TP1 boot FAIL (weights exceed single-GPU envelope)"; fi
./stop.sh >/dev/null 2>&1; sleep 8
# ---- phase 4: gated ladder ----
if clean_sheet measured459-probe20-tp2 && clean_sheet measured459-probe20-tp1; then
  say "clean sheets -> 300W ladder for measured459"
  scripts/power-phase.sh set 300 >>"$LOG" 2>&1
  trap 'scripts/power-phase.sh restore >>"$LOG" 2>&1; say "power restored (trap)"' EXIT INT TERM
  boot measured459 2 0.88 || say "WARN ladder boot"
  B=$R/benchmarks/ladder-20261016
  sp=$(nvidia-smi --query-gpu=timestamp,index,power.draw,clocks.sm,utilization.gpu --format=csv,noheader -lms 500 > "$B/measured459-power.csv" 2>/dev/null & echo $!)
  python3 scripts/benchmark-decode.py --base-url "$URL/v1" --model qwen38-exl3 --profile exl3 --mtp-tokens 3 \
    --request-mode clients --output-tokens 256 --concurrency 1 2 4 8 16 --runs 3 --server-container qwen38-exl3 \
    --output "$B/measured459-decode.json" >>"$LOG" 2>&1 || say "WARN decode 459"
  python3 scripts/benchmark-prefill.py --base-url "$URL/v1" --model qwen38-exl3 --profile exl3 \
    --prompt-tokens 131072 261632 523264 --runs 3 --server-container qwen38-exl3 \
    --output "$B/measured459-prefill.json" >>"$LOG" 2>&1 || say "WARN prefill 459"
  python3 scripts/benchmark-context.py --base-url "$URL/v1" --model qwen38-exl3 \
    --depths 16384 65536 131072 261632 523264 --runs 2 --warmups 1 --output-tokens 256 \
    --output "$B/measured459-context.jsonl" >>"$LOG" 2>&1 || say "WARN context 459"
  kill "$sp" 2>/dev/null || true
  ./stop.sh >/dev/null 2>&1
  scripts/power-phase.sh restore >>"$LOG" 2>&1; trap - EXIT
  say "ladder-459 done"
else say "DEFECT GATE FAILED for 459 -> no ladder"; fi
commit "measured459 lane: recipe + build + 425 suite catch-up + qualification, TP1 capacity test, receipts"
say "RB5 COMPLETE"

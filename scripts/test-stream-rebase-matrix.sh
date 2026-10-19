#!/bin/bash
# Backend x graph-regime matrix for the PLE streaming work, burst layout (GPU=1, GPU 0 sampled and
# required to stay idle). Run: CELLS=<list> REF=<strict-hash> bash scripts/test-stream-rebase-matrix.sh
#
# The two pinned regime cells are the reason this driver exists. A streaming cell that disagrees with the
# pinned baseline is only evidence about streaming if the graph regime streaming forces has been shown
# output-neutral by itself -- otherwise the regime is an unmeasured second variable. On vLLM v0.32 that
# control was not a formality: pinned + VLLM_USE_BREAKABLE_CUDAGRAPH=1 + cudagraph_mode=PIECEWISE
# reproduces the streaming-v1 hash with no streaming involved, while pinned + PIECEWISE *without* that
# env reproduces the baseline.
# Test the rebased branch on burst, GPU=1 only. GPU 0 (bus 51:00.0) is sampled continuously and
# must never show usage.
set -uo pipefail
REPO=/home/opencode/sm12x-exl3-qwen3.8-flash-next
cd "$REPO" || exit 1
IMG=localhost/qwen38-rtx:exp2-stream
OUT=/home/opencode/exp2-test; mkdir -p "$OUT"
REF="${REF:-}"
GP0MAX=0

cleanup() {
  podman ps -aq --filter name=qwen38 | xargs -r podman rm -f >/dev/null 2>&1
  echo "  [cleanup] containers removed; GPU state:"
  nvidia-smi --query-gpu=index,memory.used --format=csv,noheader | sed 's/^/    /'
}
trap cleanup EXIT INT TERM

cell() {   # name  extra-env-string  [extra start.sh args]
  local name=$1 envs=$2 extra_args=${3:-}
  echo
  echo "=== cell $name ==="
  podman ps -aq --filter name=qwen38 | xargs -r podman rm -f >/dev/null 2>&1
  sleep 3
  # shellcheck disable=SC2086
  if ! env IMAGE=$IMG QUANT=exl3-ple8 GPU=1 $envs bash "$REPO/start.sh" $extra_args > "$OUT/$name-start.log" 2>&1; then
    echo "  start.sh FAILED:"; tail -6 "$OUT/$name-start.log" | sed 's/^/    /'; return 1
  fi
  local ctr=$(podman ps --format '{{.Names}}' | tail -1)
  echo "  container: $ctr"
  local code=000
  for i in $(seq 1 120); do
    code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' http://127.0.0.1:8001/health 2>/dev/null)
    [ "$code" = "200" ] && { echo "  HEALTHY after $((i*10))s"; break; }
    sleep 10
  done
  [ "$code" = "200" ] || { echo "  never healthy"; podman logs --tail 15 "$ctr" 2>&1 | sed 's/^/    /'; return 1; }
  podman logs "$ctr" > "$OUT/$name.log" 2>&1

  echo "  --- GPU isolation (the constraint under test) ---"
  podman exec "$ctr" nvidia-smi --query-gpu=index,pci.bus_id --format=csv,noheader | sed 's/^/    in-container: /'
  local n=$(podman exec "$ctr" nvidia-smi --query-gpu=index --format=csv,noheader | wc -l)
  local bus=$(podman exec "$ctr" nvidia-smi --query-gpu=pci.bus_id --format=csv,noheader | head -1)
  echo "    devices visible: $n (want 1);  bus: $bus (want 00000000:9C:00.0)"
  echo "  --- memory ---"
  awk '/^Shmem:/ {printf "    Shmem %.2f GiB\n", $2/1048576}' /proc/meminfo
  echo "  --- streaming / backend lines ---"
  grep -m1 -iE "streaming backend selected" "$OUT/$name.log" | cut -c1-120 | sed 's/^/    /'
  grep -m1 -iE "PLE stream v2: staging" "$OUT/$name.log" | cut -c1-150 | sed 's/^/    /'
  grep -m1 -iE "PLE pinned host memory|pinned at exact size" "$OUT/$name.log" | cut -c1-130 | sed 's/^/    /'
  grep -m1 -iE "cudagraph_mode" "$OUT/$name.log" | grep -oE "CUDAGraphMode\.[A-Z_]+" | head -1 | sed 's/^/    /'
  echo "    capture-copy errors: $(grep -ciE 'Cannot copy between CPU and CUDA' "$OUT/$name.log")"
  echo "    staging failures:    $(grep -ciE 'staging failed' "$OUT/$name.log")"
  echo "  --- greedy determinism, twice ---"
  local h1 h2
  local dump=""
  if [[ "${DUMP:-0}" == 1 ]]; then
    # Text as well as a hash: a hash says two regimes disagree, only the text says by how much, and
    # "differs" and "one early token cascaded" need separating before anyone calls either a bug.
    dump="--dump $OUT/dumps.jsonl --label $name"
  fi
  h1=$(cd /tmp && python3 -I "$REPO/scripts/ple-probe.py" --reps 2 --warmup 1 $dump 2>&1 | awk '$1=="strict"{print $2}')
  h2=$(cd /tmp && python3 -I "$REPO/scripts/ple-probe.py" --reps 2 --warmup 1 2>&1 | awk '$1=="strict"{print $2}')
  echo "    strict hashes: $h1 / $h2   $([ "$h1" = "$h2" ] && echo SELF-CONSISTENT || echo VARIES)"
  if [ -z "$REF" ]; then REF="$h1"; echo "    (reference set from this cell)"; else
    echo "    vs reference $REF: $([ "$h1" = "$REF" ] && echo IDENTICAL || echo DIFFERS)"
  fi
  ( cd /tmp && python3 -I "$REPO/scripts/ple-probe.py" --reps 3 --warmup 2 > "$OUT/$name-perf.txt" 2>&1 )
  grep -E "^(prefill|decode) " "$OUT/$name-perf.txt" | sed 's/^/    /'
  podman stop -t 30 "$ctr" >/dev/null 2>&1
  return 0
}

# Sample GPU 0 for the whole run. Without this the "GPU 0 idle" claim below would be an assertion
# about something never measured -- the peak is written to a file the final report reads.
: > "$OUT/gpu0.log"
( while :; do
    m=$(nvidia-smi --id=0 --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | tr -d ' ')
    p=$(nvidia-smi --id=0 --query-compute-apps=pid,used_memory --format=csv,noheader 2>/dev/null | head -1)
    echo "$(date +%H:%M:%S) gpu0_mib=${m:-?} procs=${p:-none}" >> "$OUT/gpu0.log"
    sleep 10
  done ) &
SAMPLER=$!
trap 'kill $SAMPLER 2>/dev/null; cleanup' EXIT INT TERM

echo "=== GPU 0 baseline: $(nvidia-smi --id=0 --query-gpu=memory.used --format=csv,noheader) ==="
run() { [[ -n "${CELLS:-}" && ",${CELLS}," != *",$1,"* ]] || cell "$@"; }
# Control cell: pinned backend, but PIECEWISE graphs forced the way v1 forces them. Without this, a
# v1 output difference cannot be attributed -- it could be the streaming gather or the graph mode.
run pinned-piecewise "PLE_PIN_STRATEGY=round2" "-cc.cudagraph_mode=PIECEWISE"
# Exact v1 graph regime (breakable env set early, as start.sh does it) on the PINNED backend. The
# piecewise-only control above omitted the env, and the eager_break_during_capture decorator decides at
# import time whether it can break -- so that cell measured a different regime (decode 20.8 tok/s vs
# v1's 108.7). This one isolates the backend, not the regime.
run pinned-v1regime "PLE_PIN_STRATEGY=round2 VLLM_USE_BREAKABLE_CUDAGRAPH=1" \
    "--compilation-config {\"cudagraph_mode\":\"PIECEWISE\"}"
run pinned-round2  ""
run pinned-exact   "PLE_PIN_STRATEGY=exact"
run stream-v1      "PLE_STREAM=1"
run stream-v2      "PLE_STREAM_V2=1"
echo
kill $SAMPLER 2>/dev/null
peak=$(awk -F= '/gpu0_mib/{if($2+0>max)max=$2+0} END{print max+0}' "$OUT/gpu0.log")
samples=$(wc -l < "$OUT/gpu0.log")
anyproc=$(grep -vc "procs=none" "$OUT/gpu0.log" || true)
echo "=== GPU 0 over $samples samples: peak ${peak} MiB, samples showing a compute process: $anyproc ==="
[ "${peak:-0}" -le 16 ] && [ "$anyproc" = 0 ] && echo "  PASS GPU 0 stayed idle" || { echo "  FAIL GPU 0 was used:"; grep -v "procs=none" "$OUT/gpu0.log" | head -5 | sed 's/^/    /'; }
nvidia-smi --query-gpu=index,memory.used --format=csv,noheader | sed 's/^/  /'
if [[ "${DUMP:-0}" == 1 && -n "${DIFF_A:-}" && -n "${DIFF_B:-}" ]]; then
  echo; echo "=== output distance: $DIFF_A vs $DIFF_B ==="
  ( cd /tmp && python3 -I "$REPO/scripts/ple-probe.py" --diff "$OUT/dumps.jsonl" "$DIFF_A" "$DIFF_B" ) 2>&1 | sed 's/^/  /'
fi
echo TESTDONE

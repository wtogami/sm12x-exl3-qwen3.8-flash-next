#!/usr/bin/env bash
# Boot each GPU_MEMORY_UTILIZATION level and run benchmark-deep-concurrency.py against
# it, so the ladder's arithmetic (pool tokens / request length) is checked against what
# the scheduler actually does with N simultaneous full-prefill requests.
#
# Same override surface as util-ladder.sh (QUANT/IMAGE/PORT/OUT/UTILS), and OUT should
# be the directory that ladder wrote, because the pool sizes quoted alongside these
# cells come from that results.tsv.
#
# Cells are skipped once recorded; a util with a partial jsonl is reported as EXISTS
# rather than silently re-run, because benchmark-deep-concurrency.py refuses to
# overwrite its own receipt.
set -uo pipefail
# Work from the repo this script sits in, not a hardcoded host path: the same
# driver now runs on gg and on burst, and a /home/rtx literal made it
# unusable anywhere else.
cd "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Fail fast if that cd did not land in a checkout. A copy of a sibling driver ran
# from outside the repo, found no start.sh, polled a dead port for 33 minutes, and its
# stop fallback SIGKILLed an engine that was loading weights on the same card.
[[ -f start.sh && -f stop.sh ]] || { echo "no start.sh/stop.sh above $(dirname -- "${BASH_SOURCE[0]}") -- run this file from <repo>/scripts/" >&2; exit 2; }
QUANT="${QUANT:-exl3}"
MODEL="${MODEL:-qwen38-$QUANT}"
NAME="${CONTAINER_NAME:-qwen38-$QUANT}"
PORT="${PORT:-8199}"
OUT="${OUT:-/home/rtx/util-ladder-exl3}"; mkdir -p "$OUT"
B=http://127.0.0.1:"$PORT"
IMG="${IMAGE:-qwen38-rtx:local}"
UTILS="${UTILS:-0.94 0.96 0.98}"
FRACTIONS="${FRACTIONS:-0.5 1}"
CONCURRENCY="${CONCURRENCY:-2 3 4}"
# Output length is the axis that decides whether this probe measures prefill throughput
# or KV residency. The pool can only bind if the sequences are still generating while
# their peers arrive, and at deep lengths the serialized prefill (~42 s per 256K, ~89 s
# per 512K at max_num_batched_tokens=2048) is so slow that a 128-token response finishes
# long before the next prefill lands -- measured, zero preemptions even 1.7x
# oversubscribed. Long outputs are what force simultaneous residency.
OUTPUT_TOKENS="${OUTPUT_TOKENS:-128}"
row="$OUT/deep-concurrency.tsv"
[[ -e "$row" ]] || printf "util\tstatus\tdepth_tokens\tconcurrency\trequests_ok\tpreemptions\tpeak_waiting\twall_s\tnote\n" > "$row"

# Check before the first boot, not after: the first run of this driver booted three
# engines at ~6 minutes each only to have every cell fail on a missing probe file,
# because the probe sits in the repo tree and the repo tree is not where the driver
# was copied to. The probe also has to sit beside benchmark-prefill.py and
# benchmark-context.py, since it imports both from its own directory.
probe="scripts/benchmark-deep-concurrency.py"
[[ -f "$probe" ]] || { echo "missing $probe (it must live in the repo tree beside its imports)" >&2; exit 1; }
python3 -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$probe" || { echo "$probe does not parse" >&2; exit 1; }

# Never `docker rm -f` a *running* container: SIGTERM, 10s, SIGKILL. Fine when a driver
# owns the machine; a misplaced copy of this family of drivers force-removed a user's
# engine at 86% through loading weights (exit 137, no kernel OOM, no Xid). Only stopped
# containers are removed, and a live one under our name that stop.sh could not stop is
# another tenant's work -- refuse the cell instead of ending it.
stop_engine() {
  QUANT=$QUANT IMAGE=$IMG PORT=$PORT bash stop.sh >>"$OUT/ops.log" 2>&1 || true
  local state
  state=$(docker inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null) || return 0
  if [[ "$state" == running || "$state" == created || "$state" == restarting ]]; then
    echo "STOP-REFUSED: container $NAME is still $state; refusing to force-remove it" >>"$OUT/ops.log"
    return 1
  fi
  docker rm "$NAME" >/dev/null 2>&1 || true
  sleep 5
}

for u in $UTILS; do
  grep -q "^$u\b" "$row" && continue
  jsonl="$OUT/deep-concurrency-$u.jsonl"
  if [[ -e "$jsonl" ]]; then
    printf "%s\tEXISTS\t\t\t\t\t\t\treceipt present, not overwritten\n" "$u" >> "$row"
    continue
  fi
  stop_engine
  QUANT=$QUANT IMAGE=$IMG GPU=${GPU:-0} PORT=$PORT GPU_MEMORY_UTILIZATION=$u bash start.sh >>"$OUT/ops.log" 2>&1 &
  ready=0
  for i in $(seq 1 100); do curl -sf $B/v1/models >/dev/null 2>&1 && { ready=1; break; }; sleep 10; done
  if [[ $ready != 1 ]]; then
    printf "%s\tBOOT-FAIL\t\t\t\t\t\t\t%s\n" "$u" "$(docker logs "$NAME" 2>&1 | grep -iE 'out of memory|CUDA error|Error' | tail -1 | cut -c1-80)" >> "$row"
    stop_engine; continue
  fi
  # Record the pool these cells were measured on. The probe measures behaviour and the
  # ladder records capacity, but a deep-concurrency run against a tree that has no
  # results.tsv row for this util would otherwise leave the eviction counts unexplained.
  docker logs "$NAME" 2>&1 \
    | grep -oE "GPU KV cache size: [0-9,]+ tokens, Maximum concurrency for [0-9,]+ tokens per request: [0-9.]+x" \
    | tail -1 > "$OUT/pool-$u.txt" || true
  if python3 scripts/benchmark-deep-concurrency.py --base-url $B/v1 --model "$MODEL" \
       --fractions $FRACTIONS --concurrency $CONCURRENCY --output-tokens "$OUTPUT_TOKENS" \
       --output "$jsonl" >>"$OUT/deep-$u.log" 2>&1; then
    status=OK
  else
    status=PROBE-FAIL
  fi
  python3 - "$u" "$status" "$jsonl" "$row" <<'PY'
import json, sys
util, status, jsonl, row = sys.argv[1:5]
try:
    lines = [json.loads(l) for l in open(jsonl) if l.strip()]
except FileNotFoundError:
    lines = []
measurements = [r for r in lines if r.get("record") == "measurement"]
with open(row, "a") as out:
    if not measurements:
        out.write(f"{util}\t{status}\t\t\t\t\t\t\tno measurement rows\n")
    for r in measurements:
        note = f"peak_cache={r['peak_gpu_cache_usage']}" if r.get("peak_gpu_cache_usage") is not None else ""
        if r.get("errors"):
            note = (note + " " + "; ".join(r["errors"]))[:120]
        out.write("\t".join(str(v) for v in (
            util, status, r["depth_tokens"], r["concurrency"],
            f"{r['requests_ok']}/{r['concurrency']}",
            r["preemptions"], r["peak_waiting"], r["wall_seconds"], note)) + "\n")
PY
  # Keep the engine's own log for the cell. The TSV carries the eviction count from the
  # /metrics counter; only the log carries WHICH allocator refused, and on a hybrid
  # model that is the difference between "util behaves non-monotonically" and a cause.
  docker logs --timestamps "$NAME" > "$OUT/engine-$u.log" 2>&1 || true
  stop_engine
done
echo "DEEP CONCURRENCY DONE $(date -u +%FT%TZ)" >> "$row"

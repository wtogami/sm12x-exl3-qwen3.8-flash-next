#!/usr/bin/env bash
# C1 A/B across GPU_MEMORY_UTILIZATION levels.
#
# Exists because the residency work concluded "0.98 costs nothing at C1" on the
# strength of a two-concurrent-sequence control cell. That was a labeling error, and
# the conclusion it supported is the one that decides the default, so this measures
# the single-stream legs directly: the prefill ladder, C1-only decode, and the
# seven/orchid timed suites, at each util, on the same boot.
#
# Same override surface as util-ladder.sh. OUT should be one directory per campaign;
# receipts are named per util inside it.
set -uo pipefail
cd "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Fail fast if that cd did not land in a checkout. This exact bug, in this exact
# script: run from a copy outside the repo, start.sh was never found, the driver
# polled a dead port for half an hour, and its stop fallback force-removed a
# container that was loading weights -- exit 137, no kernel OOM, no Xid.
[[ -f start.sh && -f stop.sh ]] || { echo "no start.sh/stop.sh above $(dirname -- "${BASH_SOURCE[0]}") -- run this file from <repo>/scripts/" >&2; exit 2; }
QUANT="${QUANT:-exl3}"
MODEL="${MODEL:-qwen38-$QUANT}"
NAME="${CONTAINER_NAME:-qwen38-$QUANT}"
PORT="${PORT:-8199}"
OUT="${OUT:-$PWD/c1-ab-exl3}"; mkdir -p "$OUT"
B=http://127.0.0.1:"$PORT"
IMG="${IMAGE:-qwen38-rtx:local}"
UTILS="${UTILS:-0.94 0.98}"
MTP="${MTP_TOKENS:-3}"
row="$OUT/c1-ab.tsv"
[[ -e "$row" ]] || printf "util\tlegs\tc1_decode_med\tprefill_128k_ttft_s/tps\tprefill_261632_ttft_s/tps\tseven_blend\torchid_med\tboot_free\tnote\n" > "$row"

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

# A live container under our name before we booted means another tenant (or a
# wedged driver) owns it. Polling its port would report *their* engine as our 0.94
# or 0.98 cell, and force-removing it is what killed gg's engine. Skip the cell.
prewarm() {
  local state
  state=$(docker inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null) || return 0
  if [[ "$state" == running || "$state" == created || "$state" == restarting ]]; then
    printf "%s\tSKIPPED-PREWARM\t\t\t\t\t\t\t\t%s is already $state on $PORT\n" "$1" "$NAME" >>"$row"
    return 1
  fi
  docker rm "$NAME" >/dev/null 2>&1 || true
}

for u in $UTILS; do
  grep -q "^$u\b" "$row" && continue
  prewarm "$u" || continue
  stop_engine
  QUANT=$QUANT IMAGE=$IMG GPU=${GPU:-0} PORT=$PORT GPU_MEMORY_UTILIZATION=$u bash start.sh >>"$OUT/ops.log" 2>&1 &
  ready=0
  for i in $(seq 1 100); do curl -sf $B/v1/models >/dev/null 2>&1 && { ready=1; break; }; sleep 10; done
  if [[ $ready != 1 ]]; then
    printf "%s\tBOOT-FAIL\t\t\t\t\t\t\t%s\n" "$u" "$(docker logs "$NAME" 2>&1 | grep -iE 'out of memory|CUDA error|Error' | tail -1 | cut -c1-80)" >> "$row"
    stop_engine; continue
  fi
  free=""
  for leg in prefill decode seven orchid; do
    case "$leg" in
      prefill) python3 scripts/benchmark-prefill.py --base-url $B/v1 --model "$MODEL" --profile "$QUANT" \
                 --prompt-tokens 2048 8192 32768 65536 128000 261632 --runs 3 --output "$OUT/prefill-$u.json" ;;
      decode)  python3 scripts/benchmark-decode.py --base-url $B/v1 --model "$MODEL" --profile "$QUANT" \
                 --mtp-tokens "$MTP" --concurrency 1 --request-mode clients --output-tokens 256 \
                 --warmup-runs 2 --runs 3 --output "$OUT/decode-c1-$u.json" ;;
      seven)   python3 scripts/benchmark-workloads.py --base-url $B --model "$MODEL" --suite seven --runs 3 \
                 --warmups 1 --collect-mtp-metrics --output "$OUT/seven-$u.jsonl" ;;
      orchid)  python3 scripts/benchmark-workloads.py --base-url $B --model "$MODEL" --suite orchid --runs 5 \
                 --warmups 1 --collect-mtp-metrics --output "$OUT/orchid-$u.jsonl" ;;
    esac >> "$OUT/legs-$u.log" 2>&1 || free="$free $leg:FAILED"
  done
  docker logs --timestamps "$NAME" > "$OUT/engine-$u.log" 2>&1 || true
  boot_free=$(grep -oE "Free memory on device \([0-9.]+/[0-9.]+ GiB\)" "$OUT/engine-$u.log" | tail -1)
  python3 - "$u" "${free:-ok}" "$OUT" "$row" "$boot_free" <<'PY'
import json, statistics as st, sys, pathlib
util, status, out, row, boot_free = sys.argv[1:6]
out = pathlib.Path(out)
def med(v): return round(v, 1) if isinstance(v, (int, float)) else ""
def timed(p):
    try:
        return [json.loads(l) for l in open(p) if l.strip() and json.loads(l).get("record") == "measurement"]
    except FileNotFoundError:
        return []
c1 = pre = sev = orc = ""
try:
    d = json.load(open(out / f"decode-c1-{util}.json"))
    p = [x for x in d["points"] if x["concurrency"] == 1]
    if p: c1 = med(p[0]["aggregate_decode_tokens_per_second"]["median"])
except Exception: pass
try:
    pts = {x["prompt_tokens"]: x for x in json.load(open(out / f"prefill-{util}.json"))["points"]}
    if 128000 in pts:
        pre = f"{pts[128000]['ttft_seconds']['median']:.2f} / {pts[128000]['effective_prompt_tokens_per_second']['median']:.0f}"
    if 261632 in pts:
        p261 = f"{pts[261632]['ttft_seconds']['median']:.2f} / {pts[261632]['effective_prompt_tokens_per_second']['median']:.0f}"
    else:
        p261 = ""
except Exception:
    p261 = ""
s = [r for r in timed(out / f"seven-{util}.jsonl") if r.get("timed")]
if s: sev = f"{sum(r['decode_tokens'] for r in s)/sum(r['decode_seconds'] for r in s):.1f}"
o = [r for r in timed(out / f"orchid-{util}.jsonl") if r.get("timed")]
if o: orc = med(st.median([r["decode_tps"] for r in o]))
open(row, "a").write("\t".join(map(str, [util, status, c1, pre, p261, sev, orc, boot_free, ""])) + "\n")
PY
  stop_engine
done
echo "C1 AB DONE $(date -u +%FT%TZ)" >> "$row"

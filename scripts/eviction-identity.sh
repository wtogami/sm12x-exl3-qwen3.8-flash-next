#!/usr/bin/env bash
# Is an evicted, re-prefilled sequence the same sequence?
#
# Why two engines. Measured on this stack (exl3, v0.32, FP8 KV, MTP3, temperature 0, fixed
# seed), the same prompt run alone five times is bit-identical with logprob drift exactly 0,
# but the same prompt run alongside one to sixteen other requests -- with NO eviction --
# produces identical tokens with 0.32-0.47 nats of per-token logprob drift. Logprobs here are
# not batch-invariant. So an evicted leg cannot be judged against a solo run: it has to be
# judged against the engine's ordinary run-to-run variance, which means booting the SAME
# utilization twice as a null control, and a third time at a utilization measured to evict.
#
# Legs default to 0.98a / 0.98b / 0.96 at 245,248 tokens x 4 with 16,384-token outputs, the
# cells docs/qualification.md measures as clean (0 evictions) and thrashing (4-11).
#
# Every failure mode this script has already lived through is guarded, and the guards are
# loud rather than fatal-by-silence:
#   * SMOKE both prompt modes at 2,048 tokens before booting any real leg -- a parse or
#     prompt-builder bug otherwise costs six minutes of engine loading to surface.
#   * the burst must be COLD: prefix hit ratio above --max-cache-hit-ratio invalidates the
#       leg, because a cache-warm burst does no prefill and cannot evict (this happened:
#       4 x 245K "completed" in 4.4 s with zero preemptions).
#   * the burst must be REAL: effective prefill tok/s is recorded and printed.
#   * the output must be COMPARABLE: empty token streams are refused, not scored as
#       agreement (this happened: an earlier version compared four empty lists and PASSED).
#   * the output must be HIGH-ENTROPY: fewer than --min-distinct-tokens distinct tokens means
#       the model is reciting the filler in a period-23 cycle, so agreement is nearly
#       guaranteed by construction (this happened too, on an otherwise perfect run).
#
# Env: QUANT IMAGE GPU PORT_BASE CLEANUP=0 SKIP_SMOKE=1 QUESTION=1 SMOKE_DEPTH DEPTH
#      CONCURRENCY OUTPUT_TOKENS UTILS ("tag=util" pairs) MTP_TOKENS
set -uo pipefail
cd "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f start.sh && -f stop.sh ]] || { echo "no start.sh/stop.sh above $(dirname -- "${BASH_SOURCE[0]}") -- run this file from <repo>/scripts/" >&2; exit 2; }

QUANT="${QUANT:-exl3}"; MODEL="${MODEL:-qwen38-$QUANT}"
IMG="${IMAGE:-qwen38-rtx:local}"; GPU="${GPU:-0}"; PORT_BASE="${PORT_BASE:-8891}"
OUT="${OUT:?set OUT to a per-campaign directory}"; mkdir -p "$OUT"
UTILS="${UTILS:-0.98a=0.98 0.98b=0.98 0.96=0.96}"
DEPTH="${DEPTH:-245248}"; N="${CONCURRENCY:-4}"; OT="${OUTPUT_TOKENS:-16384}"
MTP="${MTP_TOKENS:-3}"; CLEANUP="${CLEANUP:-1}"; SKIP_SMOKE="${SKIP_SMOKE:-0}"
QUESTION="${QUESTION:-1}"
ROWS="$OUT/verdict.tsv"
[[ -e "$ROWS" ]] || printf "leg\tpreemptions\twall_s\tprompts_sha\tburst_seq_sha\tcache_hit\tentropy\tnote\n" > "$ROWS"

boot() {  # $1 util, $2 name, $3 port, $4... extra start.sh env as VAR=VALUE
  local u="$1" name="$2" port="$3"; shift 3
  QUANT=$QUANT IMAGE=$IMG GPU=$GPU PORT=$port CONTAINER_NAME=$name \
    GPU_MEMORY_UTILIZATION=$u MTP_TOKENS=$MTP env "$@" bash start.sh >>"$OUT/ops.log" 2>&1 &
    # `env` is not redundant: bare "$@" lands in the command-name slot, so a caller's
    # LONGCTX=0 MAX_MODEL_LEN=16384 was parsed as a program to execute and the boot died
    # with "LONGCTX=0: command not found". Assignments passed as arguments need env(1).
  for _ in $(seq 1 100); do
    curl -sf "http://127.0.0.1:$port/v1/models" >/dev/null 2>&1 && return 0
    sleep 10
  done
  echo "boot failed at util=$u name=$name (see ops.log)" >&2; return 1
}

halt() {  # $1 name, $2 port -- removes only a container that has already stopped
  QUANT=$QUANT IMAGE=$IMG PORT="$2" CONTAINER_NAME="$1" bash stop.sh >>"$OUT/ops.log" 2>&1 || true
  local state
  state=$(docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null) || return 0
  if [[ "$state" == running || "$state" == created || "$state" == restarting ]]; then
    echo "STOP-REFUSED: $1 still $state; refusing to force-remove another tenant's engine" \
      | tee -a "$OUT/ops.log"
    return 1
  fi
  docker rm "$1" >/dev/null 2>&1 || true
  sleep 5
}

card_free() {  # refuse to boot onto a device someone is still holding
  local used
  used=$(nvidia-smi --id="$GPU" --query-gpu=memory.used --format=csv,noheader,nounits)
  if (( used > 2048 )); then
    echo "card $GPU holds ${used} MiB; aborting rather than spending 6 minutes on a boot" >&2
    echo "  that will fail with a device-contention error that looks like an engine bug" >&2
    return 1
  fi
  echo "  card $GPU clear (${used} MiB used)"
}

# Prompt construction: approximate by default. Hitting an exact token count costs ~80
# full-length /tokenize round-trips per prompt and eviction needs oversubscription, not
# precision. Set APPROX=0 to force the exact builder.
APPROX="${APPROX:-1}"
check_receipt() {  # $1 json path, $2 label
  python3 - "$1" "$2" <<'PY'
import json, sys
path, label = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(path))
except Exception as exc:
    print(f"{label}: no receipt ({exc})", file=sys.stderr); sys.exit(1)
lens = [len(b.get("sequence") or []) for b in d["burst"]]
seqs = [t for b in d["burst"] for t in (b.get("sequence") or [])]
ent = len(set(seqs))
empty = [i for i, n in enumerate(lens) if n < 8]
print(f"{label}: kind={d['burst'][0].get('sequence_kind')} lens={lens} distinct={ent} "
      f"evictions={d.get('preemptions')} tok/s={d.get('effective_prefill_tokens_per_second')} "
      f"prefix_hit={d.get('prefix_cache_hit_ratio')}", file=sys.stderr)
if empty:
    print(f"{label}: sequences {empty} have no token stream -- refusing to score an "
          f"absent measurement as agreement", file=sys.stderr); sys.exit(1)
if label.endswith("question") and ent < 32:
    print(f"{label}: output is a repetition cycle ({ent} distinct tokens) -- a PASS here "
          f"would prove nothing; re-check the prompt builder", file=sys.stderr); sys.exit(1)
PY
}

# ---------------------------------------------------------------- smoke gate
if [[ "$SKIP_SMOKE" != 1 ]]; then
  echo "=== smoke: both prompt modes at small depth on card $GPU ==="
  card_free || exit 1
  qname="qwen38-eid0"; qport=$PORT_BASE
  # MAX_MODEL_LEN has to exceed SMOKE_DEPTH: an earlier gate smoke-tested entropy at 2,048
  # tokens and passed, while the 245K legs it was guarding produced a repetition cycle. At
  # 2K the question is still recent context; the depth at which it stops being is the thing
  # worth checking, so the smoke engine is allowed to see 32K.
  boot 0.94 "$qname" "$qport" LONGCTX=0 MAX_MODEL_LEN=32768 || { echo "SMOKE-BOOT-FAILED" >&2; exit 1; }
  smoke_rc=0
  for mode in plain question; do
    flag=""; [[ "$mode" == question ]] && flag="--question --question-end"
    python3 scripts/test-eviction-identity.py phase \
        --base-url "http://127.0.0.1:$qport/v1" --model "$MODEL" \
        --depth "${SMOKE_DEPTH:-8192}" --concurrency 2 --output-tokens 64 \
        --approx --out-dir "$OUT/smoke-$mode" --tag "smoke-$mode" $flag \
        --min-distinct-tokens "${SMOKE_MIN_DISTINCT:-48}" > "$OUT/smoke-$mode.out" 2>&1
    if (( $? != 0 )); then echo "SMOKE-FAILED($mode): phase exited nonzero" >&2; smoke_rc=1; continue; fi
    check_receipt "$OUT/smoke-$mode/smoke-$mode.json" "smoke[$mode]" || smoke_rc=1
  done
  halt "$qname" "$qport" || smoke_rc=1
  (( smoke_rc != 0 )) && { echo "SMOKE-FAILED: aborting before any real leg boots" >&2; exit 1; }
  echo "smoke ok (both prompt modes yield high-entropy token streams)"
fi

# ---------------------------------------------------------------- legs
names=(); ports=(); tags=(); idx=0
for spec in $UTILS; do
  tag="${spec%%=*}"; u="${spec#*=}"
  idx=$((idx+1)); name="qwen38-eid$idx"; port=$((PORT_BASE + idx)); tags+=("$tag")
  if (( idx > 1 )); then
    halt "${names[$((idx-2))]}" "${ports[$((idx-2))]}" \
      || { echo "could not retire the previous leg; refusing to share a card" >&2; exit 1; }
  fi
  echo "=== leg $tag (util $u) ==="
  card_free || exit 1
  boot "$u" "$name" "$port" || exit 1
  B="http://127.0.0.1:$port/v1"
  docker logs "$name" > "$OUT/engine-$tag.log" 2>&1 || true
  extra=()
  (( idx > 1 )) && extra=(--skip-solo)      # the verdict compares burst outputs across legs
  python3 scripts/test-eviction-identity.py phase --base-url "$B" --model "$MODEL" \
      --depth "$DEPTH" --concurrency "$N" --output-tokens "$OT" --out-dir "$OUT" \
      --tag "$tag" --reuse-prompts ${QUESTION:+--question --question-end} ${APPROX:+--approx} \
      --min-distinct-tokens "${MIN_DISTINCT:-64}" "${extra[@]}" \
      > "$OUT/phase-$tag.out" 2>&1
  src=$?
  echo "phase rc=$src" | tee -a "$OUT/summary.txt"
  (( src != 0 )) && { echo "leg $tag FAILED; see $OUT/phase-$tag.out" >&2; names+=("$name"); ports+=("$port"); continue; }
  check_receipt "$OUT/$tag.json" "$tag" || echo "  (leg $tag receipt flagged; compare() will judge)"
  python3 - "$tag" "$OUT/$tag.json" "$ROWS" <<'PY'
import json, sys
tag, path, rows = sys.argv[1:4]
d = json.load(open(path))
seq = [t for b in d["burst"] for t in (b.get("sequence") or [])]
with open(rows, "a") as fh:
    fh.write("\t".join(str(x) for x in [
        tag, d.get("preemptions"), d.get("wall_seconds"), d.get("prompts_sha"),
        [b.get("seq_sha") for b in d["burst"]], d.get("prefix_cache_hit_ratio"), len(set(seq)),
        f"tok/s={d.get('effective_prefill_tokens_per_second')}"
        + (" CACHE-DEFEATED" if d.get("cache_defeated") else "")]) + "\n")
PY
  names+=("$name"); ports+=("$port")
done

echo "=== compare ==="
python3 scripts/test-eviction-identity.py compare --out-dir "$OUT" --tags "${tags[@]}" \
  2>&1 | tee "$OUT/compare.txt"
cmp_rc=${PIPESTATUS[0]}
echo "compare rc=$cmp_rc" >> "$OUT/summary.txt"

if [[ "$CLEANUP" == 1 ]]; then
  for i in "${!names[@]}"; do halt "${names[$i]}" "${ports[$i]}" || true; done
fi
echo "EVICTION IDENTITY DONE $(date -u +%FT%TZ) rc=$cmp_rc" >> "$ROWS"
exit "$cmp_rc"

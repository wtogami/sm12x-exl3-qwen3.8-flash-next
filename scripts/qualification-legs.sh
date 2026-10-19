#!/usr/bin/env bash
# Boot-orchestrated qualification legs for one or more profiles.
#
# Unlike quality-suite.sh (which expects an engine you started yourself),
# this script owns the whole cycle per profile: start via ./start.sh with
# the chosen topology, run the full functional suite, record per-step exit
# codes without aborting the matrix, stop, next profile.  Resume-safe: a
# profile whose first receipt exists is skipped.
#
# Env knobs (all with sane defaults):
#   IMAGE              engine image           (default localhost/qwen38-rtx:local)
#   QUANTS             profiles to qualify    (default "exl3 exl3-ple8 nvfp4")
#   TP_SIZE            1 or 2                 (default 1)
#   GPU                card for TP=1 boots    (default 1, per host convention)
#   PORT               serve port             (default 8002 TP=1, 8125 TP=2)
#   MTP_TOKENS         passed to start.sh only if set; unset uses the
#                      per-profile defaults there (exl3 3, nvfp4 2)
#   RESULT_DIR         receipt tree           (REQUIRED; must not be a file)
#   RETRIEVAL_TOKENS   standard retrieval leg (default "8192 240000")
#   BOUNDARY_TOKENS    past-native-window retrieval leg proving the
#                      PR 62409 cache floor; set EMPTY on native-window
#                      (LONGCTX=0 / Spark) hosts where >262144 is invalid
#                      (default "280000 480000")
#   TOOL_EVAL_DIR      tool-eval-bench checkout for tool-quality.sh
#   DRY_RUN=1          print the plan and exit 0 without booting anything
#
# Receipt layout: RESULT_DIR/<QUANT>/{api-tools,vision,retrieval,
# boundary-62409,structured}.json( l) + seven/orchid .jsonl + tools/.
# Exit status: 0 only if every step of every profile exited 0.
set -uo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-localhost/qwen38-rtx:local}"
QUANTS="${QUANTS:-exl3 exl3-ple8 nvfp4}"
TP_SIZE="${TP_SIZE:-1}"
GPU="${GPU:-1}"
PORT="${PORT:-$([[ "$TP_SIZE" == 1 ]] && echo 8002 || echo 8125)}"
B="http://127.0.0.1:${PORT}"
RESULT_DIR="${RESULT_DIR:?Set a result directory for this run}"
RETRIEVAL_TOKENS="${RETRIEVAL_TOKENS:-8192 240000}"
BOUNDARY_TOKENS="${BOUNDARY_TOKENS-280000 480000}"
LOG="$RESULT_DIR/driver.log"

stop_all() {  # best effort; containers are named qwen38-<QUANT>
  local c
  for c in $(docker ps -a --format '{{.Names}}' 2>/dev/null | grep '^qwen38-' || true); do
    docker stop -t 120 "$c" >/dev/null 2>&1 || true
    local i; for i in $(seq 1 30); do docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$c" || break; sleep 5; done
    docker rm -f "$c" >/dev/null 2>&1 || true
  done
}

boot_engine() {  # $1 = QUANT -> returns 0 once the alias answers /v1/models
  local q="$1" alias="qwen38-$1" i
  stop_all
  ( cd "$SCRIPT_DIR" && QUANT="$q" TP_SIZE="$TP_SIZE" GPU="$GPU" PORT="$PORT" \
      IMAGE="$IMAGE" ${MTP_TOKENS:+MTP_TOKENS="$MTP_TOKENS"} ./start.sh ) >>"$LOG" 2>&1 \
    || { echo "$q START-FAILED" | tee -a "$LOG"; return 1; }
  for i in $(seq 1 120); do
    curl -sf "$B/v1/models" 2>/dev/null | grep -q "\"$alias\"" && { echo "$q ready $(date -u +%FT%TZ)" | tee -a "$LOG"; return 0; }
    sleep 10
  done
  echo "$q NEVER-READY" | tee -a "$LOG"; return 1
}

step() {  # step LABEL CMD... records rc without aborting the matrix
  local label="$1"; shift
  "$@" >>"$LOG" 2>&1; local rc=$?
  echo "$label exit $rc" | tee -a "$LOG"
  FAILURES=$(( FAILURES + (rc > 0) ))
}

leg() {  # $1 = QUANT
  local q="$1" alias="qwen38-$1" dir="$RESULT_DIR/$1"
  mkdir -p "$dir/tools"
  if [[ -e "$dir/api-tools.json" ]]; then echo "===== $q complete already, skip" | tee -a "$LOG"; return 0; fi
  echo "===== $q boot $(date -u +%FT%TZ)" | tee -a "$LOG"
  boot_engine "$q" || { FAILURES=$((FAILURES+1)); return 1; }
  local common=(--base-url "$B" --model "$alias")
  step "$q api"        python3 "$SCRIPT_DIR/scripts/test-api-tool-constraints.py" "${common[@]}" --output "$dir/api-tools.json"
  step "$q vision"     python3 "$SCRIPT_DIR/scripts/test-vision-vllm.py" "${common[@]}" --output "$dir/vision.json"
  # shellcheck disable=SC2086
  step "$q retrieval"  python3 "$SCRIPT_DIR/scripts/test-context-retrieval.py" "${common[@]}" --filler-tokens $RETRIEVAL_TOKENS --output "$dir/retrieval.json"
  if [[ -n "$BOUNDARY_TOKENS" ]]; then
    # shellcheck disable=SC2086
    step "$q boundary" python3 "$SCRIPT_DIR/scripts/test-context-retrieval.py" "${common[@]}" --filler-tokens $BOUNDARY_TOKENS --output "$dir/boundary-62409.json"
  fi
  step "$q structured" python3 "$SCRIPT_DIR/scripts/verify-structured-output-live.py" --base-url "$B/v1" --model "$alias" --output "$dir/structured.json"
  step "$q seven"      python3 "$SCRIPT_DIR/scripts/benchmark-workloads.py" "${common[@]}" --suite seven --runs 3 --warmups 0 --output "$dir/seven.jsonl"
  step "$q orchid"     python3 "$SCRIPT_DIR/scripts/benchmark-workloads.py" "${common[@]}" --suite orchid --runs 5 --warmups 0 --output "$dir/orchid.jsonl"
  TOOL_EVAL_DIR="${TOOL_EVAL_DIR:-}" RESULT_DIR="$dir/tools" BASE_URL="$B" MODEL="$alias" \
    bash "$SCRIPT_DIR/scripts/tool-quality.sh" >>"$LOG" 2>&1; local rc=$?
  echo "$q tools exit $rc" | tee -a "$LOG"; FAILURES=$(( FAILURES + (rc > 0) ))
  echo "===== $q LEG-DONE $(date -u +%FT%TZ)" | tee -a "$LOG"
}

if [[ "${DRY_RUN:-0}" == 1 ]]; then
  echo "IMAGE=$IMAGE TP_SIZE=$TP_SIZE GPU=$GPU PORT=$PORT B=$B"
  echo "QUANTS=$QUANTS  RETRIEVAL=[$RETRIEVAL_TOKENS]  BOUNDARY=[${BOUNDARY_TOKENS:-<skipped>}]"
  echo "RESULT_DIR=$RESULT_DIR  MTP_TOKENS=${MTP_TOKENS:-<start.sh defaults>}"
  echo "steps per profile: api vision retrieval boundary structured seven orchid tools"; exit 0
fi

FAILURES=0
mkdir -p "$RESULT_DIR"
{ echo "===== QUAL START $(date -u +%FT%TZ) image=$IMAGE tp=$TP_SIZE quants=$QUANTS"
  nvidia-smi --query-gpu=index,power.limit --format=csv,noheader 2>/dev/null || true
} >>"$LOG"
for q in $QUANTS; do leg "$q"; done
stop_all
echo "===== QUAL DONE $(date -u +%FT%TZ) failures=$FAILURES" | tee -a "$LOG"
exit $(( FAILURES > 0 ))

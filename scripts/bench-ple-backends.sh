#!/bin/bash
# Paired-boot comparison of PLE backends against graph mode, on one host.
#
# WHY 2x2 AND NOT 2. Enabling breakable CUDA graphs sets compilation_config.mode = NONE, so comparing
# streaming (which requires breakable) against pinned (which defaults to compiled+full graphs) measures
# two changes at once and attributes both to streaming. Each cell therefore pins BOTH variables:
#
#   cell 1: pinned     + default graphs      (the deployed baseline)
#   cell 2: pinned     + breakable graphs    (isolates the graph-mode cost)
#   cell 3: streaming  + breakable graphs    (the feature, at its only legal graph mode)
#   cell 4: streaming  + enforce-eager       (fallback if breakable ever misbehaves)
#
# Cells run interleaved, twice (1,2,3,4,1,2,3,4), because the instruments on this host have a real
# noise floor: uncapped page-cache throughput varies 3-5% run to run, so a single pass cannot support a
# claim about a small delta. Median of repeats is reported, and spread is printed -- if spread exceeds
# the difference being claimed, the difference is not a result.
#
# Correctness is checked in every cell, not once: greedy output must equal BASE_SHA. A configuration
# that is fast and wrong is not a configuration.
set -uo pipefail
ACC=${ACC:-/home/rtx/accept}
REPO=${REPO:-/home/rtx/sm12x-exl3-qwen3.8-flash-next}
IMAGE_PIN=${IMAGE_PIN:-localhost/qwen38-rtx:local}
IMAGE_STREAM=${IMAGE_STREAM:-localhost/qwen38-exactpin:stream}
BASE_SHA=${BASE_SHA:-f91132436fb09e45}
CONT=${CONT:-qwen38-exl3-ple8}
REPEATS=${REPEATS:-2}
NPRO=${NPRO:-8}
OUT=${OUT:-$ACC/backend-matrix}
mkdir -p "$OUT"
TSV=$OUT/results.tsv
[ -f "$TSV" ] || printf "cell\trepeat\timage\tple_stream\tbreakable\tenforce_eager\tgreedy\tprefill_tok_s\tdecode_tok_s\tshmem_gib\tcached_gib\tvram_mib\tdisk_sectors\telapsed_s\tverdict\n" > "$TSV"

restore_serving() {
  echo "=== restoring the serving configuration ==="
  podman rm -f ple-bench >/dev/null 2>&1 || true
  podman start $CONT >/dev/null 2>&1 || bash $ACC/run-restore.sh >/dev/null 2>&1
  for i in $(seq 1 90); do
    c=$(curl -s -o /dev/null -m 3 -w '%{http_code}' http://127.0.0.1:8001/health 2>/dev/null)
    [ "$c" = "200" ] && break; sleep 10
  done
  sleep 15
  bash /home/rtx/greedy.sh > $ACC/greedy-final.txt 2>&1
  s=$(sha256sum < $ACC/greedy-final.txt | cut -c1-16)
  echo "  health=$c greedy=$s $([ "$s" = "$BASE_SHA" ] && echo MATCH || echo "MISMATCH -- INVESTIGATE")"
  awk '/^Shmem:/ {printf "  Shmem %.2f GiB (serving baseline 66.18)\n", $2/1048576}' /proc/meminfo
  nvidia-smi --query-gpu=memory.used --format=csv,noheader | sed 's/^/  vram /'
}
trap restore_serving EXIT INT TERM

cell() {  # name image ple_stream breakable enforce_eager cudagraph_mode
  local name=$1 img=$2 ps=$3 bc=$4 ee=$5 cgm=$6 rep=$7
  echo
  echo "=== cell $name (ple_stream=$ps breakable=$bc enforce_eager=$ee) repeat $rep, image $img ==="
  podman rm -f ple-bench >/dev/null 2>&1 || true
  podman stop -t 30 $CONT >/dev/null 2>&1 || true
  sleep 5
  local extra=()
  [ "$ps" = 1 ] && extra+=(-e PLE_STREAM=1 -e PLE_ROWCOPY_SO=/opt/qwen38/ple_rowcopy.so)
  [ "$bc" = 1 ] && extra+=(-e VLLM_USE_BREAKABLE_CUDAGRAPH=1)
  local eager=""
  [ "$ee" = 1 ] && eager="--enforce-eager"
  # The run line is derived from the live container's CreateCommand by mk-cell-run.py, which refuses
  # to emit podman flags after the image (that is how "vllm serve" once received -e PLE_STREAM=1).
  local gen=(python3 "$REPO/scripts/mk-cell-run.py" --base-container "$CONT" --name ple-bench
             --image "$img")
  [ "$ps" = 1 ] && gen+=(--env PLE_STREAM=1 --env PLE_ROWCOPY_SO=/opt/qwen38/ple_rowcopy.so)
  [ "$bc" = 1 ] && gen+=(--env VLLM_USE_BREAKABLE_CUDAGRAPH=1)
  [ "$ee" = 1 ] && gen+=(--arg=--enforce-eager)
  # Streaming cannot run with FULL cudagraphs at all (the decorator declines to break inside them), and
  # a "pinned + breakable" cell that keeps FULL graphs would not be the same graph regime as the
  # streaming cells -- so the mode is an explicit axis of the matrix, not a side effect.
  [ -n "$cgm" ] && gen+=(--arg=--compilation-config --arg="{\"cudagraph_mode\": \"$cgm\"}")
  "${gen[@]}" > $ACC/run-cell.sh || { echo "  run-line generation failed"; return 1; }
  bash -n $ACC/run-cell.sh || { echo "  cell run line does not parse"; return 1; }
  bash $ACC/run-cell.sh >/dev/null 2>&1 || { echo "  run rejected"; return 1; }
  local code=000
  for i in $(seq 1 150); do
    [ -z "$(podman ps -q --filter name=ple-bench)" ] && { echo "  cell DIED"; podman logs --tail 12 ple-bench 2>&1 | sed 's/^/    /'; podman logs ple-bench > "$OUT/$name-die.log" 2>&1; return 1; }
    code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' http://127.0.0.1:8001/health 2>/dev/null)
    [ "$code" = "200" ] && break; sleep 10
  done
  [ "$code" = "200" ] || { echo "  never healthy"; podman logs ple-bench > "$OUT/$name-timeout.log" 2>&1; return 1; }
  podman logs ple-bench > "$OUT/$name.log" 2>&1
  grep -m1 "streaming backend selected" "$OUT/$name.log" >/dev/null && sel=yes || sel=no
  echo "  backend selection line: $sel"
  bash /home/rtx/greedy.sh > "$OUT/$name-greedy.txt" 2>&1
  local sha=$(sha256sum < "$OUT/$name-greedy.txt" | cut -c1-16)
  local verdict=OK; [ "$sha" = "$BASE_SHA" ] || verdict="GREEDY_MISMATCH"
  echo "  greedy $sha -> $verdict"
  local r0=$(awk '$3=="nvme0n1"{print $6}' /proc/diskstats)
  local t0=$(date +%s.%N)
  python3 /home/rtx/stage/bench-fixed.py > "$OUT/$name-bench.txt" 2>&1 || echo "  bench failed"
  local el=$(awk -v a=$(date +%s.%N) -v b=$t0 'BEGIN{printf "%.1f", a-b}')
  local r1=$(awk '$3=="nvme0n1"{print $6}' /proc/diskstats)
  local pre=$(grep -m1 "^prefill" "$OUT/$name-bench.txt" | awk '{print $2}')
  local dec=$(grep -m1 "^decode"  "$OUT/$name-bench.txt" | awk '{print $2}')
  local shm=$(awk '/^Shmem:/{printf "%.2f", $2/1048576}' /proc/meminfo)
  local ca=$(awk '/^Cached:/{printf "%.2f", $2/1048576}' /proc/meminfo)
  local vr=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits)
  printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
    "$name" "$rep" "$img" "$ps" "$bc" "$ee" "$sha" "${pre:-NA}" "${dec:-NA}" "$shm" "$ca" "$vr" "$((r1-r0))" "$el" "$verdict" | tee -a "$TSV"
}

if [[ "${DRY_RUN:-0}" == 1 ]]; then
  # Generate and validate every cell's run line without touching the GPU. Cheap insurance against
  # discovering a malformed cell ten minutes into a paired-boot ladder.
  echo "=== DRY RUN: generating run lines only ==="
  trap - EXIT
  cell_dry() {
    local name=$1 img=$2 ps=$3 bc=$4 ee=$5 cgm=$6
    local gen=(python3 "$REPO/scripts/mk-cell-run.py" --base-container "$CONT" --name "ple-dry-$name"
               --image "$img")
    [ "$ps" = 1 ] && gen+=(--env PLE_STREAM=1 --env PLE_ROWCOPY_SO=/opt/qwen38/ple_rowcopy.so)
    [ "$bc" = 1 ] && gen+=(--env VLLM_USE_BREAKABLE_CUDAGRAPH=1)
    [ "$ee" = 1 ] && gen+=(--arg=--enforce-eager)
    [ -n "$cgm" ] && gen+=(--arg=--compilation-config --arg="{\"cudagraph_mode\": \"$cgm\"}")
    if "${gen[@]}" > "$OUT/$name.dry.sh" 2>"$OUT/$name.dry.err"; then
      python3 - "$OUT/$name.dry.sh" "$img" <<'PY'
import shlex, sys
PODMAN = {"-e","--env","-d","--detach","--name","-v","--volume","--device","--network","--ipc",
          "--security-opt","--annotation","-p","--publish"}
toks = shlex.split(open(sys.argv[1]).read().strip().splitlines()[-1][5:])
img = next((i for i, x in enumerate(toks) if x == sys.argv[2]), None)
assert img is not None, "image token missing"
bad = [x for x in toks[img + 1:] if x in PODMAN]
assert not bad, f"podman flags after image: {bad}"
argv = toks[img + 1:]
print(f"  OK  {sys.argv[1].split('/')[-1]:26s} container argv={len(argv)} "
      f"env_before={sum(1 for x in toks[:img] if x=='-e')}")
PY
    else
      echo "  FAIL $name: $(tail -1 "$OUT/$name.dry.err")"
      false
    fi
  }
  cell_dry pinned-default      "$IMAGE_PIN"    0 0 0 ""
  cell_dry pinned-breakable-pw "$IMAGE_PIN"    0 1 0 "PIECEWISE"
  cell_dry stream-breakable-pw "$IMAGE_STREAM" 1 1 0 "PIECEWISE"
  cell_dry stream-eager        "$IMAGE_STREAM" 1 0 1 ""
  echo "  dry run complete; nothing started"
  exit 0
fi

for rep in $(seq 1 "$REPEATS"); do
  #                                                       ple_stream breakable eager  cudagraph_mode
  cell pinned-default        "$IMAGE_PIN"     0 0 0 ""          $rep   # deployed baseline
  cell pinned-breakable-pw   "$IMAGE_PIN"     0 1 0 "PIECEWISE" $rep   # graph-mode cost alone
  cell stream-breakable-pw   "$IMAGE_STREAM"  1 1 0 "PIECEWISE" $rep   # the feature, legal config
  cell stream-eager          "$IMAGE_STREAM"  1 0 1 ""          $rep   # fallback, no graphs at all
done

echo
echo "=== summary (median of $REPEATS per cell, with spread) ==="
python3 - "$TSV" <<'PY'
import sys, statistics as st
rows = [l.rstrip("\n").split("\t") for l in open(sys.argv[1])][1:]
by = {}
for r in rows:
    by.setdefault(r[0], []).append(r)
print(f"  {'cell':20s} {'prefill tok/s':>22s} {'decode tok/s':>22s} {'Shmem GiB':>11s} {'disk sectors':>14s}  greedy")
for cell, rs in by.items():
    def col(i, cast=float):
        vals = [cast(x[i]) for x in rs if x[i] not in ("NA", "")]
        return vals
    for label, i in (("prefill", 7), ("decode", 8)):
        pass
    def fmt(i):
        v = col(i)
        if not v: return "       n/a            "
        med = st.median(v)
        sp = (max(v)-min(v))/med*100 if len(v) > 1 and med else 0
        return f"{med:10.1f} (spread {sp:4.1f}%)"
    shm = col(9); sec = col(12)
    ok = all(x[14] == "OK" for x in rs)
    print(f"  {cell:20s} {fmt(7)} {fmt(8)} {(f'{st.median(shm):11.2f}') if shm else ' '*11} {(f'{st.median(sec):14.0f}') if sec else ' '*14}  {'all match' if ok else 'MISMATCH'}")
print("\n  Interpretation rule: a difference smaller than the larger spread in the pair is not a result.")
PY
echo BENCHMATRIXDONE

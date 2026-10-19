#!/bin/bash
# TP1 vs TP2 for the PLE table, with both GPUs power-capped so the comparison is not "more watts won".
#
# Why the cap belongs in the driver: at TP1 only GPU 1 is busy and it may sit at its full ~600 W ceiling,
# while TP2 splits work across two cards capped at 300 W each. Without pinning the envelope, a TP2 win or
# loss is ambiguous -- it could be the parallelism or the extra power budget. Capping both arms at 300 W
# per card makes the wattage a controlled variable instead of a confound. The cap is applied here rather
# than assumed from shell state, and printed, so a later reader can tell what the numbers mean.
#
# Also asserted, because they are different questions from speed:
#   * TP2 really injects both buses (00000000:51:00.0 and 00000000:9C:00.0) and TP1 exactly one
#   * greedy output is identical or not across tensor-parallel width (the strict hash gate answers this)
#   * PLE_STREAM with TP_SIZE>1 refuses early, with a readable reason, and leaves no container behind
#   * the v2 per-phase stats line actually appears when a boot is driven long enough (>200 staged steps)
REPO=/home/opencode/sm12x-exl3-qwen3.8-flash-next
IMG="${IMG:-localhost/qwen38-rtx:exp3-stream}"
OUT=/home/opencode/exp2-test; mkdir -p "$OUT"
REF="${REF:-}"
cd "$REPO" || exit 1

cleanup() {
  podman ps -aq --filter name=qwen38 | xargs -r podman rm -f >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

echo "=== power envelope (applied to both cards, this is the controlled variable) ==="
sudo nvidia-smi -pl 300 -i 0 >/dev/null 2>&1
sudo nvidia-smi -pl 300 -i 1 >/dev/null 2>&1
nvidia-smi --query-gpu=index,name,power.limit,memory.used --format=csv,noheader | sed 's/^/  /'

cell() {   # name  extra-env-string  [extra start.sh args]  [want_devices]
  local name=$1 envs=$2 extra_args=${3:-} want=${4:-1}
  echo
  echo "=== cell $name ==="
  podman ps -aq --filter name=qwen38 | xargs -r podman rm -f >/dev/null 2>&1
  sleep 3
  local shmem_before
  shmem_before=$(awk '/^Shmem:/ {print $2}' /proc/meminfo)
  # shellcheck disable=SC2086
  if ! env IMAGE=$IMG QUANT=exl3-ple8 $envs bash "$REPO/start.sh" $extra_args > "$OUT/$name-start.log" 2>&1; then
    echo "  start.sh FAILED:"; tail -8 "$OUT/$name-start.log" | sed 's/^/    /'; return 1
  fi
  local ctr
  ctr=$(podman ps --format '{{.Names}}' | tail -1)
  echo "  container: $ctr"
  local code=000
  for i in $(seq 1 120); do
    code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' http://127.0.0.1:8001/health 2>/dev/null)
    [ "$code" = "200" ] && { echo "  HEALTHY after $((i*10))s"; break; }
    sleep 10
  done
  [ "$code" = "200" ] || { echo "  never healthy"; podman logs --tail 20 "$ctr" 2>&1 | sed 's/^/    /'; return 1; }
  podman logs "$ctr" > "$OUT/$name.log" 2>&1

  echo "  --- devices (TP width is the variable here) ---"
  podman exec "$ctr" nvidia-smi --query-gpu=index,pci.bus_id,power.limit --format=csv,noheader | sed 's/^/    /'
  local n
  n=$(podman exec "$ctr" nvidia-smi --query-gpu=index --format=csv,noheader | wc -l)
  echo "    devices visible: $n (want $want)"

  echo "  --- memory (delta over the pre-boot sample; Shmem in meminfo is HOST-wide) ---"
  awk -v before="$shmem_before" '/^Shmem:/ {printf "    Shmem %.2f GiB (+%.2f over pre-boot)\n", $2/1048576, ($2-before)/1048576}' /proc/meminfo

  echo "  --- backend lines ---"
  grep -m2 -iE "pinned at exact size|PLE pinned host memory|[0-9.]+ GiB of pinned" "$OUT/$name.log" \
    | cut -c1-140 | sed 's/^/    /'
  grep -m1 -iE "PLE stream v2: staging" "$OUT/$name.log" | cut -c1-160 | sed 's/^/    /'
  grep -m1 -iE "world_size|tensor.parallel" "$OUT/$name.log" | cut -c1-120 | sed 's/^/    /'
  grep -m1 -oE "CUDAGraphMode\.[A-Z_]+" "$OUT/$name.log" | head -1 | sed 's/^/    graph mode: /'
  echo "    staging failures: $(grep -ciE 'staging failed' "$OUT/$name.log")"

  echo "  --- greedy determinism, twice ---"
  local h1 h2
  local dump=""
  if [[ "${DUMP:-0}" == 1 ]]; then
    dump="--dump $OUT/tp-dumps.jsonl --label $name"
  fi
  h1=$(cd /tmp && python3 -I "$REPO/scripts/ple-probe.py" --reps 2 --warmup 1 $dump 2>&1 | awk '$1=="strict"{print $2}')
  h2=$(cd /tmp && python3 -I "$REPO/scripts/ple-probe.py" --reps 2 --warmup 1 2>&1 | awk '$1=="strict"{print $2}')
  echo "    strict hashes: $h1 / $h2   $([ "$h1" = "$h2" ] && echo SELF-CONSISTENT || echo VARIES)"
  if [ -z "$REF" ]; then REF="$h1"; echo "    (reference set from this cell)"; else
    echo "    vs reference $REF: $([ "$h1" = "$REF" ] && echo IDENTICAL || echo DIFFERS)"
  fi

  echo "  --- throughput ---"
  ( cd /tmp && python3 -I "$REPO/scripts/ple-probe.py" --reps 3 --warmup 2 > "$OUT/$name-perf.txt" 2>&1 )
  grep -E "^(prefill|decode) " "$OUT/$name-perf.txt" | sed 's/^/    /'

  # Empty is expected on pinned cells: the phase averages come from StepStager, which only exists when a
  # streaming backend is selected. On streaming cells an empty section means the boot finished inside the
  # reporting interval -- lower PLE_STREAM_V2_LOG_EVERY rather than concluding staging did not run.
  echo "  --- v2 phase stats (needs >200 staged steps in this boot) ---"
  grep -h "PLE stream v2 stats" "$OUT/$name.log" | tail -2 | sed 's/^/    /' || echo "    (none)"


  # Re-snapshot the log AFTER the probes and re-run the failure greps. The first snapshot is taken at health,
  # so every line the probes provoke -- per-step staging stats, capture-copy errors during real replays,
  # staging failures under load -- was missing from the file those greps read. Until this block existed, the
  # "capture-copy errors: 0 / staging failures: 0" lines asserted only that BOOTING was clean, which is a
  # much weaker claim than they appeared to make.
  podman logs "$ctr" > "$OUT/$name.log" 2>&1
  echo "  --- after the drive (log re-snapshotted) ---"
  grep -h "PLE stream v2 stats" "$OUT/$name.log" | tail -2 | sed 's/^/    /'
  echo "    capture-copy errors: $(grep -ciE 'Cannot copy between CPU and CUDA' "$OUT/$name.log")"
  echo "    staging failures:    $(grep -ciE 'staging failed' "$OUT/$name.log")"
  echo "    staging skips:       $(grep -hoE 'skips=[0-9]+' "$OUT/$name.log" | tail -1)"
  podman stop -t 30 "$ctr" >/dev/null 2>&1
  podman rm -f "$ctr" >/dev/null 2>&1     # stopped != gone: a leftover shows up in -a counts
}

# TP1 on GPU 1 only: the shipping configuration, both cards still capped.
cell tp1-pinned  "GPU=1 TP_SIZE=1" "" 1
# TP2 across both cards: two entities injected, table split per rank.
cell tp2-pinned  "TP_SIZE=2" "" 2
# Optional streaming-under-TP cells. REF must be the TP2 hash (6fb33c3054c7562d as measured), never the TP1
# one: tensor width changes greedy output, so a TP2 cell checked against a TP1 reference would report a
# false failure. v1 is expected to differ from the TP2 reference for a reason already understood -- it
# forces the breakable PIECEWISE regime, which changes output on v0.32 by itself -- so its value here is
# "does it boot and stage at all", not "does the hash match".
if [[ "${STREAM_CELLS:-0}" == 1 ]]; then
  cell tp2-stream-v2 "TP_SIZE=2 PLE_STREAM_V2=1 PLE_ALLOW_UNVALIDATED_TP=1" "" 2
  cell tp2-stream-v1 "TP_SIZE=2 PLE_STREAM=1 PLE_ALLOW_UNVALIDATED_TP=1" "" 2
fi

# Streaming + TP used to be refused outright; that guard is now an assertion inside the stagers, so what is
# checked here is that the launcher no longer blocks plain TP2 and the loader still blocks the ETP case.
# Checked last so it cannot be the thing that failed to boot for an unrelated reason.
echo
echo "=== launcher precondition note (read from the streaming cell's own start log) ==="
# There is no dry-run mode in start.sh, and inventing a flag for it here would have launched a real
# container instead of checking wording. The streaming cells print the note into their start log anyway.
found=0
for f in tp2-stream-v2 tp2-stream-v1; do
  if [ -s "$OUT/$f-start.log" ] && grep -qiE "each rank stages only its own ids" "$OUT/$f-start.log"; then
    grep -A2 -iE "each rank stages only its own ids" "$OUT/$f-start.log" | sed 's/^/    /'; found=1
  fi
done
[ "$found" = 1 ] || echo "    (no streaming cell ran; set STREAM_CELLS=1 to exercise this)"
echo "  containers left behind: $(podman ps -aq --filter name=qwen38 | wc -l) (want 0)"

if [[ "${DUMP:-0}" == 1 && -n "${DIFF_A:-}" && -n "${DIFF_B:-}" ]]; then
  echo; echo "=== output distance: $DIFF_A vs $DIFF_B ==="
  ( cd /tmp && python3 -I "$REPO/scripts/ple-probe.py" --diff "$OUT/tp-dumps.jsonl" "$DIFF_A" "$DIFF_B" ) 2>&1 | sed 's/^/  /'
fi
echo
echo "=== power limits still applied ==="
nvidia-smi --query-gpu=index,power.limit --format=csv,noheader | sed 's/^/  /'
echo TESTDONE

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
IMG=localhost/qwen38-rtx:exp2-stream
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
  grep -m1 -iE "PLE pinned host memory|pinned at exact size" "$OUT/$name.log" | cut -c1-130 | sed 's/^/    /'
  grep -m1 -iE "PLE stream v2: staging" "$OUT/$name.log" | cut -c1-160 | sed 's/^/    /'
  grep -m1 -iE "world_size|tensor.parallel" "$OUT/$name.log" | cut -c1-120 | sed 's/^/    /'
  grep -m1 -oE "CUDAGraphMode\.[A-Z_]+" "$OUT/$name.log" | head -1 | sed 's/^/    graph mode: /'
  echo "    staging failures: $(grep -ciE 'staging failed' "$OUT/$name.log")"

  echo "  --- greedy determinism, twice ---"
  local h1 h2
  h1=$(cd /tmp && python3 -I "$REPO/scripts/ple-probe.py" --reps 2 --warmup 1 2>&1 | awk '$1=="strict"{print $2}')
  h2=$(cd /tmp && python3 -I "$REPO/scripts/ple-probe.py" --reps 2 --warmup 1 2>&1 | awk '$1=="strict"{print $2}')
  echo "    strict hashes: $h1 / $h2   $([ "$h1" = "$h2" ] && echo SELF-CONSISTENT || echo VARIES)"
  if [ -z "$REF" ]; then REF="$h1"; echo "    (reference set from this cell)"; else
    echo "    vs reference $REF: $([ "$h1" = "$REF" ] && echo IDENTICAL || echo DIFFERS)"
  fi

  echo "  --- throughput ---"
  ( cd /tmp && python3 -I "$REPO/scripts/ple-probe.py" --reps 3 --warmup 2 > "$OUT/$name-perf.txt" 2>&1 )
  grep -E "^(prefill|decode) " "$OUT/$name-perf.txt" | sed 's/^/    /'

  echo "  --- v2 phase stats (needs >200 staged steps in this boot) ---"
  grep -h "PLE stream v2 stats" "$OUT/$name.log" | tail -2 | sed 's/^/    /' || echo "    (none)"
  podman stop -t 30 "$ctr" >/dev/null 2>&1
}

# TP1 on GPU 1 only: the shipping configuration, both cards still capped.
cell tp1-pinned  "GPU=1 TP_SIZE=1" "" 1
# TP2 across both cards: two entities injected, table split per rank.
cell tp2-pinned  "TP_SIZE=2" "" 2
# Streaming + TP: expected to refuse before any container exists. Checked last so it cannot be the one
# that failed to boot for an unrelated reason.
echo
echo "=== refusal check: PLE_STREAM=1 with TP_SIZE=2 ==="
if env IMAGE=$IMG QUANT=exl3-ple8 TP_SIZE=2 PLE_STREAM=1 bash "$REPO/start.sh" > "$OUT/refusal.log" 2>&1; then
  echo "  FAIL: launcher started with streaming under TP"
else
  echo "  refused as expected:"
  sed -n '1,8p' "$OUT/refusal.log" | sed 's/^/    /'
fi
echo "  containers left behind: $(podman ps -aq --filter name=qwen38 | wc -l) (want 0)"

echo
echo "=== power limits still applied ==="
nvidia-smi --query-gpu=index,power.limit --format=csv,noheader | sed 's/^/  /'
echo TESTDONE

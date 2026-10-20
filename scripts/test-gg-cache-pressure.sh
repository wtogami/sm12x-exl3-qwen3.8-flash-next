#!/bin/bash
# How much page cache does a streaming table actually need? Measured by taking it away.
#
# Streaming replaces a table-sized pinned allocation with reads through the page cache, so the host's RAM
# stops being "big enough to hold the table" and becomes "big enough to hold the working set". The point of
# this driver is to find that second number on a host that has far more RAM than either figure, by capping the
# container (--memory, swap pinned to the same value) and dropping the host page cache before each boot
# (ple-drop-caches), so every cell starts cold and stays inside its own budget.
#
# Each cell reports: seconds to health, the cgroup's anon/file/slab after the drive, refaults and reclaim
# events during the drive, the staging phase split from the engine's own stats line, strict greedy hash, and
# prefill/decode throughput. The two things worth comparing across caps are the throughput curve and whether
# the hash moves at all -- it should not, since eviction changes which pages are resident, not what the model
# computes. If the hash DOES move under memory pressure, that is a bug in the staging path, not a curiosity.
#
# The pinned cells are the control: `round2` shows what a table-sized pin costs a capped host, and the
# preflight is expected to refuse rather than oversubscribe, which is itself one of the claims.
#
# Run: bash scripts/test-gg-cache-pressure.sh
#      CAPS="16 8" bash scripts/test-gg-cache-pressure.sh
set -uo pipefail
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1
IMG="${IMG:-localhost/qwen38-rtx:exp3}"
OUT="${OUT:-$HOME/gg-cache}"; mkdir -p "$OUT"
PORT="${PORT:-8001}"
CAPS="${CAPS:-32 16 8}"
REF="${REF:-}"

memline() { awk -v OFS=" " '/^MemAvailable:/{a=$2/1048576} /^Shmem:/{s=$2/1048576} END{printf "MemAvailable %.2f GiB, Shmem %.2f GiB", a, s}' /proc/meminfo; }

# cgroup counters that say what the cap did: bytes of cache held, and what happened when it did not fit.
cgstat() {  # $1 = container
  podman exec "$1" sh -c 'cat /sys/fs/cgroup/memory.stat /sys/fs/cgroup/memory.events 2>/dev/null' \
    | awk '$1 !~ /^$/ {v[$1]=$2} END {printf "anon=%.0f file=%.0f slab=%.0f refault_file=%.0f refault_anon=%.0f max=%.0f high=%.0f", v["anon"]/1048576, v["file"]/1048576, v["slab"]/1048576, v["workingset_refault_file"], v["workingset_refault_anon"], v["max"], v["high"]}'
}

cleanup() {
  podman ps -aq --filter name=qwen38 | xargs -r podman rm -f >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

cell() {   # name  memory-cap  extra-env-string
  local name=$1 cap=$2 envs=$3
  echo
  echo "=== cell $name (cap: ${cap:-none}) ==="
  podman ps -aq --filter name=qwen38 | xargs -r podman rm -f >/dev/null 2>&1
  # Give the old container's grace period time to finish: start.sh refuses a name that is still held, and a
  # fixed sleep left the first cell of a run aborting on a shutdown that had nothing to do with the test.
  for _w in $(seq 1 30); do
    podman ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "${CONTAINER_NAME:-qwen38-exl3-ple8}" && { sleep 1; continue; }
    break
  done

  # Start every cell from the same cold state. Without this, the second cell inherits a warm cache from the
  # first and reads as "needs less RAM" for a reason that has nothing to do with its cap.
  echo "  --- dropping host page cache ---"
  local drop_rc=0 drop_out
  drop_out=$(sudo -n ple-drop-caches 2>&1) || drop_rc=$?
  printf '%s\n' "$drop_out" | sed 's/^/    /'
  (( drop_rc == 0 )) || echo "    (drop_caches rc=${drop_rc}: this cell may inherit a warm cache from the previous one)"

  local t0=$(( $(date +%s) ))
  # shellcheck disable=SC2086
  if ! env IMAGE="$IMG" QUANT=exl3-ple8 ${cap:+MEMORY="$cap"} $envs bash "$REPO/start.sh" > "$OUT/$name-start.log" 2>&1; then
    echo "  start.sh FAILED:"; tail -8 "$OUT/$name-start.log" | sed 's/^/    /'; return 1
  fi
  local ctr code=000 i
  ctr=$(podman ps --format '{{.Names}}' | tail -1)
  for i in $(seq 1 120); do
    code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$PORT/health" 2>/dev/null)
    [ "$code" = "200" ] && break
    sleep 10
  done
  local health=$(( $(date +%s) - t0 ))
  [ "$code" = "200" ] || { echo "  never healthy within ${health}s"; podman logs --tail 15 "$ctr" 2>&1 | sed 's/^/    /'; return 1; }
  echo "  healthy after ${health}s   container: $ctr"
  echo "  host after boot: $(memline)"
  echo "    cap inside container: $(podman exec "$ctr" sh -c 'cat /sys/fs/cgroup/memory.max' 2>/dev/null)"
  echo "    preflight said: $(grep -h 'memory headroom' "$OUT/$name-start.log" | sed 's/^ *//')"
  podman logs "$ctr" > "$OUT/$name.log" 2>&1

  cgstat "$ctr" > "$OUT/$name-cg-before.txt"
  python3 -I "$REPO/scripts/ple-probe.py" --reps 3 --warmup 2 > "$OUT/$name-perf.txt" 2>&1
  cgstat "$ctr" > "$OUT/$name-cg-after.txt"

  echo "  --- cgroup during the drive (before -> after) ---"
  echo "    before: $(cat "$OUT/$name-cg-before.txt")"
  echo "    after:  $(cat "$OUT/$name-cg-after.txt")"

  echo "  --- determinism and throughput ---"
  local h1
  h1=$(awk '$1=="strict"{print $2}' "$OUT/$name-perf.txt" | head -1)
  echo "    strict hash: ${h1:-none}   $([ -z "$REF" ] && echo '(first cell sets the reference)' || { [ "$h1" = "$REF" ] && echo IDENTICAL || echo DIFFERS; })"
  [ -z "$REF" ] && REF="$h1"
  grep -E "^(prefill|decode) " "$OUT/$name-perf.txt" | sed 's/^/    /'

  echo "  --- staging phases ---"
  podman logs "$ctr" > "$OUT/$name.log" 2>&1
  local stats
  stats=$(grep -h "PLE stream v2 stats" "$OUT/$name.log" | tail -1)
  echo "    ${stats:-(no stats line: pinned cell has no staging)}"
  echo "    staging failures: $(grep -ciE 'staging failed' "$OUT/$name.log")   skips: $(grep -hoE 'skips=[0-9]+' "$OUT/$name.log" | tail -1)"
  echo "  --- at rest with the engine loaded ---"
  echo "    $(memline)"
  podman stop -t 30 "$ctr" >/dev/null 2>&1
  return 0
}

# Reference anchor: whatever this host does when nothing is tight. Everything below is judged against it.
cell uncapped-stream "" "PLE_PIN_STRATEGY=exact PLE_STREAM_V2=1"

for cap in $CAPS; do
  cell "stream-${cap}g" "$cap" "PLE_PIN_STRATEGY=exact PLE_STREAM_V2=1"
done

# The pinned arms, at the same caps. These are expected to be refused by the preflight or to OOM, and that is
# the survival argument for streaming in one line per cap.
for cap in $CAPS; do
  cell "pinned-${cap}g" "$cap" "PLE_PIN_STRATEGY=round2"
done

echo
echo "=== summary: throughput vs page-cache budget (streaming) ==="
for name in uncapped-stream; do
  printf '  %-18s %s\n' "$name" "$(grep -hE '^(prefill|decode) ' "$OUT/$name-perf.txt" 2>/dev/null | tr '\n' ' ')"
done
for cap in $CAPS; do
  printf '  %-18s %s\n' "stream-${cap}g" "$(grep -hE '^(prefill|decode) ' "$OUT/stream-${cap}g-perf.txt" 2>/dev/null | tr '\n' ' ')"
done
echo "DONE"

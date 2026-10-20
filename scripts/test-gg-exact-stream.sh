#!/bin/bash
# Pin-size x streaming check for the `gg` layout: Fedora, rootless podman, ONE GPU, ~126 GiB RAM.
#
# Three cells, one variable changing at a time:
#   pinned-round2    the shipped default -- pinned table, size rounded up to a power of two
#   pinned-exact     same engine, pin allocated at the table's actual size
#   stream-v2-exact  no table-sized pin at all; rows are staged from the checkpoint per step
#
# Why this host: the host-side numbers here are tighter than burst's. `gg` has ~126 GiB and the FP8 table
# is 47.7 GiB, so round2 (64 GiB) versus exact (47.7 GiB) is visible in MemAvailable, and streaming should
# show a further drop to a few GiB of arenas. That is the claim this driver exists to check on hardware
# where the choice actually constrains what else fits.
#
# The hash is gated across cells: all three must agree byte-for-byte, since pin size and staging strategy
# are not supposed to change what the model computes. REF is taken from the first cell when not given, so
# the comparison is against this host's own baseline rather than a copied constant.
#
# Run: bash scripts/test-gg-exact-stream.sh            (all three cells)
#      CELLS=pinned-exact bash scripts/test-gg-exact-stream.sh
set -uo pipefail
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1
IMG="${IMG:-localhost/qwen38-rtx:exp3}"
OUT="${OUT:-$HOME/gg-exp3}"; mkdir -p "$OUT"
REF="${REF:-}"
PORT="${PORT:-8001}"

memline() {   # host-wide numbers, in GiB, from /proc/meminfo
  awk '/^MemAvailable:/ {a=$2/1048576} /^Shmem:/ {s=$2/1048576} END {printf "MemAvailable %.2f GiB, Shmem %.2f GiB", a, s}' /proc/meminfo
}

cleanup() {
  podman ps -aq --filter name=qwen38 | xargs -r podman rm -f >/dev/null 2>&1
  echo "  [cleanup] containers removed; $(memline)"
}
trap cleanup EXIT INT TERM

cell() {   # name  extra-env-string  [extra start.sh args]
  local name=$1 envs=$2 extra_args=${3:-}
  echo
  echo "=== cell $name ==="
  podman ps -aq --filter name=qwen38 | xargs -r podman rm -f >/dev/null 2>&1
  # Wait for the name to actually be released. `podman rm -f` returns while the container is still in its
  # grace-period shutdown, and start.sh refuses a name that is still held -- "is already stopping, run ./stop.sh
  # first" -- so a fixed short sleep made the FIRST cell of a run abort for no reason related to what was under
  # test. Bounded, and reported if it ever times out.
  for _w in $(seq 1 30); do
    podman ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$name" && { sleep 1; continue; }
    break
  done
  echo "  before boot: $(memline)"
  # shellcheck disable=SC2086
  if ! env REPLACE=1 IMAGE="$IMG" QUANT=exl3-ple8 $envs bash "$REPO/start.sh" $extra_args > "$OUT/$name-start.log" 2>&1; then
    echo "  start.sh FAILED:"; tail -6 "$OUT/$name-start.log" | sed 's/^/    /'; return 1
  fi
  local ctr code=000
  ctr=$(podman ps --format '{{.Names}}' | tail -1)
  echo "  container: $ctr"
  for i in $(seq 1 120); do
    code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$PORT/health" 2>/dev/null)
    [ "$code" = "200" ] && { echo "  HEALTHY after $((i * 10))s"; break; }
    sleep 10
  done
  [ "$code" = "200" ] || { echo "  never healthy"; podman logs --tail 15 "$ctr" 2>&1 | sed 's/^/    /'; return 1; }
  podman logs "$ctr" > "$OUT/$name.log" 2>&1   # boot-time snapshot; refreshed after the probes below

  echo "  --- after boot ---"
  echo "    $(memline)"
  echo "  --- what the container sees ---"
  podman exec "$ctr" nvidia-smi --query-gpu=index,memory.used,power.limit --format=csv,noheader \
    | sed 's/^/    /'
  echo "    devices visible: $(podman exec "$ctr" nvidia-smi --query-gpu=index --format=csv,noheader | wc -l)"
  echo "  --- env as seen inside the container ---"
  podman exec "$ctr" sh -c 'env | grep -E "^PLE_" | sort' 2>/dev/null | sed 's/^/    /'
  echo "  --- backend lines ---"
  grep -m1 -iE "PLE pinned host memory|pinned at exact size|PLE stream v2: staging" "$OUT/$name.log" \
    | cut -c1-150 | sed 's/^/    /'
  grep -m1 -iE "cudagraph_mode" "$OUT/$name.log" | grep -oE "CUDAGraphMode\.[A-Z_]+" | head -1 | sed 's/^/    /'
  echo "    capture-copy errors: $(grep -ciE 'Cannot copy between CPU and CUDA' "$OUT/$name.log")"
  echo "    staging failures:    $(grep -ciE 'staging failed' "$OUT/$name.log")"

  echo "  --- greedy determinism, twice ---"
  local h1 h2
  h1=$(python3 -I "$REPO/scripts/ple-probe.py" --reps 2 --warmup 1 2>&1 | awk '$1=="strict"{print $2}')
  h2=$(python3 -I "$REPO/scripts/ple-probe.py" --reps 2 --warmup 1 2>&1 | awk '$1=="strict"{print $2}')
  # ERROR agreeing with ERROR is not determinism; require the hex digest before calling it consistent.
  echo "    served model: $(awk '$1=="model"{print $2}' "$OUT/$name-perf.txt" | head -1)"
  echo "    strict hashes: $h1 / $h2   $(printf "%s" "$h1" | grep -qE "^[0-9a-f]{16}$" && [ "$h1" = "$h2" ] && echo SELF-CONSISTENT || echo VARIES)"
  if [ -z "$REF" ]; then REF="$h1"; echo "    (reference taken from this cell)"; else
    echo "    vs reference $REF: $([ "$h1" = "$REF" ] && echo IDENTICAL || echo DIFFERS)"
  fi
  ( python3 -I "$REPO/scripts/ple-probe.py" --reps 3 --warmup 2 > "$OUT/$name-perf.txt" 2>&1 )
  grep -E "^(prefill|decode) " "$OUT/$name-perf.txt" | sed 's/^/    /'

  # Re-snapshot AFTER the probes: the boot-time snapshot cannot contain anything the probes provoked,
  # so grepping it only ever asserted that booting was clean.
  podman logs "$ctr" > "$OUT/$name.log" 2>&1
  echo "  --- after the drive (log re-snapshotted) ---"
  grep -h "PLE stream v2 stats" "$OUT/$name.log" | tail -2 | sed 's/^/    /'
  echo "    capture-copy errors: $(grep -ciE 'Cannot copy between CPU and CUDA' "$OUT/$name.log")"
  echo "    staging failures:    $(grep -ciE 'staging failed' "$OUT/$name.log")"
  echo "    staging skips:       $(grep -hoE 'skips=[0-9]+' "$OUT/$name.log" | tail -1)"
  echo "  --- at rest with the engine loaded ---"
  echo "    $(memline)"
  podman stop -t 30 "$ctr" >/dev/null 2>&1
  return 0
}

run() { [[ -n "${CELLS:-}" && ",${CELLS}," != *",$1,"* ]] || cell "$@"; }

run pinned-round2   "PLE_PIN_STRATEGY=round2"
run pinned-exact    "PLE_PIN_STRATEGY=exact"
run stream-v2-exact "PLE_PIN_STRATEGY=exact PLE_STREAM_V2=1"

echo
echo "=== summary ==="
echo "  reference hash: $REF"
grep -E "^(prefill|decode) " "$OUT"/*-perf.txt 2>/dev/null | sed 's/^/    /'
echo "DONE"

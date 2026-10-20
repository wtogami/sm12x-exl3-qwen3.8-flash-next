#!/bin/bash
# Qualify PLE_PIN_STRATEGY=exact on the BF16 PLE profile, at TP1 and TP2.
#
# Why BF16 needs its own run: the saving from `exact` is the power-of-two rounding of one pinned allocation,
# and that rounding is proportionally largest on the largest table. BF16 is 95.368 GiB of table, so round2
# costs 128 GiB pinned against 96.55 GiB actually needed -- about 31 GiB back, roughly double what the FP8
# profile recovers. BF16 is also the profile where the ceiling matters most, because a host with ~128 GiB RAM
# has the round2 figure and nothing else left for page cache.
#
# Why both TP widths, for different reasons:
#   * TP1 is the shipping mode for this profile, so equivalence and throughput are measured there.
#   * TP2 exercises arithmetic TP1 cannot reach. At tp=2 each rank locks ceil(size/2) at its own offset, so
#     the per-rank size and the base offset both vary; a wrong offset is exactly the kind of bug that reads as
#     a healthy boot and a subtly wrong gather. The strict hash catches offset errors that survive loading.
# Each width is compared against its OWN round2 reference, because tensor width changes greedy output here
# (TP1 and TP2 hashes are known to differ), so cross-width comparison would be a false failure.
#
# What is asserted per cell: the pin strategy the container actually received; the loader's own printed pin
# size; host Shmem as a delta over a pre-boot sample (Shmem in /proc/meminfo is host-wide and wobbles); the
# graph regime, which must stay FULL_AND_PIECEWISE since nothing here should touch it; strict hash
# self-consistency plus agreement with the same-width reference; and prefill/decode throughput.
#
# Power envelope is pinned at 300 W per card for the same reason the TP driver does it: uncapped TP1 gets a
# whole card's watts while TP2 splits two capped ones, and then width and watts are the same variable.
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
IMG="${IMG:-localhost/qwen38-rtx:exp3}"
OUT="${OUT:-$HOME/exp3-test}"; mkdir -p "$OUT"
cd "$REPO" || exit 1

# Expected host demand, computed the way the gate does: tp x round2(per-rank share), and tp x ceil(per-rank
# share) for exact. BF16 table = rows x dim x 2 bytes; token embedding = vocab x hidden x 2 bytes.
expect() {  # $1 = tp  $2 = strategy
  python3 - "$1" "$2" <<'PY'
def next_pow2(x):
    p = 1.0
    while p < x:
        p *= 2
    return p
import math, sys
tp, strategy = int(sys.argv[1]), sys.argv[2]
table = 320001536 * 160 * 2 / 2**30
embed = 248320 * 2560 * 2 / 2**30
rank_t, rank_e = math.ceil(table / tp), math.ceil(embed / tp)
pin_t = rank_t * tp if strategy == "exact" else next_pow2(rank_t) * tp
pin_e = next_pow2(rank_e) * tp            # the embedding always goes through pin_memory
print(f"{pin_t + pin_e:.2f}")
PY
}

cleanup() {
  podman ps -aq --filter name=qwen38 | xargs -r podman rm -f >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

echo "=== power envelope (attempted cap; the printed line is what actually applies) ==="
sudo -n nvidia-smi -pl 300 -i 0 >/dev/null 2>&1
sudo -n nvidia-smi -pl 300 -i 1 >/dev/null 2>&1
nvidia-smi --query-gpu=index,pci.bus_id,power.limit,memory.used --format=csv,noheader | sed 's/^/  /'

cell() {   # name  extra-env-string  [want_devices]
  local name=$1 envs=$2 want=${3:-1}
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
  local shmem_before
  shmem_before=$(awk '/^Shmem:/ {print $2}' /proc/meminfo)
  # shellcheck disable=SC2086
  if ! env REPLACE=1 IMAGE="$IMG" QUANT=exl3 $envs bash "$REPO/start.sh" > "$OUT/$name-start.log" 2>&1; then
    echo "  start.sh FAILED:"; tail -8 "$OUT/$name-start.log" | sed 's/^/    /'; return 1
  fi
  local ctr code=000 i
  ctr=$(podman ps --format '{{.Names}}' | tail -1)
  echo "  container: $ctr"
  for i in $(seq 1 120); do
    code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' http://127.0.0.1:8001/health 2>/dev/null)
    [ "$code" = "200" ] && { echo "  HEALTHY after $((i * 10))s"; break; }
    sleep 10
  done
  [ "$code" = "200" ] || { echo "  never healthy"; podman logs --tail 20 "$ctr" 2>&1 | sed 's/^/    /'; return 1; }
  podman logs "$ctr" > "$OUT/$name.log" 2>&1

  echo "  --- devices / topology ---"
  podman exec "$ctr" nvidia-smi --query-gpu=index,pci.bus_id,power.limit,memory.used --format=csv,noheader | sed 's/^/    /'
  local n
  n=$(podman exec "$ctr" nvidia-smi --query-gpu=index --format=csv,noheader | wc -l)
  echo "    devices visible: $n (want $want)   $([ "$n" = "$want" ] && echo PASS || echo FAIL)"
  echo "  --- env as seen inside the container ---"
  podman exec "$ctr" sh -c 'env | grep -E "^PLE_" | sort' 2>/dev/null | sed 's/^/    /'

  echo "  --- memory ---"
  awk -v before="$shmem_before" '/^Shmem:/ {printf "    Shmem %.2f GiB (+%.2f over pre-boot)\n", $2/1048576, ($2-before)/1048576}' /proc/meminfo
  local tp_strat tp want_total
  tp_strat=$(podman exec "$ctr" sh -c 'echo "${PLE_PIN_STRATEGY:-unset}"' 2>/dev/null)
  tp=$(podman exec "$ctr" sh -c 'nvidia-smi --query-gpu=index --format=csv,noheader | wc -l' 2>/dev/null)
  want_total=$(expect "$tp" "$tp_strat")
  echo "    predicted demand for strategy=${tp_strat} at tp=${tp}: ${want_total} GiB"

  echo "  --- backend lines ---"
  grep -m2 -iE "pinned at exact size|PLE pinned host memory|[0-9.]+ GiB of pinned" "$OUT/$name.log" \
    | cut -c1-150 | sed 's/^/    /'
  grep -m1 -oE "CUDAGraphMode\.[A-Z_]+" "$OUT/$name.log" | head -1 | sed 's/^/    graph mode: /'

  echo "  --- greedy determinism, twice ---"
  local h1 h2 partner out1="$OUT/$name-h1.txt" out2="$OUT/$name-h2.txt"
  ( cd /tmp && python3 -I "$REPO/scripts/ple-probe.py" --reps 2 --warmup 1 > "$out1" 2>&1 )
  ( cd /tmp && python3 -I "$REPO/scripts/ple-probe.py" --reps 2 --warmup 1 > "$out2" 2>&1 )
  h1=$(awk '$1=="strict"{print $2}' "$out1" | head -1)
  h2=$(awk '$1=="strict"{print $2}' "$out2" | head -1)
  echo "    served model: $(awk '$1=="model"{print $2}' "$out1" | head -1)"
  echo "    strict hashes: $h1 / $h2   $([ -n "$h1" ] && [ "$h1" = "$h2" ] && echo SELF-CONSISTENT || echo VARIES)"
  # Pairing is same-width only. Tensor width changes greedy output on this build, and the BF16 checkpoint is a
  # different checkpoint from the FP8 one, so neither a TP width nor a quant variant crosses over: each exact
  # cell is judged against its own width's round2 cell from this same run.
  # "ERROR / ERROR" agrees with itself and proves nothing, so the gate requires a hex digest.
  if ! printf "%s" "$h1" | grep -qE "^[0-9a-f]{16}$"; then
    echo "    FAIL: no strict hash -- the probe did not complete"; tail -4 "$out1" | sed 's/^/      /'
    podman stop -t 30 "$ctr" >/dev/null 2>&1; podman rm -f "$ctr" >/dev/null 2>&1; return 1
  fi
  partner="${name%-*}-round2"
  if [ -s "$OUT/$partner-hash.txt" ]; then
    echo "    vs same-width round2 ($(cat "$OUT/$partner-hash.txt")): $([ "$h1" = "$(cat "$OUT/$partner-hash.txt")" ] && echo IDENTICAL || echo DIFFERS)"
  fi
  printf '%s\n' "$h1" > "$OUT/$name-hash.txt"

  echo "  --- throughput ---"
  ( cd /tmp && python3 -I "$REPO/scripts/ple-probe.py" --reps 3 --warmup 2 > "$OUT/$name-perf.txt" 2>&1 )
  grep -E "^(prefill|decode) " "$OUT/$name-perf.txt" | sed 's/^/    /'

  podman logs "$ctr" > "$OUT/$name.log" 2>&1
  echo "  --- after the drive (log re-snapshotted) ---"
  echo "    capture-copy errors: $(grep -ciE 'Cannot copy between CPU and CUDA' "$OUT/$name.log")"
  echo "    staging failures:    $(grep -ciE 'staging failed' "$OUT/$name.log")"
  echo "    load errors:         $(grep -ciE 'does not match|unexpected shape|expected [0-9]+, got' "$OUT/$name.log")"
  podman stop -t 30 "$ctr" >/dev/null 2>&1
  podman rm -f "$ctr" >/dev/null 2>&1
}

# TP1 first: shipping mode for this profile, and the pair that answers "does exact change anything an
# operator would notice". TP2 then exercises per-rank offset arithmetic that TP1 cannot reach.
run() { [[ -n "${CELLS:-}" && ",${CELLS}," != *",$1,"* ]] || cell "$@"; }
run tp1-round2 "GPU=${GPU_SEL:-1} TP_SIZE=1 PLE_PIN_STRATEGY=round2" 1
run tp1-exact  "GPU=${GPU_SEL:-1} TP_SIZE=1 PLE_PIN_STRATEGY=exact" 1
run tp2-round2 "TP_SIZE=2 PLE_PIN_STRATEGY=round2" 2
run tp2-exact  "TP_SIZE=2 PLE_PIN_STRATEGY=exact" 2

echo
echo "=== summary (BF16 profile, pinned; envelope as observed above, not assumed) ==="
for name in tp1-round2 tp1-exact tp2-round2 tp2-exact; do
  if [ -s "$OUT/$name-perf.txt" ]; then
    printf '  %-12s %s\n' "$name" "$(grep -hE '^(prefill|decode) ' "$OUT/$name-perf.txt" | sed 's/ (prompt.*//' | tr '\n' ' ')"
  fi
done
echo "DONE"

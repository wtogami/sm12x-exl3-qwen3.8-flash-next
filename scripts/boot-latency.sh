#!/usr/bin/env bash
# Boot-latency probe: how long from "start.sh" to "serving", as a function of what the host
# page cache holds when the boot begins.
#
# WHY THIS EXISTS, AND WHY IT IS DELIBERATELY NARROW.
# This started life as a safety item -- "cold page-cache boot is unmeasured, and 0.98 leaves only
# ~1.4 GiB of headroom" -- and it is not one. Free-at-startup is a DEVICE quantity (VRAM total
# minus what other processes hold on the GPU); the page cache is host RAM. The two were already
# decorrelated by 22 burst boots whose device free-at-startup read 94.50/94.51 GiB while host-side
# available memory at those moments ranged 662 -> 893 GiB: a 231 GiB swing, no device-side
# response. What remains is an operational number -- how long a restart takes -- and one boot
# answers it.
#
# Also measured, on the RIGHT checkpoint directory (the exl3 profile's snapshot, 168 GiB across 37
# files): 43,827,417 of 43,829,883 pages resident -- 100%. The weight files are therefore fully warm
# on this host, which means every boot in this campaign has been a WARM-cache boot and the cold case
# really is unmeasured. `cold` is the informative arm; `asis` reproduces the historical number.
#
# Two artifacts to avoid here, both of which produced a confidently wrong "it is already cold":
#   * Measuring the wrong model. This host holds several checkpoints; the 96 GiB nvidia/...FP4-Local
#     directory is the nvfp4 profile, not this recipe. Resolve the path by sourcing model-profiles.sh.
#   * Running vmtouch on a Hugging Face snapshot directory WITHOUT -f. Snapshots are directories of
#     symlinks into ../blobs, so without -f vmtouch reports the link entries and returns ~0% resident
#     for a checkpoint that is entirely in RAM. Same shape as the fadvise story in
#     docs/ssd-stream-findings.md: an eviction/residency call that reports success while measuring
#     nothing. Everything below passes -f.
#
# Note this is still only an operational number. Cold cache cannot change free-at-startup: that is a
# DEVICE quantity, and 22 burst boots read 94.50/95.06 GiB device-free while host-side available
# memory ranged 662 -> 893 GiB. Cold cache changes how long a restart takes, not how much VRAM is
# left. drop_caches also cannot un-pin the PLE table (host Shmem was 195.7 GiB with two engines up,
# ~98 GiB each, matching the BF16 table's 95.368 GiB) -- level 1 frees clean unmapped page cache only,
# which is why ple-drop-caches prints Shmem before and after as proof it did not disturb a live pin.
#
# MODES:
#   warm   populate the checkpoint files into the page cache first (vmtouch -t), assert it worked,
#          then boot. If the loader does not care about the cache, warm ~= asis and that is the
#          answer.
#   cold   ask the host to drop the page cache first (sudo ple-drop-caches; it fails closed while
#          an engine is busy), assert residency actually fell, then boot. Note it CANNOT drop
#          shmem, so it cannot un-pin the table -- it only re-colds the files.
#   asis   no intervention; reproduces what every past boot did.
#
# Residency is asserted, never assumed: vmtouch reports percentages, and the eviction primitives
# here return success whether or not they evicted anything (see docs/ssd-stream-findings.md:2).
# A boot whose cache state was not verified is labelled UNVERIFIED rather than reported as a
# clean cold or warm sample.
#
#   GPU=2 MODES="cold asis" OUT=/home/opencode/burst-bootlat-$(date +%Y%m%d) bash scripts/boot-latency.sh
set -uo pipefail
cd "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f start.sh && -f stop.sh ]] || { echo "run this from <repo>/scripts" >&2; exit 2; }

QUANT="${QUANT:-exl3}"
MODEL="${MODEL:-qwen38-$QUANT}"
IMG="${IMAGE:-qwen38-rtx:local}"
GPU="${GPU:?set GPU to one index; this probe holds a whole card}"
[[ "$GPU" =~ ^[0-9]+$ ]] || { echo "GPU=$GPU must be a single index" >&2; exit 2; }
PORT="${PORT:-8311}"
NAME="${CONTAINER_NAME:-qwen38-bootlat}"
UTIL="${GPU_MEMORY_UTILIZATION:-0.98}"
MODES="${MODES:-warm asis}"
OUT="${OUT:?set OUT to a per-campaign directory}"; mkdir -p "$OUT"
B=http://127.0.0.1:"$PORT"
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
DROPCACHES="${DROPCACHES:-sudo -n /usr/local/bin/ple-drop-caches}"

# Resolve the checkpoint the way the launcher does -- by sourcing the recipe's own profile table,
# not by re-deriving it here. The first version of this script grepped MODEL_CACHE_NAME out of
# start.sh, where it is only ever *used*; the definition lives in model-profiles.sh. A grep that
# misses produces an empty path, and an empty path under a hub root silently means "measured every
# model on the host" -- which is how a 96 GiB nvfp4 blob directory got quoted as this recipe's
# weights. Target the snapshot revision, with -f to follow the symlinks, so the file set is exactly
# the one the boot reads.
source ./model-profiles.sh
MODEL_DIR="$HF_CACHE/hub/$MODEL_CACHE_NAME/snapshots/$MODEL_REVISION"
[[ -d "$MODEL_DIR" ]] || { echo "no such checkpoint snapshot: $MODEL_DIR" >&2; exit 2; }

command -v vmtouch >/dev/null || { echo "vmtouch is required to verify cache state" >&2; exit 2; }

# Refuse rather than perturb: this probe writes to the host page cache, which is shared by every
# engine on the machine, and it wants the whole card to itself.
if pgrep -f "[v]llm serve" >/dev/null 2>&1 || pgrep -f "[v]llm.entrypoints" >/dev/null 2>&1; then
  echo "an engine is running on this host; the page-cache intervention would disturb it." >&2
  echo "run this only when nothing is serving. Holders:" >&2
  pgrep -af "[v]llm serve" | head -3 | sed 's/^/  /' >&2
  exit 2
fi
used=$(nvidia-smi --id="$GPU" --query-gpu=memory.used --format=csv,noheader,nounits)
(( used < 256 )) || { echo "GPU $GPU holds $used MiB; not exclusively free" >&2; exit 2; }

ROW="$OUT/bootlat.tsv"; [[ -e "$ROW" ]] || printf "mode\tresidency_pct_before\tresident_before\tresident_total\tready_seconds\tmodel_load_seconds\tkv_alloc_seconds\tfree_at_startup_GiB\tcached_before_GiB\tcached_after_GiB\tnote\n" >> "$ROW"

residency() {   # -> "<pct>\t<resident>\t<total>" for the checkpoint directory
  vmtouch -f "$MODEL_DIR" 2>/dev/null | awk '
    /^ *Resident Pages/ { pct=$NF; sub(/%/,"",pct); print pct "\t" $4 "\t" $6 }'
}
cached_gib() { awk -v k="$1:" '$1==k {printf "%.1f", $2/1048576}' /proc/meminfo Cached; }

for mode in $MODES; do
  echo "=== mode: $mode ==="
  note=""
  case "$mode" in
    warm)
      echo "  populating the checkpoint into the page cache (this reads ~96 GiB once)"
      vmtouch -t -f "$MODEL_DIR" > "$OUT/vmtouch-warm.log" 2>&1 || echo "  vmtouch -t failed" | tee -a "$OUT/vmtouch-warm.log"
      ;;
    cold)
      $DROPCACHES > "$OUT/dropcaches.log" 2>&1 || { echo "  drop_caches refused (see $OUT/dropcaches.log)"; note="drop-refused"; }
      ;;
    asis) : ;;
    *) echo "unknown mode: $mode"; exit 2 ;;
  esac

  IFS=$'\t' read -r rpct rb rtotal < <(residency)
  cbefore=$(cached_gib)
  echo "  checkpoint residency before boot: ${rpct:-NA}% (${rb:-?}/${rtotal:-?}) | host Cached ${cbefore} GiB"
  case "$mode" in
    warm) awk -v p="${rpct:-0}" 'BEGIN{ exit !(p+0 >= 50) }' || note="${note}residency=${rpct:-NA}%<50:UNVERIFIED " ;;
    cold) awk -v p="${rpct:-0}" 'BEGIN{ exit !(p+0 <= 25) }'  || note="${note}residency=${rpct:-NA}%>25:UNVERIFIED " ;;
  esac

  # Timed from the launch call to the first successful /v1/models, at 1 s resolution: the queueing
  # and weight read are the part a cold cache can move, and start.sh returns before serving.
  t0=$(date +%s)
  QUANT="$QUANT" IMAGE="$IMG" GPU="$GPU" PORT="$PORT" CONTAINER_NAME="$NAME" \
    GPU_MEMORY_UTILIZATION="$UTIL" bash start.sh > "$OUT/start-$mode.log" 2>&1 &
  launcher=$!
  ready=0
  for _ in $(seq 1 1200); do
    curl -sf --max-time 4 "$B/v1/models" >/dev/null 2>&1 && { ready=1; break; }
    kill -0 "$launcher" 2>/dev/null || { sleep 2; curl -sf --max-time 4 "$B/v1/models" >/dev/null 2>&1 && { ready=1; break; }; echo "  launcher exited before serving"; break; }
    sleep 1
  done
  t1=$(date +%s)
  ready_s=$(( t1 - t0 ))
  (( ready )) || note="${note}BOOT-FAILED "

  docker logs "$NAME" > "$OUT/engine-$mode.log" 2>&1 || true
  load_s=$(grep -hoE "Model loading took [0-9.]+ GiB and [0-9.]+ seconds" "$OUT/engine-$mode.log" | tail -1 | grep -oE "and [0-9.]+ seconds" | grep -oE "[0-9.]+")
  kv_s=$(grep -hoE "init engine .* took [0-9.]+ seconds" "$OUT/engine-$mode.log" | tail -1 | grep -oE "[0-9.]+ second" | grep -oE "[0-9.]+")
  freeg=$(grep -hoE "Free memory on device \([0-9.]+/[0-9.]+ GiB\)" "$OUT/engine-$mode.log" | tail -1 | grep -oE "\([0-9.]+" | tr -d '(')
  cafter=$(cached_gib)
  printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
    "$mode" "${rpct:-NA}" "${rb:-NA}" "${rtotal:-NA}" "$ready_s" "${load_s:-NA}" "${kv_s:-NA}" "${freeg:-NA}" "$cbefore" "$cafter" "${note:-ok}" >> "$ROW"
  echo "  ready in ${ready_s}s | model load ${load_s:-NA}s | kv init ${kv_s:-NA}s | device free at startup ${freeg:-NA} GiB | Cached ${cbefore}->${cafter} GiB | ${note:-ok}"

  QUANT="$QUANT" IMAGE="$IMG" PORT="$PORT" CONTAINER_NAME="$NAME" bash stop.sh > "$OUT/stop-$mode.log" 2>&1 || true
  sleep 15
done

echo "=== boot-latency rows ==="; column -t -s$'\t' "$ROW" | cut -c1-150

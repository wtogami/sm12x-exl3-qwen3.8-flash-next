#!/usr/bin/env bash
# GPU_MEMORY_UTILIZATION ladder on gg: KV pool size vs boot-safety headroom.
# Default profile: exl3-ple8 (resident, pinned 49.7 GiB), LONGCTX default
# (524288 YaRN), MTP3 + CUDA graphs (recipe defaults).
#
# QUANT/IMAGE/PORT/OUT/UTILS are overridable because the ladder was written when
# exl3-ple8 was the only profile `gg` could host at all, and that is no longer
# true: with PLE_PIN_STRATEGY=exact (the launcher default since 2026-10-19) the
# BF16 profile pins 95.368 GiB and boots on this 125.68 GiB host, where round2
# rounded the same table to 128 GiB and could not. The headroom question now
# belongs to the profile `gg` actually serves, so run it as
#   QUANT=exl3 IMAGE=qwen38-rtx:local OUT=/home/rtx/util-ladder-exl3 \
#   UTILS="0.94 0.96 0.98 0.99" bash scripts/util-ladder.sh
# OUT must stay one-directory-per-profile: util-stress.sh reads results.tsv from
# the same directory, and a shared file would stress another profile's cells.
set -uo pipefail
# Repo-relative, not a hardcoded /home/rtx path: this driver runs on gg and on burst.
cd "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Fail fast if that cd did not land in a checkout. A copy placed outside the repo
# resolved to a directory with no start.sh, polled a port for 33 minutes, and its
# stop fallback SIGKILLed an engine loading weights on the same card.
[[ -f start.sh && -f stop.sh ]] || { echo "no start.sh/stop.sh above $(dirname -- "${BASH_SOURCE[0]}") -- run this file from <repo>/scripts/" >&2; exit 2; }
QUANT="${QUANT:-exl3-ple8}"
MODEL="${MODEL:-qwen38-$QUANT}"          # served alias, matches start.sh's default
NAME="${CONTAINER_NAME:-qwen38-$QUANT}"  # and so is the container name
PORT="${PORT:-8199}"
OUT="${OUT:-/home/rtx/util-ladder}"; mkdir -p "$OUT"
B=http://127.0.0.1:"$PORT"
IMG="${IMAGE:-qwen38-rtx:utiltest}"
UTILS="${UTILS:-0.94 0.95 0.96 0.97 0.98}"
BUILD_LOG="${BUILD_LOG:-/home/rtx/utiltest-build.log}"
row="$OUT/results.tsv"
[[ -e "$row" ]] || printf "util\tstatus\tweights_GiB\tkv_GiB\tkv_tokens\tcapture\tvram_free_MiB\tnote\n" > "$row"

wait_build() {
  docker image inspect "$IMG" >/dev/null 2>&1 && return 0
  for i in $(seq 1 120); do
    grep -q "Successfully tagged $IMG" "$BUILD_LOG" 2>/dev/null && return 0
    grep -qE "^Error:|^error:" "$BUILD_LOG" 2>/dev/null && return 1
    sleep 15
  done; return 1
}

# Never `docker rm -f` a *running* container: that is SIGTERM, 10s, SIGKILL. Fine
# when a driver owns the whole machine, but a misplaced copy of this driver killed a
# user's engine that was 86% through loading weights. Only stopped containers are
# removed; a live one under our name that stop.sh could not stop is someone else's
# work (or a wedged driver) and we refuse the cell loudly instead of ending it.
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

run_cell() {  # $1 = utilization
  local u="$1" log="$OUT/util-$u.log" w kvg kvt cap free status note pin host_avail
  stop_engine
  QUANT=$QUANT IMAGE=$IMG GPU=0 PORT=$PORT GPU_MEMORY_UTILIZATION=$u bash start.sh >>"$OUT/ops.log" 2>&1 &
  local spid=$! ready=0
  for i in $(seq 1 100); do
    curl -sf $B/v1/models >/dev/null 2>&1 && { ready=1; break; }
    kill -0 $spid 2>/dev/null || { [ -z "$(docker ps -q -f name=$NAME)" ] && [ -z "$(docker ps -aq -f name=$NAME -f status=running)" ] && break; }
    sleep 10
  done
  docker logs "$NAME" > "$log" 2>&1 || true
  # Host side belongs in the note, not in a new column: for exl3 the scarce
  # resource is the 95.368 GiB pin on a 125.68 GiB host, not VRAM, and a cell
  # that boots and then cannot be reloaded after a reboot is not a pass.
  host_avail=$(awk '/MemAvailable/{printf "%.1f", $2/1048576}' /proc/meminfo)
  pin=$(grep -oE "PLE weight pinned at exact size [0-9.]+ GiB|[0-9.]+ GiB of pinned host memory" "$log" | tail -1 | grep -oE "[0-9.]+ GiB" | head -1)
  if [[ $ready == 1 ]]; then
    python3 scripts/test-context-retrieval.py --base-url $B/v1 --model "$MODEL" --filler-tokens 8000 --output "$OUT/probe-$u.json" >>"$log" 2>&1 && note="probe:pass" || note="probe:FAIL"
    free=$(docker exec "$NAME" true 2>/dev/null && nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -1)
    status=OK
  else
    status=FAILED; note=$(grep -iE "out of memory|CUDA error|No available memory|raise|Error" "$log" | tail -1 | cut -c1-90)
  fi
  note="$note host_avail=${host_avail}GiB pin=${pin:-unrecorded}"
  w=$(grep -oE "Model loading took [0-9.]+ [GT]iB" "$log" | tail -1 | grep -oE "[0-9.]+")
  kvg=$(grep -oE "Available KV cache memory: [0-9.]+ GiB" "$log" | tail -1 | grep -oE "[0-9.]+")
  kvt=$(grep -oE "GPU KV cache size: [0-9,]+ tokens" "$log" | tail -1 | grep -oE "[0-9,]+" | tr -d ',')
  cap=$(grep -iE "Graph capture|capturing|capture takes" "$log" | tail -1 | grep -oE "[0-9.]+ (GiB|s)" | tr '\n' '/' )
  printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" "$u" "$status" "$w" "$kvg" "$kvt" "$cap" "$free" "$note" >> "$row"
  stop_engine
}

wait_build || { echo "BUILD FAILED" >> "$row"; exit 1; }
for u in $UTILS; do
  grep -q "^$u\b" "$row" || run_cell "$u"
done
echo "LADDER DONE $(date -u +%FT%TZ)" >> "$row"

#!/usr/bin/env bash
# GPU_MEMORY_UTILIZATION ladder on gg: KV pool size vs boot-safety headroom.
# Profile: exl3-ple8 (resident, pinned 49.7 GiB), LONGCTX default (524288 YaRN),
# MTP3 + CUDA graphs (recipe defaults).
set -uo pipefail
cd /home/rtx/sm12x-exl3-qwen3.8-flash-next
OUT=/home/rtx/util-ladder; mkdir -p "$OUT"
B=http://127.0.0.1:8199
IMG=qwen38-rtx:utiltest
row="$OUT/results.tsv"
[[ -e "$row" ]] || printf "util\tstatus\tweights_GiB\tkv_GiB\tkv_tokens\tcapture\tvram_free_MiB\tnote\n" > "$row"

wait_build() {
  docker image inspect "$IMG" >/dev/null 2>&1 && return 0
  for i in $(seq 1 120); do
    grep -q "Successfully tagged $IMG" /home/rtx/utiltest-build.log 2>/dev/null && return 0
    grep -qE "^Error:|^error:" /home/rtx/utiltest-build.log 2>/dev/null && return 1
    sleep 15
  done; return 1
}

stop_engine() { QUANT=exl3-ple8 IMAGE=$IMG PORT=8199 bash stop.sh >>"$OUT/ops.log" 2>&1 || docker rm -f qwen38-exl3-ple8 >/dev/null 2>&1; sleep 5; }

run_cell() {  # $1 = utilization
  local u="$1" log="$OUT/util-$u.log" w kvg kvt cap free status note
  stop_engine
  QUANT=exl3-ple8 IMAGE=$IMG GPU=0 PORT=8199 GPU_MEMORY_UTILIZATION=$u bash start.sh >>"$OUT/ops.log" 2>&1 &
  local spid=$! ready=0
  for i in $(seq 1 100); do
    curl -sf $B/v1/models >/dev/null 2>&1 && { ready=1; break; }
    kill -0 $spid 2>/dev/null || { [ -z "$(docker ps -q -f name=qwen38-exl3-ple8)" ] && [ -z "$(docker ps -aq -f name=qwen38-exl3-ple8 -f status=running)" ] && break; }
    sleep 10
  done
  docker logs qwen38-exl3-ple8 > "$log" 2>&1 || true
  if [[ $ready == 1 ]]; then
    python3 scripts/test-context-retrieval.py --base-url $B/v1 --model qwen38-exl3-ple8 --filler-tokens 8000 --output "$OUT/probe-$u.json" >>"$log" 2>&1 && note="probe:pass" || note="probe:FAIL"
    free=$(docker exec qwen38-exl3-ple8 true 2>/dev/null && nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -1)
    status=OK
  else
    status=FAILED; note=$(grep -iE "out of memory|CUDA error|No available memory|raise|Error" "$log" | tail -1 | cut -c1-90)
  fi
  w=$(grep -oE "Model loading took [0-9.]+ [GT]iB" "$log" | tail -1 | grep -oE "[0-9.]+")
  kvg=$(grep -oE "Available KV cache memory: [0-9.]+ GiB" "$log" | tail -1 | grep -oE "[0-9.]+")
  kvt=$(grep -oE "GPU KV cache size: [0-9,]+ tokens" "$log" | tail -1 | grep -oE "[0-9,]+" | tr -d ',')
  cap=$(grep -iE "Graph capture|capturing|capture takes" "$log" | tail -1 | grep -oE "[0-9.]+ (GiB|s)" | tr '\n' '/' )
  printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" "$u" "$status" "$w" "$kvg" "$kvt" "$cap" "$free" "$note" >> "$row"
  stop_engine
}

wait_build || { echo "BUILD FAILED" >> "$row"; exit 1; }
for u in 0.94 0.95 0.96 0.97 0.98; do
  grep -q "^$u\b" "$row" || run_cell "$u"
done
echo "LADDER DONE $(date -u +%FT%TZ)" >> "$row"

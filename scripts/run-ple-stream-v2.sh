#!/bin/bash
# Run on gg. v2 acceptance: boot the staging backend with FULL cudagraphs left alone, check the
# pre-registered predictions (Shmem ~2.18 GiB, decode ~160 tok/s, strict hash a6cc8fed286b7071), and
# ALWAYS restore the serving container. MODE=sync|async selects the staging mode (async is known
# unsafe: a captured graph cannot wait on an externally recorded event). Measured result: 2.21 GiB,
# 150.5-150.7 tok/s decode, strict hash identical, zero staging failures.
# v2 acceptance: does staging before replay let FULL cudagraphs survive, and is the output still identical?
# Falsifiable predictions, written before the run:
#   decode ~160 tok/s (v1: 126.7)   Shmem ~2.18 GiB (v1: 4.44)   strict hash == a6cc8fed286b7071
set -uo pipefail
ACC=/home/rtx/accept
REF=a6cc8fed286b7071
cd /home/rtx/sm12x-exl3-qwen3.8-flash-next

restore() {
  local rc=$?
  podman logs ple-v2 > "$OUT/startup.log" 2>&1 || true
  podman inspect ple-v2 > "$OUT/inspect.json" 2>/dev/null || true
  echo "  [archived $(wc -l < "$OUT/startup.log" 2>/dev/null || echo 0) log lines]"
  podman rm -f ple-v2 >/dev/null 2>&1 || true
  podman start qwen38-exl3-ple8 >/dev/null 2>&1 || bash $ACC/run-restore.sh >/dev/null 2>&1
  local code=000
  for i in $(seq 1 120); do
    code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' http://127.0.0.1:8001/health 2>/dev/null)
    [ "$code" = "200" ] && break; sleep 10
  done
  sleep 15
  bash /home/rtx/greedy.sh > $OUT/greedy-restored.txt 2>&1
  echo "  [restore] health=$code greedy=$(sha256sum < $OUT/greedy-restored.txt | cut -c1-16) (want f91132436fb09e45)"
  return $rc
}
trap restore EXIT INT TERM

MODE="${MODE:-sync}"
OUT="$ACC/v2-$MODE"; mkdir -p "$OUT"
echo "  staging mode: $MODE"
python3 scripts/mk-cell-run.py --base-container qwen38-exl3-ple8 --name ple-v2 \
  --image localhost/qwen38-exactpin:stream --detach \
  --env PLE_STREAM=1 --env PLE_STREAM_V2=1 --env PLE_ROWCOPY_SO=/opt/qwen38/ple_rowcopy.so \
  --env PLE_STREAM_V2_MODE="$MODE" \
  > "$OUT/run.sh"
echo "  no breakable env:  $(grep -c VLLM_USE_BREAKABLE "$OUT/run.sh")"
echo "  no compilation cfg: $(grep -c compilation-config "$OUT/run.sh")"
podman stop -t 30 qwen38-exl3-ple8 >/dev/null; sleep 5
bash "$OUT/run.sh" >/dev/null || { echo "  run rejected"; exit 1; }
code=000
for i in $(seq 1 120); do
  [ -z "$(podman ps -q --filter name=ple-v2)" ] && { echo "  DIED at $((i*10))s"; podman logs --tail 25 ple-v2 2>&1 | tail -18 | sed 's/^/    /'; exit 1; }
  code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' http://127.0.0.1:8001/health 2>/dev/null)
  [ "$code" = "200" ] && { echo "  HEALTHY after $((i*10))s"; break; }
  sleep 10
done
[ "$code" = "200" ] || { echo "  never healthy"; exit 1; }

echo "=== graph regime actually in force ==="
podman logs ple-v2 2>&1 | grep -m1 -iE "cudagraph_mode|CompilationMode" | cut -c1-140 | sed 's/^/  /'
podman logs ple-v2 2>&1 | grep -ciE "Breakable CUDA graph enabled" | sed 's/^/  breakable enabled lines (expect 0): /'
echo "=== backend + staging ==="
podman logs ple-v2 2>&1 | grep -m1 "streaming backend selected" | cut -c1-140 | sed 's/^/  /'
podman logs ple-v2 2>&1 | grep -m1 "PLE stream v2: staging" | cut -c1-170 | sed 's/^/  /'
podman logs ple-v2 2>&1 | grep -icE "staging failed|zeroes rather than reusing" | sed 's/^/  staging failures logged: /'
echo "=== memory: prediction was ~2.18 GiB ==="
awk '/^Shmem:|^Cached:|^MemAvailable:/ {printf "  %-14s %8.2f GiB\n", $1, $2/1048576}' /proc/meminfo
nvidia-smi --query-gpu=memory.used --format=csv,noheader | sed 's/^/  vram /'
echo "=== correctness + throughput (predictions: strict match, decode ~160) ==="
cd /tmp && python3 -I /home/rtx/stage/ple-probe.py --expect $REF --reps 3 --warmup 2 2>&1 | tail -9 | sed 's/^/  /'
echo V2DONE

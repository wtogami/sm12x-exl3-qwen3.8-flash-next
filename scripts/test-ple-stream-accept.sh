#!/bin/bash
# Acceptance test for PLE_STREAM=1 against the pinned-host baseline.
#
# PRE-REGISTERED expectations (written before the streaming engine was ever started):
#   1. greedy output sha256 must EQUAL the pinned baseline (BASE_SHA below).
#      Byte-identical is the bar, not "similar"; the pinned backend is the reference implementation.
#   2. Shmem must fall from 66.18 GiB to ~18.5 GiB. The pinned FP8 table is 47.684 GiB of shmem-backed
#      page-locked memory; if Shmem stays high, the table is still pinned and the backend did not
#      switch, however the logs read.
#   3. The startup log must contain "checkpoint streaming backend selected".
#   4. VRAM should be within ~1 GiB of baseline (the table was never resident in VRAM).
#   5. Throughput within the measured noise band for the configuration being compared -- uncapped
#      pcache rates on this host vary 3-5%, so a small prefill difference is not a result.
#   6. On completion the engine MUST be restored to the pinned serving config and re-verified, per the
#      standing requirement that the host ends in its serving state.
set -uo pipefail
BASE_SHA="${BASE_SHA:-f91132436fb09e45}"     # sha256 prefix of greedy-pinned-run{1,2}.txt, 392 bytes
SHMEM_TABLE_GIB="${SHMEM_TABLE_GIB:-47.684}"

echo "=== 1. correctness: greedy output versus the pinned baseline ==="
bash /home/rtx/greedy.sh > /home/rtx/accept/greedy-stream.txt 2>&1
sha=$(sha256sum < /home/rtx/accept/greedy-stream.txt | cut -c1-16)
echo "  streaming: $(wc -c < /home/rtx/accept/greedy-stream.txt) bytes  sha $sha"
echo "  baseline : sha $BASE_SHA"
[ "$sha" = "$BASE_SHA" ] && echo "  PASS byte-identical" || { echo "  FAIL output differs"; diff /home/rtx/accept/greedy-pinned-run1.txt /home/rtx/accept/greedy-stream.txt | head -8 | sed 's/^/    /'; }

echo
echo "=== 2. memory: is the table still pinned? ==="
shmem=$(awk '/^Shmem:/{printf "%.2f", $2/1048576}' /proc/meminfo)
echo "  Shmem now: ${shmem} GiB   (baseline 66.18, expected ~$(awk -v s="$SHMEM_TABLE_GIB" 'BEGIN{printf "%.1f", 66.18-s}') with the table unpinned)"
awk -v s="$shmem" -v t="$SHMEM_TABLE_GIB" 'BEGIN{ if (s < 66.18 - t*0.8) print "  PASS table is not pinned"; else print "  FAIL table still pinned (or another big shmem user appeared: check /proc/*/smaps)" }'

echo
echo "=== 3. the backend actually selected ==="
eng="${CONTAINER:-qwen38-exl3-ple8}"
if podman logs "$eng" 2>&1 | grep -q "checkpoint streaming backend selected"; then
  echo "  PASS selection logged"
  podman logs "$eng" 2>&1 | grep -m1 "streaming backend selected" | cut -c1-140 | sed 's/^/    /'
else
  echo "  FAIL no selection line -- the port did not take or PLE_STREAM did not reach the container"
fi

echo
echo "=== 4. device memory ==="
echo "  (baseline 91992 MiB; the table was never VRAM-resident, so this should barely move)"
nvidia-smi --query-gpu=memory.used --format=csv,noheader | sed "s/^/  vram used: /"

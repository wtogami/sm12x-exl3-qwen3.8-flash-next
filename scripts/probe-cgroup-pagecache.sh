#!/bin/bash
# Run inside a container (podman run --memory=1g ... -v this:/p.sh:ro ... -c 'bash /p.sh').
# Establishes, by timing re-reads, that a cgroup memory cap does NOT reproduce a small host when the
# file's pages are already resident: pages are charged to whoever instantiates them, so a capped
# container gets 15 GB/s hits inside a 1 GiB limit and pays for only part of what it read. Any
# low-RAM measurement therefore needs forced freshness (private copy, or global drop_caches with the
# other reader stopped) plus an assertion that memory.stat file actually grew.
# Does a memory cap force misses the way a small host would? And is charging exclusive to the reader?
set -u
mapfile -t F < <(ls -S /usr/local/lib/python3.12/dist-packages/torch/lib/*.so /usr/local/cuda/lib64/libcublas.so* 2>/dev/null | head -4)
tot=0; for f in "${F[@]}"; do s=$(stat -c %s "$f" 2>/dev/null || echo 0); tot=$((tot+s)); done
awk -v t=$tot 'BEGIN{printf "  %d candidate files, %.2f GiB total\n", 4, t/2^30}'
cur() { awk '{printf "%.2f", $1/2^30}' /sys/fs/cgroup/memory.current; }
fil() { awk '$1=="file"{printf "%.2f", $2/2^30}' /sys/fs/cgroup/memory.stat; }
echo "  at entry: memory.current=$(cur) GiB  file=$(fil) GiB   (already-cached pages are NOT re-charged here)"
echo
echo "--- read far more than the cap ---"
for f in "${F[@]}"; do [ -f "$f" ] && cat "$f" > /dev/null; done
echo "  after bulk read: memory.current=$(cur) GiB  file=$(fil) GiB   memory.max=$(awk '{printf "%.2f", $1/2^30}' /sys/fs/cgroup/memory.max) GiB"
echo
echo "--- timing tells us whether a re-read hit or missed ---"
for f in "${F[@]}"; do
  [ -f "$f" ] || continue
  sz=$(stat -c %s "$f")
  t0=$(date +%s.%N); cat "$f" > /dev/null; t1=$(date +%s.%N)
  awk -v n="$(basename $f)" -v s=$sz -v a=$t0 -v b=$t1 'BEGIN{d=b-a; printf "  %-38s %6.0f MiB  re-read %5.2f s  %6.0f MiB/s  %s\n", n, s/2^20, d, s/2^20/d, (s/2^20/d > 2000 ? "HIT (cache)" : "MISS (device)")} '
done
echo
echo "  host-wide Cached (unchanged by any of this if reclaim is cgroup-scoped):"
awk '/^Cached:/ {printf "    Cached %.2f GiB\n", $2/1048576}' /proc/meminfo

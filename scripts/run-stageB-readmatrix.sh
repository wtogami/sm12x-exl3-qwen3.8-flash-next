#!/bin/bash
# Stage B-i: the access-method matrix on the real n-gram tensor.
#
#   modes    pcache (preadv + FADV_RANDOM)  |  odirect (O_DIRECT into page-aligned buffers)
#   caps     host, or a podman --memory limit standing in for a small-RAM machine
#   threads  submit-path scaling
#
# The caps are the point. This host has ~50-65 GiB spare, so an uncapped page-cache run
# says nothing about the hosts the feature exists for, and the drive's own DRAM cache will
# absorb a small region and report it as a hit. Every receipt therefore carries residency
# before/after (mincore) and device reads (diskstats), and anything that did not start cold
# or was served by the drive is visible as such rather than believed.
set -u
cd "$(dirname "$0")/.." || exit 1
IMG=${IMAGE:-localhost/qwen38-exactpin:local}
MODEL=${MODEL:-$(bash -c 'source ./platform-config.sh >/dev/null 2>&1; source ./model-profiles.sh >/dev/null 2>&1; echo "${MODEL_PATH:-}"' 2>/dev/null)}
TENSOR=${TENSOR:-ngram}
OUT=${OUT:-/home/rtx/stageB-$(date +%Y%m%d-%H%M)}
mkdir -p "$OUT"; echo "  receipts: $OUT"

# The engine's --model path is container-side and the snapshot dir is symlinks into blobs/,
# so resolve both before anything else: an empty mount source makes podman fail with
# "host directory cannot be empty", and a silent one makes the cell measure the wrong file.
MODEL=${MODEL:-$(podman inspect "$CONTAINER" --format '{{join .Args " "}}' 2>/dev/null \
  | grep -oE '/[^ "]*snapshots/[^ "]+' | head -1)}
if [ -n "${MODEL:-}" ] && [ ! -d "$MODEL" ]; then
  CONTAINER=${CONTAINER:-qwen38-exl3-ple8}
  RESOLVED=""
  while read -r src dst; do
    [ -n "$dst" ] || continue
    case "$MODEL" in "$dst"/*) cand="$src${MODEL#"$dst"}"; [ -d "$cand" ] && RESOLVED="$cand" ;; esac
  done < <(podman inspect -f '{{range .Mounts}}{{.Source}} {{.Destination}}{{"\n"}}{{end}}' "$CONTAINER")
  [ -n "$RESOLVED" ] && { echo "  mapped $MODEL -> $RESOLVED"; MODEL="$RESOLVED"; }
fi

echo "=== locate the blob to measure (headers only, no bulk reads) ==="
BLOB=${BLOB:-}
if [ -z "$BLOB" ]; then
  BLOB=$(find "${MODEL:-/nonexistent}" -maxdepth 1 -name '*.safetensors' -exec readlink -f {} + 2>/dev/null \
         | while read -r f; do printf '%s\t%s\n' "$(stat -c %s "$f")" "$f"; done \
         | sort -rn | head -1 | cut -f2-)
fi
if [ -z "$BLOB" ] || [ ! -f "$BLOB" ]; then
  echo "  FATAL: no blob resolved (BLOB='${BLOB:-}'); set BLOB= explicitly. Refusing to run."
  exit 1
fi
if false; then
  BLOB=$(python3 - "$MODEL" "$TENSOR" <<'PY'
import glob, os, sys
from importlib import util as u
sys.path.insert(0, "scripts")
spec = u.spec_from_file_location("rp", "scripts/bench-ple-readpath.py")
m = u.module_from_spec(spec); spec.loader.exec_module(m)
model = sys.argv[1] or os.path.expanduser("~/.cache/huggingface/hub")
files = glob.glob(os.path.join(model, "**", "*.safetensors"), recursive=True)
if not files:
    files = glob.glob(os.path.expanduser("~/.cache/huggingface/hub/**/model*.safetensors"), recursive=True)
for f in sorted(files, key=lambda p: -os.path.getsize(p)):
    info, err = m.safetensors_tensor(f, sys.argv[2])
    if info:
        print(f)
        print(f"  {info['name']} {info['dtype']} {info['shape']} "
              f"{(info['abs_end']-info['abs_start'])/2**30:.2f} GiB at byte {info['abs_start']:,}",
              file=sys.stderr)
        break
else:
    print("", end="")
PY
)
fi
true
echo "  blob: $BLOB"

wait_idle() {   # the device counters are host-wide, so a benchmark must not overlap a serving engine
  [ -n "${SKIP_IDLE:-}" ] && return 0
  for _ in $(seq 1 30); do
    n=$(curl -s -m 3 http://127.0.0.1:8001/metrics 2>/dev/null | \
        awk '/^vllm:num_requests_running/ {s+=$2} END {printf "%d", s+0}')
    [ -z "$n" ] && n=0
    [ "$n" = "0" ] && return 0
    echo "  engine busy ($n requests), waiting"; sleep 20
  done
  echo "  !! engine still busy; device counters would be shared. Aborting this cell."; return 1
}

run_host() {  # $1 mode $2 threads $3 tag
  wait_idle || return 1
  timeout 1800 python3 scripts/bench-ple-readpath.py --blob "$BLOB" --tensor "$TENSOR" \
    --mode "$1" --threads "$2" --tokens-per-step "${TOKS:-2048}" --steps "${STEPS:-150}" \
    --require-cold --evict-first --residency --dev "${DEV:-nvme0n1}" \
    --target-pages-per-s "${TARGET:-115700}" --min-working-set "$((16*2**30))" \
    --output "$OUT/$3.json" >"$OUT/$3.log" 2>&1
  echo "    rc=$?"
}

run_capped() {  # $1 mode $2 threads $3 gib $4 tag
  wait_idle || return 1
  timeout 2400 podman run --rm --memory "${3}g" --memory-swap "${3}g" \
    -v "$PWD/scripts/bench-ple-readpath.py:/work/bench.py:ro,Z" \
    # /proc/diskstats is not namespaced, so device counters inside the container are the
    # host's -- which is what we want here, but only because wait_idle() ran first.
    -v "$BLOB:/work/blob:ro,Z" \
    --entrypoint python3 "$IMG" /work/bench.py --blob /work/blob \
      --mode "$1" --threads "$2" --tokens-per-step "${TOKS:-2048}" --steps "${STEPS:-150}" \
      --require-cold \
      --evict-first --residency --dev "${DEV:-nvme0n1}" \
      --target-pages-per-s "${TARGET:-115700}" --min-working-set "$((16*2**30))" \
      --output /work/out.json >"$OUT/$3.log" 2>&1
  # The instrument writes its receipt into the container, where nothing is mounted
  # writable (SELinux refuses directory bind-mounts on gg), so the receipt is taken from
  # stdout -- the script prints the same JSON it writes. No stray second container run.
  sed -n '/^{$/,/^}$/p' "$OUT/$3.log" > "$OUT/$3.json" || true
  echo "    rc=$?  (parsed $(wc -c < "$OUT/$3.json" 2>/dev/null) bytes of json)"
}

: > "$OUT/cells.txt"
for th in ${THREADS:-4 8 12 16}; do
  for mode in pcache odirect; do
    tag="${mode}-t${th}-host"; echo "--- $tag ---"
    run_host "$mode" "$th" "$tag"; echo "$tag" >> "$OUT/cells.txt"
    for cap in ${CAPS:-4 8}; do
      tag="${mode}-t${th}-cap${cap}g"; echo "--- $tag ---"
      run_capped "$mode" "$th" "$cap" "$tag"; echo "$tag" >> "$OUT/cells.txt"
    done
  done
done

python3 - "$OUT" <<'PY'
import json, os, sys
out = sys.argv[1]
rows = []
for line in open(os.path.join(out, "cells.txt")):
    tag = line.strip()
    f = os.path.join(out, tag + ".json")
    if not os.path.exists(f) or os.path.getsize(f) < 50:
        rows.append((tag, None)); continue
    try: rows.append((tag, json.load(open(f))))
    except Exception as e: rows.append((tag, {"error": str(e)}))
print(f"\n{'cell':26s} {'pages/s':>10s} {'met':>4s} {'host GiB':>9s} {'device':>10s} {'drive-abs':>10s} {'p50':>7s} {'p99':>8s} {'cpu ms/1k':>10s}")
for tag, j in rows:
    if j is None or "error" in (j or {}):
        print(f"{tag:26s}  MISSING/FAILED {(j or {}).get('error','')}"); continue
    hc = j.get("host_cache_gib_used"); dv = j.get("device_pages_read"); dr = j.get("drive_cache_absorbed_pages")
    warn = ""
    if j.get("evict_first") and (j.get("host_resident_pages_before") or 0) > 1000: warn = " !not cold"
    if j["mode"] == "odirect":
        # O_DIRECT that silently degrades to buffered populates the page cache, so growth in
        # residency across an "O_DIRECT" run IS the fallback signature (btrfs does this on
        # compressed extents without telling anyone).
        if (j.get("host_cache_gib_used") or 0) > 0.5: warn += " !fell-back-to-buffered"
        if j.get("measured_hit_rate", 0) > 0.5: warn += " !direct-but-cached"
    if not j["met_target"]: warn += " !under target"
    print(f"{tag:26s} {j['pages_per_s_achieved']:10,.0f} {str(j['met_target']):>4s} "
          f"{(hc if hc is not None else -1):9.2f} {dv if dv is not None else -1:10,} "
          f"{dr if dr is not None else -1:10,} {j['latency_us']['p50']:7.1f} {j['latency_us']['p99']:8.1f} "
          f"{j['cpu_ms_per_1k_pages']:10.1f}{warn}")
print("\n  flags: !not cold = region was resident at start; !fell-back-to-buffered = an O_DIRECT run")
print("  that populated the page cache, i.e. the kernel ignored O_DIRECT (btrfs does this on")
print("  compressed extents); !under target = could not sustain the prefill page rate, which")
print("  is a real result about that configuration, not a failed run.")
PY
echo MATRIXDONE

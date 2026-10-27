#!/usr/bin/env bash
# Did we find the engine's true runtime VRAM maximum?
#
# util-stress.sh answers "did it OOM". This answers a different question -- what is
# the peak device demand -- and it can only do that where there is slack to measure
# against. Three defects in the existing gate are why the question is still open:
#
#   1. It skips 0.94 (`$2=="OK" && $1!="0.94"`). 0.94 is the only ladder level with
#      real headroom (5,362 MiB boot free on gg). At 0.98 the KV pool absorbs every
#      runtime demand as an eviction, so device usage is flat by construction and
#      "flat" is not evidence that nothing grew.
#   2. The receipts show why: samples-0.98.txt holds 79 values, 78 of them identical
#      (95,868 MiB), the 79th 514 MiB lower. A flat line sampled after the engine
#      already reports ready measures nothing about warmup-time growth.
#   3. nvidia-smi alone cannot separate "nothing was allocated" from "it was
#      allocated and immediately reused from the torch cache". The allocator has to
#      be forced to show its demand, or reuse has to be ruled out by another route.
#
# Method: boot with slack, sample nvidia-smi AND the engine's own /metrics from
# before boot until the last leg finishes, then run the risk shapes one at a time and
# again concurrently, reporting peak device usage and the increment over baseline per
# phase. The quantity that matters is not peak used, it is
#
#     peak_used - (engine accounted state at boot)
#
# i.e. how much the runtime demanded beyond what the engine measured for itself during
# its own memory profiling. If that increment never exceeds the engine's profiled
# activation peak, no headroom above the profile is required and shrinking the pool
# costs nothing. If it does, that number IS the required headroom.
#
# PYTORCH_NO_CUDA_MEMORY_CACHING=1 (NOCACHE=1) is the decisive variant: with the
# caching allocator disabled, every peak demand becomes a real driver allocation and
# nvidia-smi stops hiding reuse. It is slow, so run it on the concurrent shapes only.
set -uo pipefail
cd "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f start.sh && -f stop.sh ]] || { echo "no start.sh/stop.sh above $(dirname -- "${BASH_SOURCE[0]}") -- run this file from <repo>/scripts/" >&2; exit 2; }

QUANT="${QUANT:-exl3}"
MODEL="${MODEL:-qwen38-$QUANT}"
NAME="${CONTAINER_NAME:-qwen38-$QUANT}"
PORT="${PORT:-8199}"
OUT="${OUT:?set OUT to a per-campaign directory}"; mkdir -p "$OUT"
B=http://127.0.0.1:"$PORT"
IMG="${IMAGE:-qwen38-rtx:local}"
GPU="${GPU:-0}"
UTIL="${UTIL:-0.94}"                     # slack is the instrument; do not raise it
# Unscoped nvidia-smi prints one line PER GPU, and command substitution keeps the
# newlines, so an unspecifying sampler appends several cards to one sample and the
# peak silently becomes the busiest neighbour. This script exists to measure a
# single card under a single tenant; holding it to one index is the whole point.
CARD="${GPU%%,*}"
[[ "$CARD" =~ ^[0-9]+$ ]] || { echo "GPU=$GPU does not start with an index; the sampler needs one" >&2; exit 2; }
DEEP="${DEEP:-220000}"                   # per-request prefill tokens for the storm
NOCACHE="${NOCACHE:-0}"
ROW="$OUT/peak.tsv"; [[ -e "$ROW" ]] || printf "phase\tpeak_used_MiB\tdelta_over_prev_peak_MiB\tsamples\tpreemptions\tpeak_kv_usage\tpool_pages\tnote\n" > "$ROW"

stop_engine() {
  QUANT=$QUANT IMAGE=$IMG PORT=$PORT bash stop.sh >>"$OUT/ops.log" 2>&1 || true
  local state; state=$(docker inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null) || return 0
  if [[ "$state" == running || "$state" == created || "$state" == restarting ]]; then
    echo "STOP-REFUSED: $NAME still $state" >>"$OUT/ops.log"; return 1
  fi
  docker rm "$NAME" >/dev/null 2>&1 || true; sleep 5
}

# Sampling spans the boot, not just the legs: the growth we cannot currently rule out
# is the kind that happens between "model loaded" and "API serving".
start_sampler() {
  : > "$OUT/samples.txt"; : > "$OUT/metrics.txt"
  ( for i in $(seq 1 3600); do
      printf "%s\t%s\n" "$(date +%s)" "$(nvidia-smi --id="$CARD" --query-gpu=memory.used --format=csv,noheader,nounits)" >> "$OUT/samples.txt"
      # Sum over label sets: what matters per sample is the aggregate, and vLLM emits
      # one line per label set. One awk process per sample, so a missing endpoint is
      # simply an empty metric line rather than a gap in the series.
      # Name is everything before "{" or " " -- the label set sits between them, and a
      # greedy strip swallows the value too (tested against a live engine: it collapsed
      # every series into one empty name). Sum over label sets, one awk per sample.
      curl -s --max-time 5 "$B/metrics" 2>/dev/null | awk '
        /^vllm:/ { line = $0; sub(/[{ ].*$/, "", line); tot[line] += $NF + 0 }
        END { for (n in tot) printf "%s\t%.6g\n", n, tot[n] }' >> "$OUT/metrics.txt"
      sleep 2
    done ) &
  SAMPLER=$!
  trap 'kill $SAMPLER 2>/dev/null' EXIT
}

peak() { awk -F'\t' 'NF==2 && $2 ~ /^[0-9]+$/ && $2>m{m=$2}END{print m+0}' "$OUT/samples.txt"; }
# Cross-check against the host-side view of the same card. If the sample is not
# consistent with what nvidia-smi says is free right now, the probe is reading the
# wrong device -- fail here rather than report a neighbour's footprint.
sanity() {
  local base free total
  # NOT head -1 of the sample file: sampling starts before the container exists, so the
  # first line is an idle card (measured: 2 MiB). The baseline is this instant.
  base=$(peak)
  # `read` consumes ONE LINE: it splits fields, it does not read records. The first
  # version of this piped the two CSV fields through tr ',' '\n' and got free=<value>
  # total=<empty>, because the second value was on line 2 -- so the guard below
  # arithmetic'd against nothing and could never fire. Keep both fields on one line.
  read -r free total < <(nvidia-smi --id="$CARD" --query-gpu=memory.free,memory.total \
    --format=csv,noheader,nounits | tr ',' ' ')
  echo "baseline ${base} MiB used vs free=${free}/${total} on card $CARD (expect used+free ~= total)" >> "$OUT/engine-accounting.txt"
  [[ -n "${free:-}" ]] || return 0
  if (( base + free < total - 2048 || base + free > total + 2048 )); then
    echo "SAMPLER SANITY FAILED: card $CARD baseline $base + free $free != total $total" | tee -a "$ROW" >&2
    kill "$SAMPLER" 2>/dev/null; exit 1
  fi
}
nsamples() { wc -l < "$OUT/samples.txt"; }
PREV_PEAK=0
mark() {   # $1 = phase name, $2 = note
  local p; p=$(peak)
  local prem kv
  prem=$(awk -F'\t' '$1=="vllm:num_preemptions_total"{if($2>m)m=$2}END{print m+0}' "$OUT/metrics.txt")
  kv=$(awk -F'\t' '$1 ~ /_cache_usage_perc$/{if($2>m)m=$2}END{printf "%.3f", m+0}' "$OUT/metrics.txt")
  # blocks is the residency unit (pool pages), read from the engine rather than
  # inferred from a log line: vllm:cache_config_info carries num_gpu_blocks and
  # gpu_memory_utilization as labels on every boot.
  blocks="${POOL_PAGES:-}"   # constant per boot; captured once at ready
  printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" "$1" "$p" "$((p - PREV_PEAK))" "$(nsamples)" "$prem" "$kv" "${blocks:-}" "$2" >> "$ROW"
  PREV_PEAK=$p
}

# Positive control. Every phase in this probe can only ever report "no growth", and a
# sampler that cannot SEE growth would report that forever, correctly-shaped and wrong --
# which is exactly how util-stress.sh came to record a flat 0.98 line for the wrong
# reason. So grow the device deliberately, inside the engine's own container, and require
# the sampler to record it. If this fails, nothing below it means anything.
control() {
  local before after hi
  before=$(peak)
  # 1536 1 MiB blocks is unambiguous against a background of tens of MiB, and it is well
  # inside the ~5.2 GiB of slack a 0.94 boot leaves -- so it cannot perturb the phases.
  docker exec "$NAME" python3 -c '
import torch, time
t = [torch.empty(1024*1024, dtype=torch.uint8, device="cuda") for _ in range(1536)]
torch.cuda.synchronize(); time.sleep(16); del t; torch.cuda.empty_cache()' \
    >>"$OUT/legs.log" 2>&1 || echo "control: docker exec failed (engine gone?)" >> "$OUT/legs.log"
  sleep 4
  after=$(peak); hi=$((after - before))
  echo "control: device usage rose $((after - before)) MiB while 1.5 GiB was held" >> "$OUT/legs.log"
  (( hi >= 1024 )) && return 0
  echo "control: only $hi MiB observed -- the sampler cannot see growth" >> "$OUT/legs.log"
  return 1
}

stop_engine
# NOCACHE=1 boots with the torch caching allocator disabled, so every peak demand
# becomes a real driver allocation and nvidia-smi stops hiding reuse behind a flat
# line. Slow: use it on the concurrent shapes, not the whole ladder.
ce=""
[[ "$NOCACHE" == 1 ]] && ce="PYTORCH_NO_CUDA_MEMORY_CACHING=1"
QUANT=$QUANT IMAGE=$IMG GPU=${GPU:-0} PORT=$PORT GPU_MEMORY_UTILIZATION=$UTIL \
  CONTAINER_ENV="$ce" bash start.sh >>"$OUT/ops.log" 2>&1 &
start_sampler
ready=0
for i in $(seq 1 120); do curl -sf $B/v1/models >/dev/null 2>&1 && { ready=1; break; }; sleep 10; done
[[ $ready == 1 ]] || { echo "BOOT FAILED (see ops.log)" >&2; kill $SAMPLER 2>/dev/null; exit 1; }
docker logs "$NAME" > "$OUT/engine-$UTIL.log" 2>&1 || true
# The engine publishes its own residency geometry as labels on one gauge: pool pages,
# attention block size, the utilization it actually booted with, and the token ratio it
# (mis)advertises as max concurrency. Reading it here means the number under test is the
# number the scheduler uses, not one transcribed from a log line.
curl -s --max-time 8 "$B/metrics" | grep -aE "^vllm:cache_config_info" > "$OUT/cache-config.txt" || true
POOL_PAGES=$(sed 's/,/\n/g' "$OUT/cache-config.txt" | sed -n 's/.*num_gpu_blocks="\([0-9]*\)".*/\1/p' | head -1)
echo "pool_pages=$POOL_PAGES $(sed 's/,/ /g' "$OUT/cache-config.txt" | grep -oE 'block_size="[0-9]+"|gpu_memory_utilization="[0-9.]+"' | tr '\n' ' ')" >> "$OUT/engine-accounting.txt"
# The engine's own claim about what it needs, for the final verdict line.
grep -hE "Desired GPU memory utilization|peak activation|Available KV cache memory|Setting attention block size" \
  "$OUT/engine-$UTIL.log" | sed 's/^[^ ]* //' >> "$OUT/engine-accounting.txt" || true

sanity
mark baseline "boot+warmup, no legs"
python3 scripts/test-vision-vllm.py --base-url $B --model "$MODEL" --image-counts 16 --output "$OUT/shape-vision.json" >>"$OUT/legs.log" 2>&1 || echo "vision leg failed" >>"$OUT/legs.log"
mark vision "16 images, alone"
python3 scripts/verify-structured-output-live.py --base-url $B/v1 --model "$MODEL" --output "$OUT/shape-structured.json" >>"$OUT/legs.log" 2>&1 || echo "structured leg failed" >>"$OUT/legs.log"
mark structured "FSM constrained decode, alone"
python3 scripts/test-context-retrieval.py --base-url $B/v1 --model "$MODEL" --filler-tokens 240000 --output "$OUT/shape-prefill240k.json" >>"$OUT/legs.log" 2>&1 || echo "prefill leg failed" >>"$OUT/legs.log"
mark prefill-240k "single deep prefill, alone"
# The shape the old gate never produced: many sequences resident at once.
python3 scripts/benchmark-deep-concurrency.py --base-url $B/v1 --model "$MODEL" \
  --fractions 0.42 --concurrency 4 --output-tokens 512 --output "$OUT/shape-deep4.jsonl" >>"$OUT/legs.log" 2>&1 || echo "deep4 leg failed" >>"$OUT/legs.log"
mark deep-4x-concurrent "4 concurrent deep prefills, short outputs"
# All-hands: the riskiest shapes overlapping, which is the only interleaving that can
# exceed every individual peak if the allocator cannot reuse across them.
python3 scripts/test-vision-vllm.py --base-url $B --model "$MODEL" --image-counts 16 --output "$OUT/shape-all-vision.json" >>"$OUT/legs.log" 2>&1 &
hand_pids=($!)
python3 scripts/verify-structured-output-live.py --base-url $B/v1 --model "$MODEL" --output "$OUT/shape-all-structured.json" >>"$OUT/legs.log" 2>&1 &
hand_pids+=($!)
python3 scripts/benchmark-deep-concurrency.py --base-url $B/v1 --model "$MODEL" \
  --fractions 0.42 --concurrency 2 --output-tokens 512 --output "$OUT/shape-all-deep2.jsonl" >>"$OUT/legs.log" 2>&1 &
hand_pids+=($!)
# A bare `wait` also waits for the sampler subshell, whose loop runs to its iteration
# cap -- the driver then hangs for the rest of the sampling window with every leg
# already finished and the GPU idle. This is the same defect util-stress.sh carries a
# comment about (its grid recorded nothing for 30 minutes after the engine stopped);
# wait on the requests only.
for p in "${hand_pids[@]}"; do wait "$p"; done
sleep 5
mark all-hands "vision + structured + 2 deep prefills overlapping"
python3 scripts/benchmark-decode.py --base-url $B/v1 --model "$MODEL" --profile "$QUANT" --mtp-tokens "${MTP_TOKENS:-3}" \
  --concurrency 16 --request-mode clients --output-tokens 512 --warmup-runs 1 --runs 2 \
  --output "$OUT/shape-decode16.json" >>"$OUT/legs.log" 2>&1 || echo "decode16 leg failed" >>"$OUT/legs.log"
mark decode-16-at-cudagraph-bucket "16-way decode, max bucket"

# The control runs LAST, and that ordering is not cosmetic. Device usage is a high-water
# mark, so a control that deliberately adds 1.5 GiB raises the peak permanently: run first,
# it makes every later phase read delta 0 no matter what those phases do -- which is a
# different kind of blind sampler, wearing the costume of the thing that fixes it. Measured
# that way on the first run: control took usage to 93,635 MiB and all six following phases
# reported 0. Phases are the measurement; the control is the validation, done at the end on
# a device that is otherwise idle, and its result is reported alongside the verdict.
control_note="PASS"
control || control_note="FAIL"
printf "control-1.5GiB\t%s\t%s\t%s\t\t\t%s\tsampler sensitivity check, run after the phases\n" \
  "$(peak)" "$control_note" "$(nsamples)" "" >> "$ROW"
kill $SAMPLER 2>/dev/null
# Verdict: how much did runtime demand exceed what the engine profiled for itself?
python3 - "$ROW" "$OUT/engine-accounting.txt" <<'PY'
import re, sys
rows = [l.rstrip("\n").split("\t") for l in open(sys.argv[1])][1:]
base = int(rows[0][1]); top = max(int(r[1]) for r in rows)
act = 0.0
try:
    txt = open(sys.argv[2]).read()
    m = re.search(r"([\d.]+) GiB for peak activation", txt)
    act = float(m.group(1)) * 1024 if m else 0.0
except OSError:
    pass
print(f"baseline {base} MiB | worst phase {top} MiB | growth over baseline {top - base} MiB"
      f" | engine profiled activation {act:.0f} MiB")
print("VERDICT: headroom above the engine's own profile:",
      "none required (growth <= profiled activation)" if top - base <= act else
      f"{top - base - act:.0f} MiB REQUIRED -- reduce the pool by at least that")
PY
echo "PEAK PROBE DONE $(date -u +%FT%TZ)" >> "$ROW"
# Release the card. A finished probe that leaves its engine holding 92 GiB is a ghost
# with a heartbeat: it looks like a running experiment to the next audit and it blocks
# the next boot on that device.
stop_engine

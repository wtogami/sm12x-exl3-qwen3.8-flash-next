#!/usr/bin/env bash
set -euo pipefail
RECIPE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$RECIPE_DIR/model-profiles.sh"
source "$RECIPE_DIR/platform-config.sh"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-$DEFAULT_GPU_MEMORY_UTILIZATION}"
if [[ "$PLATFORM_KIND" == spark ]]; then
  for argument in "$@"; do
    case "$argument" in
      --gpu-memory-utilization|--gpu-memory-utilization=*)
        echo "Use GPU_MEMORY_UTILIZATION (capped at 0.7 on Spark)" >&2; exit 2 ;;
    esac
  done
  awk -v value="$GPU_MEMORY_UTILIZATION" 'BEGIN {exit !(value ~ /^[0-9]+([.][0-9]+)?$/ && value > 0 && value <= 0.7)}' || {
    echo "Spark GPU_MEMORY_UTILIZATION must be greater than zero and at most 0.7" >&2; exit 2;
  }
fi
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
RUNTIME_CACHE="${RUNTIME_CACHE:-$HOME/.cache/qwen38-rtx/${QUANT:-exl3}}"
mkdir -p "$RUNTIME_CACHE"
MODEL_PATH="/root/.cache/huggingface/hub/$MODEL_CACHE_NAME/snapshots/$MODEL_REVISION"
CONFIG_PATH="$HF_CACHE/hub/$MODEL_CACHE_NAME/snapshots/$MODEL_REVISION/config.json"
if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "Missing checkpoint. Run QUANT=${QUANT:-exl3} $RECIPE_DIR/download.sh" >&2
  exit 1
fi
MTP_TOKENS="${MTP_TOKENS:-$( [[ ${QUANT:-exl3} == nvfp4 ]] && echo 2 || echo "$DEFAULT_EXL3_MTP_TOKENS" )}"
# PLE_MMAP=1 reads the PLE table in place from the checkpoint mapping
# (EngramConfig checkpoint_mapped). It requires a GPU that dereferences
# pageable host memory through the host page tables (DGX Spark / GB10); the
# CUDA driver rejects others at startup. PLE_MMAP=0 keeps the host-offloaded
# resident table (the upstream cpu_offload default).
PLE_MMAP="${PLE_MMAP:-${VLLM_PLE_MMAP:-${DEFAULT_PLE_MMAP:-0}}}"
[[ "$PLE_MMAP" == 0 || "$PLE_MMAP" == 1 ]] || { echo "PLE_MMAP must be 0 or 1" >&2; exit 2; }
# PLE_PIN_STRATEGY chooses how the PLE table's pinned host memory is allocated.
# round2 (default) is torch's pin_memory path, which rounds every request up to a power
# of two -- 47.684 GiB of FP8 table costs 64.000 GiB pinned. exact page-locks the true
# size through the CUDA driver instead (patches/qwen_pinned_host.py), which does not
# round. The value has to reach the container as well as the preflight a few lines
# below: the gate sizes the reservation, the loader performs it, and if they disagree
# the gate approves a footprint the host cannot commit.
PLE_PIN_STRATEGY="${PLE_PIN_STRATEGY:-round2}"
[[ "$PLE_PIN_STRATEGY" == round2 || "$PLE_PIN_STRATEGY" == exact ]] || { echo "PLE_PIN_STRATEGY must be round2 or exact" >&2; exit 2; }
# PLE_STREAM=1 reads the PLE table from the checkpoint by CPU row copy into pinned staging
# (patches/qwen_ple_stream.py), instead of page-locking the whole table in host RAM. It exists for
# hosts with less RAM than the table: the FP8 table pins 47.684 GiB and the BF16 one 95.368 GiB,
# neither of which a 32 GiB host can commit. It reuses the checkpoint_mapped storage plumbing --
# including the host page prefetcher, which is what keeps the page cache warm -- and replaces only
# the gather, because the mapped path asks the GPU to dereference host page-table mappings and
# discrete RTX parts report PAGEABLE_MEMORY_ACCESS_USES_HOST_PAGE_TABLES=0.
# Measured on this shape: ~0.5% of prefill exposed at p50, ~0.8% at p99, page-cache hits 74-80%
# at 8-16 GiB of cache. Default off: the resident pinned table is faster when it fits.
PLE_STREAM="${PLE_STREAM:-${VLLM_PLE_STREAM:-0}}"
[[ "$PLE_STREAM" == 0 || "$PLE_STREAM" == 1 ]] || { echo "PLE_STREAM must be 0 or 1" >&2; exit 2; }
if [[ "$PLE_STREAM" == 1 && "$PLE_MMAP" == 0 ]]; then
  # Streaming selects its backend by class before checkpoint_mapped is consulted, but the storage
  # plumbing it inherits (never-materialised weights, host page prefetcher) is gated on that flag.
  # Turning it on silently would run the streaming gather with the prefetcher off and the weights
  # resident -- slower and using the RAM the feature exists to avoid.
  echo "  note: PLE_STREAM=1 implies PLE_MMAP=1 (checkpoint-mapped storage, CPU gather); setting it" >&2
  PLE_MMAP=1
fi
if [[ "$PLE_STREAM" == 1 ]]; then
  # A streaming gather is host code and cannot live inside a captured CUDA graph: capture would
  # freeze one row set and replay it forever. Breakable CUDA graphs are what makes it legal --
  # @eager_break_during_capture on start_prefetch records the callable and re-runs the Python on
  # every replay. Without it the engine dies at capture time with "Cannot copy between CPU and
  # CUDA tensors during CUDA graph capture unless the CPU tensor is pinned".
  # The cost is real and separate: enabling this sets compilation mode to NONE, so throughput must
  # be compared as {pinned, streaming} x {graphs, breakable} before any delta is attributed to
  # streaming itself.
  if [[ -z "${VLLM_USE_BREAKABLE_CUDAGRAPH:-}" ]]; then
    export VLLM_USE_BREAKABLE_CUDAGRAPH=1
    echo "  note: PLE_STREAM=1 implies VLLM_USE_BREAKABLE_CUDAGRAPH=1 (the host gather must run" >&2
    echo "        eagerly on every replay); set it explicitly to override" >&2
  fi
fi
# LONGCTX=1 serves 512K via YaRN 2.0 rope scaling of the 262K-native
# checkpoint (RULER 97.66 at 523K vs 97.92 at native 262K, no measured
# throughput cost). LONGCTX=0 stays at the native 262144 window.
LONGCTX="${LONGCTX:-${DEFAULT_LONGCTX:-0}}"
[[ "$LONGCTX" == 0 || "$LONGCTX" == 1 ]] || { echo "LONGCTX must be 0 or 1" >&2; exit 2; }
MAX_MODEL_LEN="${MAX_MODEL_LEN:-$([[ "$LONGCTX" == 1 ]] && echo 524288 || echo 262144)}"
EXTRA_ARGS=()
if [[ "$LONGCTX" == 1 ]]; then
  case " $* " in
    *" --hf-overrides "*|*" --hf-overrides="*) ;;
    *)
      EXTRA_ARGS+=(--hf-overrides '{"text_config":{"max_position_embeddings":524288,"rope_parameters":{"rope_type":"yarn","factor":2.0,"original_max_position_embeddings":262144}}}')
      ;;
  esac
fi
CUSTOM_ENGRAM=0
case " $* " in
  *" --engram-config "*|*" --engram-config="*) CUSTOM_ENGRAM=1 ;;
  *)
    if [[ "$PLE_MMAP" == 1 ]]; then
      EXTRA_ARGS+=(--engram-config '{"cpu_offload": true, "checkpoint_mapped": true}')
    else
      EXTRA_ARGS+=(--engram-config '{"cpu_offload": true, "checkpoint_mapped": false}')
    fi
    ;;
esac
# Size the pinned host memory before the container exists. A checkpoint-mapped
# table allocates nothing table-sized, and a caller-supplied --engram-config
# means we cannot know the placement, so both cases skip the check.
# With PLE_STREAM=1 there is no table-sized pinned allocation, so this gate would refuse a host
# that fits -- the exact disagreement this check exists to prevent. The staging arena it needs
# instead is megabytes, not gigabytes, and is sized by the loader.
if [[ "$PLE_MMAP" == 0 && "$PLE_STREAM" == 0 && "$CUSTOM_ENGRAM" == 0 \
     && "${QWEN38_SKIP_MEMCHECK:-0}" != 1 ]] && [[ -r /proc/meminfo ]]; then
  mem_total_kib=0
  mem_avail_kib=0
  while read -r key value _; do
    case "$key" in
      MemTotal:) mem_total_kib=$value ;;
      MemAvailable:) mem_avail_kib=$value ;;
    esac
  done < /proc/meminfo
  qwen38_ple_memcheck "$CONFIG_PATH" "${PLE_BYTES_PER_ELEMENT:-2}" "$mem_total_kib" "$mem_avail_kib"
fi
# Streaming replaces the pinned-table requirement with a page-cache requirement, so it needs its own
# preflight rather than the absence of one: "no table pinned" is not "no memory needed". This reads the
# cgroup limit too, because /proc/meminfo inside a capped container reports the host's RAM.
if [[ "$PLE_STREAM" == 1 && "$CUSTOM_ENGRAM" == 0 && "${QWEN38_SKIP_MEMCHECK:-0}" != 1 ]] \
   && [[ -r /proc/meminfo ]]; then
  mem_total_kib=0
  mem_avail_kib=0
  while read -r key value _; do
    case "$key" in
      MemTotal:) mem_total_kib=$value ;;
      MemAvailable:) mem_avail_kib=$value ;;
    esac
  done < /proc/meminfo
  qwen38_ple_stream_check "$CONFIG_PATH" "${PLE_BYTES_PER_ELEMENT:-2}" "$mem_total_kib" "$mem_avail_kib" || exit 3
fi
# A container left over from a previous run still owns the name, and both engines
# then reject the run with "name is already in use". Removing a dead one is done
# automatically: it is serving nothing, and otherwise the documented stop.sh then
# start.sh sequence is a dead end that only works once you already know about
# docker rm -- which is how people end up deleting containers by accident.
#
# A live predecessor is refused rather than replaced. That is a deliberate answer
# to --replace, not an omission of it. Replacing cuts a serving engine over
# mid-request with no warning, and --replace performs its own stop using the
# runtime's default grace period, the same 10s this workload gets SIGKILLed
# under, so the "convenient" path is also the rude one. REPLACE=1 does the handoff
# explicitly and with the grace period stop.sh uses.
container_name="${CONTAINER_NAME:-qwen38-${QUANT:-exl3}}"
if state="$(docker inspect --format '{{.State.Status}}' "$container_name" 2>/dev/null)"; then
  case "$state" in
    created|exited|dead)
      echo "Removing $state container $container_name from a previous run" >&2
      docker rm "$container_name" >/dev/null ;;
    *)
      if [[ "${REPLACE:-0}" == 1 ]]; then
        echo "Stopping $state container $container_name to replace it (REPLACE=1)" >&2
        docker stop --time "$QWEN38_STOP_TIMEOUT" "$container_name" >/dev/null
        docker rm "$container_name" >/dev/null
      else
        {
          echo "$container_name is already $state. Run ./stop.sh first, or set"
          echo "REPLACE=1 to stop and replace it in one step. Not done automatically:"
          echo "a running container is serving requests and a replacement cuts them"
          echo "off mid-flight, with the default 10s grace period it would not even"
          echo "shut down cleanly."
        } >&2
        exit 1
      fi ;;
  esac
fi
[[ "${ENFORCE_EAGER:-0}" == 1 ]] && EXTRA_ARGS+=(--enforce-eager)
if [[ "${MTP_TOKENS:-3}" != 0 ]]; then
  EXTRA_ARGS+=(--speculative-config "{\"method\":\"mtp\",\"num_speculative_tokens\":${MTP_TOKENS:-3}}")
fi
# Docker wants --gpus; podman's docker shim rejects its device= syntax and
# resolves GPUs through CDI instead (see platform-config.sh).
#
# Do not "unify" these two branches on --device nvidia.com/gpu=$N. The flags are
# not equivalent: --gpus resolves through whichever mechanism the engine has, so
# moby maps it onto CDI when a spec exists and falls back to the prestart hook
# when none does, whereas --device requires a spec in a directory that engine
# scans. This recipe generates its spec under $HOME, which rootful docker does
# not read, so switching the docker branch to --device would add a root-time
# /etc/cdi provisioning step and a new stale-spec failure mode, and would raise
# the docker floor to 28.2.0 (CDI default-on) to no benefit. Divergence here is
# deliberate. Docker paths in this repo are untested: no docker host was
# available, so treat that branch as reasoned-about, not verified.
#
# Serving is where the GPU is actually needed, and a pull.sh deployment never
# runs build.sh, so the CDI spec is provisioned here as well. Unlike build.sh
# this one is fatal: without it the container cannot start at all.
if qwen38_is_podman; then
  qwen38_ensure_cdi
  GPU_ARGS=(--device nvidia.com/gpu=all -e NVIDIA_VISIBLE_DEVICES="${GPU:-all}")
else
  GPU_ARGS=(--gpus "device=${GPU:-0}")
fi
# The container writes its triton and cuda build caches into RUNTIME_CACHE and
# runs as root so the image's /root/.cache paths resolve. Under rootless podman
# container uid 0 is the invoking host user, so those caches land user-owned
# without a keep-id mapping. The :z relabel is the separate SELinux half: both
# mounts keep their host cache_home_t label, which container_t cannot access.
# :z rather than :Z because these caches are shared with download.sh, and :Z
# would isolate each run behind its own MCS category.
docker run -d --name "$container_name" \
  "${GPU_ARGS[@]}" \
  --ipc=host --network=host \
  -e OMP_NUM_THREADS="${CPU_THREADS:-8}" \
  -e PLE_PIN_STRATEGY="${PLE_PIN_STRATEGY}" \
  -e PLE_STREAM="${PLE_STREAM}" \
  -e VLLM_USE_BREAKABLE_CUDAGRAPH="${VLLM_USE_BREAKABLE_CUDAGRAPH:-0}" \
  -e PLE_ROWCOPY_SO="${PLE_ROWCOPY_SO:-/opt/qwen38/ple_rowcopy.so}" \
  -e CUDA_CACHE_PATH=/root/.cache/cuda \
  -e TRITON_CACHE_DIR=/root/.cache/triton \
  -e QWEN38_B12X_VOCAB="${B12X_VOCAB:-$DEFAULT_B12X_VOCAB}" \
  -e QWEN38_B12X_NVFP4="${B12X_NVFP4:-0}" \
  -v "$RUNTIME_CACHE:/root/.cache:z" \
  -v "$HF_CACHE:/root/.cache/huggingface:ro,z" \
  "${IMAGE:-$DEFAULT_IMAGE}" "$MODEL_PATH" \
  --served-model-name "qwen38-${QUANT:-exl3}" --port "${PORT:-8001}" \
  --tensor-parallel-size 1 --distributed-executor-backend mp \
  --max-model-len "${MAX_MODEL_LEN:-262144}" \
  --max-num-seqs "${MAX_NUM_SEQS:-16}" \
  --max-num-batched-tokens "${MAX_BATCHED_TOKENS:-${DEFAULT_MAX_BATCHED_TOKENS:-2048}}" \
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" \
  --kv-cache-dtype fp8 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3 \
  "${EXTRA_ARGS[@]}" "$@"

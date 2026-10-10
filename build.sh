#!/usr/bin/env bash
set -euo pipefail
RECIPE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$RECIPE_DIR/platform-config.sh"
# No build step touches the GPU: every RUN is a pip install, a source patch of
# vllm/, or a CPU-only test suite, so the "No CUDA runtime is found" and
# "libcuda.so.1: cannot open shared object file" lines docker/podman print from
# those steps are expected. libcuda.so.1 is the host driver library and is only
# bind-mounted into /usr/local/nvidia/lib64 when a container starts. Serving
# does need it, so provision the podman CDI spec here, where a fresh checkout
# lands first. Failing is only a warning: it cannot break this build.
if qwen38_is_podman; then
  qwen38_ensure_cdi || echo "WARNING: podman GPU passthrough is not configured; start.sh will fail until it is" >&2
fi
docker build --progress=plain --build-arg CUTE_DSL_ARCH="$DEFAULT_CUTE_DSL_ARCH" --build-arg B12X_VOCAB="$DEFAULT_B12X_VOCAB" --tag "${IMAGE:-$DEFAULT_IMAGE}" "$RECIPE_DIR"

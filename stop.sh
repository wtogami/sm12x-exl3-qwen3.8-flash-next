#!/usr/bin/env bash
set -euo pipefail
RECIPE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$RECIPE_DIR/platform-config.sh"
name="${CONTAINER_NAME:-qwen38-${QUANT:-exl3}}"
if ! docker inspect --format '{{.State.Status}}' "$name" >/dev/null 2>&1; then
  # Not an error worth a stack of podman output: the name is derived from QUANT,
  # so the usual cause of this is stopping for the wrong profile.
  echo "No container named $name is present. QUANT selects the name (qwen38-\$QUANT):" >&2
  docker ps -a --format '  {{.Names}}  {{.Status}}' >&2 || true
  exit 1
fi
# The engine default stop timeout is 10s and this workload does not fit inside
# it. Measured on a 125.68 GiB RTX host running the exl3-ple8 profile: an idle
# engine takes 13.9s to shut down cleanly, just past the default, so every stop
# was being escalated to SIGKILL. The signal itself is not being dropped -- PID 1
# is vllm itself under an exec-form entrypoint, and its SigCgt mask catches
# SIGTERM -- the grace period was simply too short. 90s leaves room for in-flight
# requests to drain without letting a wedged engine hang this script forever;
# override with STOP_TIMEOUT.
docker stop --time "$QWEN38_STOP_TIMEOUT" "$name"

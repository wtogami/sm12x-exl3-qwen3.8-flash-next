#!/usr/bin/env bash
set -euo pipefail
RECIPE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$RECIPE_DIR/model-profiles.sh"
source "$RECIPE_DIR/platform-config.sh"
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
mkdir -p "$HF_CACHE"
# Pinning the container user to the invoking uid keeps the checkpoint cache
# user-owned under docker, which shares the host uid namespace. Rootless podman
# instead maps container uids into the $USER subuid range, so uid 1002 inside
# the container is not uid 1002 on the host and the cache mount is unwritable;
# keep-id maps the invoking user through at the same uid so --user keeps working.
# The :z relabel is the separate SELinux half: a bind mount keeps its
# cache_home_t label and the container_t process is denied write access to it.
# :z switches it to the shared container_file_t:s0 that both this download and
# the later start.sh mount need; it is ignored where SELinux is off.
if qwen38_is_podman; then
  USER_ARGS=(--userns=keep-id)
else
  USER_ARGS=()
fi
# Passed by name so the token stays out of the image history and the process argv.
AUTH_ARGS=()
[[ -n "${HF_TOKEN:-}" ]] && AUTH_ARGS=(-e HF_TOKEN)
docker run --rm --user "$(id -u):$(id -g)" "${USER_ARGS[@]}" \
  -e HF_HOME=/hf \
  --env MODEL_REPO="$MODEL_REPO" --env MODEL_REVISION="$MODEL_REVISION" \
  "${AUTH_ARGS[@]}" \
  -v "$HF_CACHE:/hf:z" --entrypoint python3 "${IMAGE:-$DEFAULT_IMAGE}" \
  -c 'import os; from huggingface_hub import snapshot_download; print(snapshot_download(os.environ["MODEL_REPO"], revision=os.environ["MODEL_REVISION"]))'

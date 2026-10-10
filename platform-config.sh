#!/usr/bin/env bash
# Native build/run defaults for RTX SM120 and Spark SM121.
case "$(uname -m)" in
  aarch64|arm64)
    DEFAULT_EXL3_MTP_TOKENS=2
    DEFAULT_B12X_VOCAB=1
    PLATFORM_KIND=spark
    DEFAULT_IMAGE=spark-exl3-qwen3.8-flash-next:local
    DEFAULT_RELEASE_IMAGE=ghcr.io/tpurtell/spark-exl3-qwen3.8-flash-next@sha256:67f7b104711451878e5af188195c7ac675e79246aab65ff36bb9919bcdffeb33
    DEFAULT_GPU_MEMORY_UTILIZATION=0.7
    DEFAULT_CUTE_DSL_ARCH=sm_121a
    DEFAULT_LONGCTX=0
    DEFAULT_MAX_BATCHED_TOKENS=2048
    # GB10 reads pageable host memory through the host page tables, so the
    # checkpoint-mapped PLE table is the Spark default.
    DEFAULT_PLE_MMAP=1
    ;;
  x86_64|amd64)
    DEFAULT_EXL3_MTP_TOKENS=3
    DEFAULT_B12X_VOCAB=0
    PLATFORM_KIND=rtx
    DEFAULT_IMAGE=qwen38-rtx:local
    DEFAULT_RELEASE_IMAGE=ghcr.io/tpurtell/rtx6k-exl3-qwen3.8-flash-next@sha256:0f3fdb9e1073446ca756db7541bbcfe943a7e5c43aa97e85cbcba982d82b7516
    DEFAULT_GPU_MEMORY_UTILIZATION=0.94
    DEFAULT_CUTE_DSL_ARCH=sm_120a
    DEFAULT_LONGCTX=1
    DEFAULT_MAX_BATCHED_TOKENS=2048
    # Discrete RTX cards cannot dereference the checkpoint mapping (CUDA
    # PAGEABLE_MEMORY_ACCESS_USES_HOST_PAGE_TABLES=0); use the host-offloaded
    # resident table instead.
    DEFAULT_PLE_MMAP=0
    ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 2 ;;
esac

# Docker wants --gpus; podman's docker shim rejects its device= syntax and
# resolves GPUs through CDI instead.
qwen38_is_podman() { docker --version 2>/dev/null | grep -qi podman; }

# podman only reads CDI specs from the cdi_spec_dirs list in containers.conf,
# whose defaults (/etc/cdi and /var/run/cdi) are root-owned, and a spec is not
# portable: it pins absolute host driver paths carrying the driver version. So
# both the spec and the directory registration live under $HOME and are
# refreshed whenever the installed driver no longer matches the spec. This
# needs no root, only the nvidia-container-toolkit package.
qwen38_ensure_cdi() {
  local config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
  local spec_dir="$config_home/cdi"
  local spec="$spec_dir/nvidia.yaml"
  local dropin="$config_home/containers/containers.conf.d/99-qwen38-nvidia-cdi.conf"
  local driver reason
  if ! command -v nvidia-ctk >/dev/null 2>&1 || ! command -v nvidia-cdi-hook >/dev/null 2>&1; then
    echo "nvidia-ctk and nvidia-cdi-hook are required for podman GPU passthrough and are not" >&2
    echo "installed. Neither Fedora nor RPM Fusion packages them; on Fedora 44 use the COPR:" >&2
    echo "  dnf copr enable @ai-ml/nvidia-container-toolkit" >&2
    echo "  dnf install nvidia-container-toolkit nvidia-container-toolkit-selinux" >&2
    return 1
  fi
  driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1)"
  reason=""
  if [[ ! -s "$spec" ]]; then
    reason="absent"
  elif [[ -n "$driver" ]] && ! grep -q "$driver" "$spec"; then
    reason="pins a driver other than the installed $driver"
  elif ! grep -q "name: all" "$spec"; then
    reason="has no nvidia.com/gpu=all entity"
  fi
  if [[ -n "$reason" ]]; then
    echo "Generating $spec ($reason)" >&2
    mkdir -p "$spec_dir"
    nvidia-ctk --quiet cdi generate --output="$spec" \
      || { echo "nvidia-ctk cdi generate failed writing $spec" >&2; return 1; }
  fi
  # A drop-in leaves any containers.conf the deployment already owns untouched.
  if ! grep -qs "\"$spec_dir\"" "$config_home/containers/containers.conf" \
       "$config_home"/containers/containers.conf.d/*.conf /etc/containers/containers.conf; then
    mkdir -p "$(dirname "$dropin")"
    printf '[engine]\ncdi_spec_dirs = ["/etc/cdi", "/var/run/cdi", "%s"]\n' "$spec_dir" >"$dropin"
    echo "Registered $spec_dir as a cdi_spec_dirs entry in $dropin" >&2
  fi
}

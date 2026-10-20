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

# How long to let the engine shut down before the runtime escalates to SIGKILL.
# The runtime default is 10s and an idle engine here measures 13.9s to stop
# cleanly, so the default always escalates; see stop.sh for the full reasoning.
# STOP_TIMEOUT is the operator-facing name and wins, so one variable covers
# stop.sh and the REPLACE=1 handoff in start.sh alike.
QWEN38_STOP_TIMEOUT="${STOP_TIMEOUT:-${QWEN38_STOP_TIMEOUT:-90}}"

# Docker wants --gpus; podman's docker shim rejects its device= syntax and
# resolves GPUs through CDI instead.
qwen38_is_podman() { docker --version 2>/dev/null | grep -qi podman; }

# podman only reads CDI specs from the cdi_spec_dirs list in containers.conf,
# whose defaults (/etc/cdi and /var/run/cdi) are root-owned, so both the spec
# and its directory registration live under $HOME and need no root beyond
# installing the toolkit package.
#
# A spec is host state that goes stale, but regenerating unconditionally would
# be wrong: a deployment may narrow discovery to particular GPUs, and rewriting
# that silently widens GPU exposure on every launch. So instead check whether
# the spec is still satisfiable and only rewrite one this recipe generated.

# The host sources a spec names. Mounts carry hostPath; device nodes carry path,
# which CDI also treats as the host source when no hostPath is given. The
# optional list dash matters, and containerPath is deliberately not matched: it
# names the inside of the container, which need not exist on the host.
qwen38_cdi_stale_paths() {
  [[ -s "$1" ]] || return 0
  local source
  sed -nE 's/^[[:space:]]*(- )?(hostPath|path):[[:space:]]*(\/[^[:space:]]+).*$/\3/p' "$1" |
    sort -u | while read -r source; do [[ -e "$source" ]] || printf '%s\n' "$source"; done
}

# The digest recorded beside a spec we wrote. Hand-editing it, including to
# narrow which GPUs it exposes, changes the digest and transfers ownership to
# the deployment: a spec that is not ours is validated but never rewritten.
qwen38_cdi_ours() {
  [[ -f "$2" ]] && [[ "$(sha256sum "$1" | cut -d' ' -f1)" == "$(head -n1 "$2")" ]]
}

qwen38_ensure_cdi() {
  local config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
  local spec_dir="$config_home/cdi"
  local spec="$spec_dir/nvidia.yaml"
  local marker="$spec_dir/.nvidia.yaml.qwen38"
  local dropin="$config_home/containers/containers.conf.d/99-qwen38-nvidia-cdi.conf"
  local driver recorded missing fresh
  if ! command -v nvidia-ctk >/dev/null 2>&1 || ! command -v nvidia-cdi-hook >/dev/null 2>&1; then
    echo "nvidia-ctk and nvidia-cdi-hook are required for podman GPU passthrough and are not" >&2
    echo "installed. Neither Fedora nor RPM Fusion packages them; on Fedora 44 use the COPR:" >&2
    echo "  dnf copr enable @ai-ml/nvidia-container-toolkit" >&2
    echo "  dnf install nvidia-container-toolkit nvidia-container-toolkit-selinux" >&2
    echo "That is the last of three host steps; the RPM Fusion driver and the CUDA repo" >&2
    echo "come first. See docs/podman-rootless-fedora.md, 'Install (root, once)'." >&2
    return 1
  fi
  driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1 || true)"
  if [[ -f "$spec" ]] && ! qwen38_cdi_ours "$spec" "$marker"; then
    # Not ours: validate it, never rewrite it. Narrowing a spec is a way to
    # restrict which GPUs a container can reach, so replacing it would widen
    # that access silently, on a path the operator did not choose.
    missing="$(qwen38_cdi_stale_paths "$spec")"
    if [[ -n "$missing" ]]; then
      {
        echo "$spec names host paths that no longer exist, and was not generated by"
        echo "this recipe, so it is not being rewritten."
        printf '%s\n' "$missing" | sed 's/^/  missing /'
        echo "Regenerate it yourself, or delete it to let this recipe own it again."
      } >&2
      return 1
    fi
    echo "Using $spec as-is; it was not generated by this recipe" >&2
  else
    # Ours, or absent: regenerate every time. At 0.22s it costs nothing, and it
    # picks up what no staleness heuristic would catch: a relocated driver tree,
    # an added or replaced GPU, a MIG toggle, a persistenced or fabric manager
    # socket that started existing. Only report when something changed, so a
    # routine launch stays quiet.
    recorded="$(sed -n 2p "$marker" 2>/dev/null || true)"
    mkdir -p "$spec_dir" || return 1
    # Stage outside spec_dir: a partially written file there would end in .yaml
    # and be scanned as a second spec of the same kind, which podman rejects
    # outright. nvidia-ctk also rewrites an --output path whose extension it
    # does not recognise, appending .yaml, so the file it produces is not the
    # one we asked for. Generating as nvidia.yaml avoids that, and the move is
    # atomic because both sides are under $HOME.
    stage="$(mktemp -d "$config_home/.qwen38-cdi-XXXXXX")" || return 1
    if nvidia-ctk --quiet cdi generate --output="$stage/nvidia.yaml" && [[ -s "$stage/nvidia.yaml" ]]; then
      mv -f "$stage/nvidia.yaml" "$spec"
      rm -rf "$stage"
      printf '%s\n%s\n' "$(sha256sum "$spec" | cut -d' ' -f1)" "${driver:-unknown}" >"$marker"
      [[ -n "$recorded" && "$recorded" == "$driver" ]] \
        || echo "Generated $spec (driver ${recorded:-none} -> ${driver:-unknown})" >&2
    else
      rm -rf "$stage"
      # The previous spec was never opened by the generator, so it is intact;
      # keep serving from it while it still resolves rather than failing an
      # otherwise good launch.
      if [[ -s "$spec" ]] && [[ -z "$(qwen38_cdi_stale_paths "$spec")" ]]; then
        echo "nvidia-ctk cdi generate failed; continuing with the existing $spec" >&2
      else
        echo "nvidia-ctk cdi generate failed and no usable CDI spec remains" >&2
        return 1
      fi
    fi
  fi
  # A drop-in leaves any containers.conf the deployment already owns untouched.
  if ! grep -qs "\"$spec_dir\"" "$config_home/containers/containers.conf" \
       "$config_home"/containers/containers.conf.d/*.conf /etc/containers/containers.conf; then
    mkdir -p "$(dirname "$dropin")"
    printf '[engine]\ncdi_spec_dirs = ["/etc/cdi", "/var/run/cdi", "%s"]\n' "$spec_dir" >"$dropin"
    echo "Registered $spec_dir as a cdi_spec_dirs entry in $dropin" >&2
  fi
}

# Whole-and-tenths GiB from a KiB count, rounded, using only shell arithmetic:
# the preflight must not add a dependency the x86_64 path does not already have.
qwen38_gib() {
  local tenths=$(( ($1 * 10 + 524288) / 1048576 ))
  printf '%d.%d' $(( tenths / 10 )) $(( tenths % 10 ))
}

# Round up to the next power of two, which is what the pinned allocator does to
# every request. KiB inputs stay in 64-bit range well past any host we target.
qwen38_next_pow2() {
  local n="$1" p=1
  while (( p < n )); do p=$(( p << 1 )); done
  printf '%d' "$p"
}

# Preflight the pinned host memory the PLE table will need. With PLE_MMAP=0 the
# worker page-locks its shard of the n-gram table (torch.empty(pin_memory=True)) and
# reads it over UVA, so the binding constraint is host RAM rather than VRAM, and
# it fails about 40 s into engine initialisation as "torch.AcceleratorError: CUDA
# error: out of memory" -- which reads like a GPU fault and sends you hunting in
# the wrong place. Better to refuse before the container exists.
#
# Demand is the rounded size, not the checkpoint size. CachingHostAllocator.h
# rounds every pinned request with PowerOf2Ceil(), gated only on
# pinned_max_round_threshold() and pinned_max_cached_size(); both default to
# std::numeric_limits<size_t>::max() (AllocatorConfig.h:361,363) and neither has
# a parser, so libc10_cuda.so exposes only parsePinned{FreeCatchAll,
# NumRegisterThreads,ReserveSegmentSize,UseCudaHostRegister} and no
# PYTORCH_CUDA_ALLOC_CONF key can turn the rounding off. The BF16 table is 95.4
# GiB of checkpoint and 128 GiB of pin; the token embedding is 1.2 GiB and 2 GiB.
# The live exl3 worker on a 995 GiB host maps exactly 128.000 + 2.000 GiB of
# /dev/zero (deleted), which is the model with no free parameters.
#
# The old gate compared the checkpoint figure against 55% of installed RAM. That
# bracket was this same rounding seen from the wrong side: a 64 GiB pin succeeded
# and a 70 GiB one "failed" on a 125.68 GiB host not because of any 55% law but
# because 70 GiB rounds to 128 GiB, which does not fit. Rounding is also why the
# derived figure under-stated the live FP8 host (48.9 GiB derived against 66.18
# GiB of Shmem -- 64 GiB plus a 2 GiB embedding). Comparing rounded demand with
# installed RAM removes both the bogus constant and the 1.35x fudge.
#
# Where to put the line is a judgement, stated as one, and two kinds of evidence
# are kept apart. Highest share with a *serving engine* on it: 71.0% -- 130 GiB
# pinned on the 183 GiB qualification host; 52.5% (66 GiB on 125.68) serves today.
# Highest share merely *pinned successfully* by a probe: 77.5%, measured 2026-10-11
# on gg -- cudaHostAlloc of the 95.368 GiB BF16 table plus torch's 2 GiB rounded
# embedding, 97.38 GiB of Shmem on a 125.68 GiB host, 22.76 GiB still available,
# swap untouched, 42 s to page-lock. That is an allocation result, not a served
# request, so it deliberately does NOT move the warn tier: a serving engine also
# needs page cache, KV headroom and a kernel that can breathe, none of which the
# probe exercised. Recorded failure is 103.4% (130 GiB on 125.68); between 77.5% and
# 103.4% nothing has even been pinned. So refuse only what arithmetic condemns --
# demand landing within max(8 GiB, 5% of RAM) of installed -- and warn above 70%,
# just under the highest share that has actually served.
#
# Demand is read from the checkpoint so a geometry change needs no edit here.
qwen38_pinned_holders_present() {
  # 0 = some live process/container plausibly holds pinned/mlocked memory.
  docker ps -q 2>/dev/null | grep -q . && return 0
  local sum
  sum=$(awk '/^SummLocked:/{s+=$2} END{print s+0}' /proc/[0-9]*/status 2>/dev/null)
  (( ${sum:-0} > 8388608 ))   # >8 GiB locked by live processes
}

qwen38_ple_memcheck() {
  local config="$1" bytes="$2" memtotal_kib="$3" memavail_kib="$4" tp="${5:-1}"
  local dims ngram_rows ple_dim vocab hidden table_kib embed_kib total_kib
  local table_pin_kib embed_pin_kib demand_kib limit_kib warn_kib reserve_kib
  local table_gib total_gib mem_gib demand_gib table_pin_gib embed_pin_gib want_gib pct
  if [[ ! -r "$config" ]] || ! command -v python3 >/dev/null 2>&1; then
    echo "WARNING: cannot read $config, or no python3 to parse it; skipping the PLE memory preflight" >&2
    return 0
  fi
  if ! dims="$(python3 -c 'import json, sys
text = json.load(open(sys.argv[1]))["text_config"]
print(int(text["ngram_vocab_size_base"]), int(text["ple_embed_dim"]),
      int(text["vocab_size"]), int(text["hidden_size"]))' "$config" 2>/dev/null)"; then
    echo "WARNING: $config carries no PLE geometry I can read; skipping the PLE memory preflight" >&2
    return 0
  fi
  read -r ngram_rows ple_dim vocab hidden <<<"$dims"
  table_kib=$(( ngram_rows * ple_dim * bytes / 1024 ))
  # The token embedding table is pinned in host RAM in both PLE modes.
  embed_kib=$(( vocab * hidden * 2 / 1024 ))
  total_kib=$(( table_kib + embed_kib ))
  # With TP_SIZE>1 both tables are vocab-parallel: the n-gram worker pins its
  # checkpoint-shard/TP-range overlap (vllm models/qwen4_exp/common/ple.py) and
  # the host embedding pins the per-rank VocabParallelEmbedding weight
  # (patches/qwen_host_embedding.py pins layer.weight.shape). Demand is then
  # tp x round(size/tp) -- rounding is per rank, so two ranks pay 2 x 32 GiB for
  # a 47.7 GiB FP8 table, not round2(47.7). tp=1 reproduces the single-rank
  # figures; checkpoint row counts are TP-divisible by design
  # (make_ngram_vocab_size_divisible_by), so the ceil is exact arithmetic.
  local table_rank_kib embed_rank_kib
  table_rank_kib=$(( (table_kib + tp - 1) / tp ))
  embed_rank_kib=$(( (embed_kib + tp - 1) / tp ))
  # PLE_PIN_STRATEGY must agree with what the image's loader actually does; see
  # patches/port-ple-exact-pin.py. "exact" (the default) page-locks the table at its
  # true size via cudaHostAlloc, which does not round. "round2" is torch's pin_memory
  # path and rounds to a power of two. The token embedding is rounded in BOTH modes --
  # it still goes through pin_memory -- so only the table rounding is conditional.
  local ple_pin_strategy="${PLE_PIN_STRATEGY:-exact}"
  if [[ "$ple_pin_strategy" == "exact" ]]; then
    table_pin_kib=$(( table_rank_kib * tp ))
  else
    ple_pin_strategy="round2"
    table_pin_kib=$(( $(qwen38_next_pow2 "$table_rank_kib") * tp ))
  fi
  embed_pin_kib=$(( $(qwen38_next_pow2 "$embed_rank_kib") * tp ))
  demand_kib=$(( table_pin_kib + embed_pin_kib ))
  reserve_kib=$(( memtotal_kib * 5 / 100 ))
  (( reserve_kib < 8 * 1048576 )) && reserve_kib=$(( 8 * 1048576 ))
  limit_kib=$(( memtotal_kib - reserve_kib ))
  (( limit_kib < 0 )) && limit_kib=0
  warn_kib=$(( memtotal_kib * 70 / 100 ))
  table_gib="$(qwen38_gib "$table_kib")"
  total_gib="$(qwen38_gib "$total_kib")"
  mem_gib="$(qwen38_gib "$memtotal_kib")"
  demand_gib="$(qwen38_gib "$demand_kib")"
  table_pin_gib="$(qwen38_gib "$table_pin_kib")"
  embed_pin_gib="$(qwen38_gib "$embed_pin_kib")"
  pct=$(( ( demand_kib * 100 + memtotal_kib / 2 ) / memtotal_kib ))
  if (( demand_kib > limit_kib )); then
    want_gib="$(qwen38_gib "$(( demand_kib + reserve_kib ))")"
    {
      echo "Refusing to start: the PLE table is ${table_gib} GiB in the checkpoint, but with"
      echo "PLE_PIN_STRATEGY=${ple_pin_strategy} it needs ${table_pin_gib} GiB of pinned host memory"
      echo "(PyTorch's pin_memory path rounds every allocation up to a power of two)."
      echo "With the token embedding pinned at ${embed_pin_gib} GiB the host has to commit"
      echo "${demand_gib} GiB of pinned host memory (${pct}% of the ${mem_gib} GiB installed), and"
      echo "that leaves the kernel and the page cache too little to work with."
      echo "Choose one of:"
      echo "  QUANT=exl3-ple8   FP8 PLE table, half the pinned footprint"
      echo "  PLE_MMAP=1        map the table from the checkpoint; needs host-page-table dereference (DGX Spark)"
      echo "  a host with at least about ${want_gib} GiB of RAM for this profile"
      echo "Set QWEN38_SKIP_MEMCHECK=1 to start anyway."
    } >&2
    return 1
  fi
  if (( demand_kib > memavail_kib )); then
    # Warn, do not refuse. MemAvailable is a snapshot, and a working host can be
    # momentarily below it -- another engine holding its pinned table, or caches
    # not yet reclaimed after a restart. Refusing here blocked a profile that was
    # serving at that moment, so the hard gate stays with installed RAM.
    if qwen38_pinned_holders_present; then
      echo "WARNING: this profile pins ${demand_gib} GiB but the host reports only $(
        qwen38_gib "$memavail_kib") GiB available, and a live process/container holds" >&2
      echo "  locked memory. Start will likely fail when the table is pinned; drop the" >&2
      echo "  other engine first." >&2
    else
      # No holder: the shortfall is teardown lag (kernel still releasing a dead
      # engine's cudaHostAlloc pages) or reclaimable cache. Wait it out before
      # proceeding, so container launches never start inside the unpin window.
      local pin_wait="${PIN_WAIT:-180}" waited=0 now_kib=$memavail_kib
      echo "NOTE: pinned-RAM demand ${demand_gib} GiB exceeds MemAvailable $(
        qwen38_gib "$memavail_kib") GiB with no live holder; waiting up to ${pin_wait}s" >&2
      echo "  for post-teardown unpin to settle (PIN_WAIT=0 disables)..." >&2
      while (( waited < pin_wait )) && (( demand_kib > now_kib )); do
        sleep 5; waited=$(( waited + 5 ))
        now_kib=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
      done
      if (( demand_kib > now_kib )); then
        echo "WARNING: still only $(qwen38_gib "$now_kib") GiB available after ${waited}s" >&2
        echo "  (Mlocked=$(awk '/^Mlocked:/{print $2/1048576}' /proc/meminfo) GiB). Starting anyway;" >&2
        echo "  expect pin failure unless memory frees." >&2
      else
        echo "NOTE: headroom recovered to $(qwen38_gib "$now_kib") GiB after ${waited}s; proceeding." >&2
      fi
    fi
  fi
  if (( demand_kib > warn_kib )); then
    echo "WARNING: this profile pins ${demand_gib} GiB, ${pct}% of the ${mem_gib} GiB installed." >&2
    echo "  The highest share that has actually served an engine is 71.0% (130 GiB on a 183" >&2
    echo "  GiB host). 77.5% has been pinned by a probe but never served; above 77.5% nothing" >&2
    echo "  has been pinned at all. Expect failure if anything else holds pinned memory." >&2
  fi
  if [[ "$ple_pin_strategy" == "exact" ]]; then
    # The fallback figure is what an image WITHOUT the port actually pins: torch rounds each rank's
    # allocation up to a power of two. Quoting table_pin_gib here was wrong -- under exact it equals the
    # table itself, so the warning read "pins 95.4 GiB as 95.4 GiB", which says nothing. The point of the
    # note is the concrete cost of silently missing the port, so the note must print the rounded size.
    local fallback_pin_gib
    fallback_pin_gib="$(qwen38_next_pow2 "$table_rank_kib")"
    fallback_pin_gib="$(qwen38_gib "$(( fallback_pin_gib * tp ))")"
    echo "NOTE: PLE_PIN_STRATEGY=exact is only honoured by a build carrying the" >&2
    echo "  qwen_pinned_host port. On an image without it the variable is ignored and" >&2
    echo "  the table pins ${table_gib} GiB as ${fallback_pin_gib} GiB instead -- this preflight" >&2
    echo "  cannot see inside the image, so confirm the loader logs the exact-size line." >&2
  fi
  echo "PLE pinned host memory (strategy ${ple_pin_strategy}, TP ${tp}): table ${table_gib} GiB pins" \
     "${table_pin_gib} GiB ($(qwen38_gib "$(( table_pin_kib / tp ))")/rank x ${tp}); with token embeddings" \
     "${demand_gib} GiB pinned against ${total_gib} GiB of checkpoint, on ${mem_gib} GiB installed (${pct}%)." >&2
}

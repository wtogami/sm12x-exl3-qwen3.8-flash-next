# Rootless podman on Fedora 44: host prerequisites

Scope: running `build.sh` / `download.sh` / `start.sh` through podman's docker
shim as an unprivileged user. Reference host: `gg`, Fedora 44 Workstation,
kernel 7.2.9, podman 5.8.7 / crun 1.28, SELinux enforcing, cgroup v2, btrfs
`/home`, RPM Fusion NVIDIA driver 615.71.09
(`akmod-nvidia`, `xorg-x11-drv-nvidia-cuda`), NVIDIA `cuda-toolkit-13.4.2`,
RTX PRO 6000 Blackwell.

## What needs root

| Item | Root? |
| --- | --- |
| `./build.sh` image build | no |
| NVIDIA driver from RPM Fusion (`akmod-nvidia`) | yes, plus a reboot |
| Secure Boot kmod signing, if Secure Boot is on | yes, before the driver |
| CUDA repo + `cuda-toolkit` (optional, see below) | yes |
| `dnf copr enable @ai-ml/nvidia-container-toolkit` + install | yes |
| SELinux boolean for `/dev/nvidia*` | yes, but the toolkit package does it |
| Generate the CDI spec | no |
| Register the spec directory with podman | no |
| `./download.sh` checkpoint download | no |
| Raise `RLIMIT_MEMLOCK` | not needed, see below |

Everything below the install step is handled automatically by `build.sh`.

## `./build.sh` needs no host GPU support

All 26 build steps complete without a GPU, and three CUDA messages they print
look worse than they are:

```
W1010 21:35:11 torch/utils/cpp_extension.py:178] No CUDA runtime is found, using CUDA_HOME='/usr/local/cuda'
Failed to import from vllm._C: ModuleNotFoundError("No module named 'vllm._C'")
Failed to import the DeepSelect extension (vllm._deepselect_C): libcuda.so.1: cannot open shared object file: No such file or directory
```

Neither docker nor podman attaches a GPU or a driver to a `RUN` step, so every
`python3` in the Dockerfile runs in a driver-less sandbox:

- **PyTorch "No CUDA runtime is found"** is a warning (`W` prefix) from
  `torch.utils.cpp_extension` at import time. The same line reports that it
  *did* resolve `CUDA_HOME=/usr/local/cuda`, the CUDA 13.0.2 toolkit inside the
  `vllm/vllm-openai` base image.
- **`No module named 'vllm._C'`** comes from `vllm/platforms/cpu.py:626`. With
  no device visible vLLM selects the CPU platform, whose optional-op loader
  probes libraries a CUDA wheel does not ship. vLLM 0.31 builds its CUDA ops as
  `_C_stable_libtorch.abi3.so`; there is no `vllm/_C.abi3.so` even on a working
  host.
- **`libcuda.so.1: cannot open shared object file`** is the real signal and is
  by design. `libcuda.so.1` is the closed-source driver library. It is never in
  the image: the container runtime bind-mounts it from the host into
  `/usr/local/nvidia/lib64`, the first entry on the image's `LD_LIBRARY_PATH`,
  when a container *starts*. `/usr/local/nvidia` does not exist in the image at
  all, and `_deepselect_C.abi3.so` needs it at import time.

The host's own CUDA path is irrelevant: `gg` has `/usr/local/cuda-13.4`, the
container has `/usr/local/cuda` 13.0.2, and the container never reads the host
toolkit. Nothing here needs fixing, and adding a GPU-attached builder stage
would buy nothing — every build step is a pip install, a source patch, or a
CPU-only test suite.

## GPU passthrough: CDI

`start.sh` asks for the GPU with `--device nvidia.com/gpu=all` (commit
6d28aab57606), which podman resolves through a CDI spec.

### Install (root, once)

Three installs, in this order: the driver from RPM Fusion, then the CUDA
repository with NVIDIA's driver packages excluded, then the container toolkit.
The order is not ceremonial -- the CUDA repository also ships a complete NVIDIA
driver userspace, and step 2 exists mainly to stop that from landing on top of
the step 1 driver.

Follow the upstream pages rather than this summary; both are living documents
and this section is a snapshot of them as of 2026-10-10:

- driver: <https://rpmfusion.org/Howto/NVIDIA>, section *Current
  GeForce/Quadro/Tesla* (page last edited 2026-10-05)
- CUDA: <https://rpmfusion.org/Howto/CUDA>, section *CUDA Toolkit*, the *Fedora
  42 and later with DNF-5* variant (page last edited 2025-10-01)

**Prerequisites.** RPM Fusion nonfree must already be enabled, per their
Configuration page; `dnf repolist` should list `rpmfusion-nonfree-nvidia-driver`.
And if Secure Boot is on -- it is on `gg`, `mokutil --sb-state` reports
`SecureBoot enabled` -- the howto requires the kmod signing key to be enrolled
**before** the driver is installed. The failure mode is the module refusing to
load, which presents as absent hardware rather than a signature error.

**1. Driver, from RPM Fusion.** As root, following the NVIDIA howto:

```sh
dnf update -y                             # and reboot if not on the latest kernel
dnf install akmod-nvidia
dnf install xorg-x11-drv-nvidia-cuda      # CUDA / NVENC / NVDEC userspace
dnf mark user akmod-nvidia                # DNF5, Fedora 41+: stops autoremove removing it
reboot                                    # the howto: a full reboot, on the newest kernel
```

Rebooting is not optional, and neither is the wait afterwards: `akmods` builds
the kmod for the running kernel, which the howto says can take five minutes.
Check it finished before going further:

```
$ modinfo -F version nvidia
615.71.09
$ grep "NVRM version" /proc/driver/nvidia/version
NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  615.71.09  Release Build  ...
```

`modinfo: ERROR: Module nvidia not found` means the build is still running or the
kmod is not loaded. "Open Kernel Module" is the expected reading, not a
misconfiguration: Blackwell needs the open module and RPM Fusion builds that by
default. Both outputs above are from `gg`.

**2. CUDA toolkit, from NVIDIA's repo, with its driver excluded.** As root:

```sh
dnf config-manager addrepo --from-repofile=https://developer.download.nvidia.com/compute/cuda/repos/fedora44/$(uname -m)/cuda-fedora44.repo
dnf clean all
dnf config-manager setopt 'cuda-fedora44-x86_64.exclude=nvidia-driver*,cuda-drivers*,nvidia-modprobe,nvidia-persistenced,nvidia-settings,nvidia-libXNVCtrl*,nvidia-xconfig,nvidia-open,nvidia-imex,nvidia-kmod-common,libnvidia-fbc,kmod-nvidia-*-dkms,nvidia-fs*,nvidia-gds*,xorg-x11-nvidia*'
dnf install -y cuda-toolkit xorg-x11-drv-nvidia-cuda
```

The repository file name follows the release: the howto shows `fedora42`, `gg`
uses `fedora44`. Verify both halves of the result:

```
$ rpm -q cuda-toolkit
cuda-toolkit-13.4.2-1.x86_64
$ dnf repoquery --repo cuda-fedora44-x86_64 nvidia-driver-libs
$                                  # empty: the exclusion is in effect
```

*The howto's own exclude list is not sufficient* -- tested, not inferred. It
reads `exclude=nvidia-driver,nvidia-modprobe,...` with no wildcards, and an exact
name does not match `nvidia-driver-libs`. Checked against the repository with
`gg`'s override file bypassed, by pointing a throwaway repo alias at the same
baseurl:

```
$ BASE=https://developer.download.nvidia.com/compute/cuda/repos/fedora44/x86_64
$ dnf repoquery --repofrompath=cudatest,$BASE --repo cudatest --nogpgcheck \
    --setopt='cudatest.exclude=nvidia-driver,cuda-drivers,nvidia-modprobe' nvidia-driver-libs
nvidia-driver-libs-3:610.43.02-1.fc44.i686
nvidia-driver-libs-3:610.43.02-1.fc44.x86_64      # howto's exact names: still admitted
$ dnf repoquery --repofrompath=cudatest,$BASE --repo cudatest --nogpgcheck \
    --setopt='cudatest.exclude=nvidia-driver*' nvidia-driver-libs
                                                  # globbed: excluded
```

`nvidia-driver-libs` and `nvidia-driver-cuda-libs` are precisely the packages
that would land on top of the RPM Fusion userspace, and the repository offers
nine distinct `nvidia-driver*` names in its metadata. It also carries driver
series older than the one in use here -- 610.43.02 against RPM Fusion's 615.71.09
-- so admitting it is a downgrade as well as a mismatch. Use the globs above,
which are `gg`'s live configuration.

Also note where DNF5 puts it, because it is a false-negative when auditing a
host: `config-manager setopt` writes
`/etc/dnf/repos.override.d/99-config_manager.repo`, not
`/etc/yum.repos.d/cuda-fedora44.repo`. Grepping the `.repo` file for `exclude`
finds nothing on a correctly configured machine.

The host toolkit is **optional for this recipe** -- the image brings its own
CUDA, and the driver is the only hard requirement. Install it if you compile
CUDA code on the host. If you do, `nvcc` is not on `PATH` by default; it lives at
`/usr/local/cuda/bin/nvcc`, so `rpm -q cuda-toolkit` is the check for
installation, not `command -v nvcc` (`gg` has the toolkit installed and reports
no `nvcc` on `PATH`). The howto's *GCC version* section covers the case where
the repository's toolkit is older than Fedora's compiler.

**3. Container toolkit.** Fedora 44 and RPM Fusion do not package
`nvidia-container-toolkit`;
`dnf list available 'nvidia-container-toolkit*' 'libnvidia-container*'` returns
nothing with `fedora`, `updates`, and `rpmfusion-nonfree-nvidia-driver`
enabled. It ships from NVIDIA's own repo or the `@ai-ml` COPR, and the COPR is
what the RPM Fusion CUDA howto itself recommends under *Community repositories*:

```sh
dnf copr enable @ai-ml/nvidia-container-toolkit
dnf install nvidia-container-toolkit nvidia-container-toolkit-selinux
```

Verified: COPR `nvidia-container-toolkit-1.20.1-1.fc44`. At 1.20 the
`libnvidia-container1` / `libnvidia-container-tools` packages are gone; there
is no `nvidia-container-cli` binary and nothing needs it, because CDI discovery
runs through NVML in-process.

Keep the driver itself from RPM Fusion -- step 1, not step 2 and not this repo.
That package is correctly scoped: it supplies `/usr/lib64/libcuda.so.1`,
`libnvidia-ml.so.1`, `libnvidia-ptxjitcompiler.so.1` and the rest of the set the
CDI spec mounts. Only the toolkit comes from elsewhere; mixing an NVIDIA-repo
driver userspace with the RPM Fusion kernel module is what breaks, which is why
step 2 excludes it rather than merely preferring not to install it.

### SELinux (root, and already automatic)

Fedora labels the NVIDIA device nodes `xserver_misc_device_t`:

```
system_u:object_r:xserver_misc_device_t:s0 /dev/nvidia0
unconfined_u:object_r:xserver_misc_device_t:s0 /dev/nvidia-uvm
```

The `container_t` domain cannot open them unless the
`container_use_xserver_devices` boolean is on. Without it, container setup
fails with `ls: cannot access '/dev/nvidia0': Permission denied`.

`nvidia-container-toolkit-selinux` enables that boolean in its scriptlet, so
installing it is the whole fix — no manual `setsebool`. Confirmed on `gg` after
install, having changed nothing by hand:

```
$ getsebool container_use_xserver_devices
container_use_xserver_devices --> on
```

Note this is a system-wide allowance for all containers, as the package's own
README states. The same package ships a restrictive alternative, a
`nvidia_container_t` policy type used via
`--security-opt label=type:nvidia_container_t`, if that global grant is not
acceptable. `--security-opt label=disable` is the debug escape hatch and is
strictly weaker isolation; do not ship it.

### Spec generation and registration (no root)

Two facts that are easy to get wrong:

1. **`nvidia-ctk cdi generate` runs unprivileged.** It only queries the driver
   through NVML, so a user-generated spec is complete — the one on `gg` carries
   60 driver mounts, the `0`, UUID, and `all` entities, and all six CDI hooks.
   Generating into `/etc/cdi` is what needs root, and it is unnecessary.
2. **podman does not look in `~/.config/cdi`.** Its spec directories come from
   `cdi_spec_dirs` in the `[engine]` section of `containers.conf`, whose
   defaults are `/etc/cdi` and `/var/run/cdi` only. A user-generated spec that
   podman never scans produces the misleading

   ```
   Error: setting up CDI devices: unresolvable CDI devices nvidia.com/gpu=all
   ```

   Registration goes in a drop-in so an existing `containers.conf` stays
   untouched — podman reads `~/.config/containers/containers.conf.d/*.conf`:

   ```toml
   [engine]
   cdi_spec_dirs = ["/etc/cdi", "/var/run/cdi", "/home/rtx/.config/cdi"]
   ```

`build.sh` and `start.sh` perform both steps on a podman host through
`qwen38_ensure_cdi` in `platform-config.sh`.

**That function is not a read-only probe, so it is a misleading thing to call
while debugging.** It always writes the spec and always checks the drop-in, which
means sourcing `platform-config.sh` and calling it to look at current state
changes the host: on any machine that merely happens to have `nvidia-ctk`
installed, it produces `~/.config/cdi/nvidia.yaml`, its `.nvidia.yaml.qwen38`
marker, and registers the directory with podman. That is easy to do by accident on
a workstation, and it succeeds quietly, which reads as evidence about the
deployment you are actually investigating when it is evidence only about the local
driver. To see what it would do without touching anything:

```sh
env -u XDG_CONFIG_HOME HOME=$(mktemp -d) bash -c \
  'source ./platform-config.sh; qwen38_ensure_cdi'
```

`XDG_CONFIG_HOME` is honoured ahead of `HOME` and must be unset for the sandbox to
hold.

To read state instead of provoking it, the spec and marker are plain files under
`~/.config/cdi/`. Do not use `nvidia-ctk cdi list` to ask what podman can resolve:
it scans only its own default directories, `/etc/cdi` and `/var/run/cdi`, and says
nothing about a spec registered through `cdi_spec_dirs`. On `gg`, where podman
resolves the device without complaint, it reports:

```
$ nvidia-ctk cdi list
INFO[0000] Found 0 CDI devices
```

`podman info` does not expose `cdi_spec_dirs` either. The only command that
actually answers the question is the launch itself -- see the `--entrypoint
nvidia-smi` check at the end of this document, which proves the GPU is visible
rather than merely that the entity resolved.

### Regeneration policy: regenerate ours, validate theirs

Regenerating unconditionally is the transparent answer to driver updates, and
at 0.22s it costs nothing. But it is the wrong answer applied to the wrong file:
a spec is also an access-control artefact. Narrowing discovery to particular
GPUs — `nvidia-ctk cdi generate --device-ids 0`, or dropping the `all` entity —
is how you stop a container reaching GPUs it was never given, and CDI enforces
that at the device node and cgroup level, unlike `NVIDIA_VISIBLE_DEVICES`,
which is a soft in-container filter. Rewriting such a spec silently widens GPU
exposure, on a path the operator did not choose, and the only symptom is a GPU
showing up in `nvidia-smi`.

So ownership decides the behaviour, recorded as the spec's digest in
`~/.config/cdi/.nvidia.yaml.qwen38`:

- **Absent, or ours** → regenerate on every run. Generation writes a temp file
  and moves it into place, so a concurrent start never sees a partial spec and a
  failed generator cannot truncate a good one. Output stays quiet unless the
  driver version actually changed.
- **Not ours** (never generated here, or hand-edited since — the digest catches
  both) → validated, never rewritten. If every host path it names still resolves,
  it is used as-is. If not, `start.sh` fails naming each missing path and says
  how to proceed, rather than quietly replacing it.
- **Generator fails** → if the previous spec still resolves, keep serving from
  it with a warning; only fail when nothing usable remains.

The host-path check reads `hostPath` from mounts and `path` from device nodes,
which CDI treats as the host source when no `hostPath` is given, and ignores
`containerPath`. On `gg` that is 60 + 24 entries, 67 unique, resolved in under a
millisecond.

Handing a spec to the recipe, or taking it back:

```sh
rm ~/.config/cdi/nvidia.yaml ~/.config/cdi/.nvidia.yaml.qwen38 && ./build.sh   # recipe owns it
nvidia-ctk cdi generate --device-ids 0 --output=~/.config/cdi/nvidia.yaml      # you own it
```

Narrow by device id rather than by deleting the `all` entity. `--device-ids 0`
still emits an `all` entity scoped to that device, so `start.sh`, which always
asks for `nvidia.com/gpu=all`, keeps working. Delete `all` instead and podman
reports the device unresolvable and `start.sh` stops — correctly, but confusingly,
since the spec looks fine.

An existing spec written before this policy existed has no digest and is
therefore treated as yours until you delete it once and let `build.sh` recreate
it.

### What regeneration picks up

Because our own spec is rebuilt from live host state, all of these are handled
without a heuristic:

- **Any driver update — the common case.** The spec embeds 103 host paths
  carrying the driver version: `/usr/lib64/libcuda.so.615.71.09`,
  `/lib/firmware/nvidia/615.71.09/gsp_ga10x.bin`, and so on. A stale one fails
  during CDI setup, before any process starts. This also covers switching driver
  *source*, RPM Fusion to a `.run` installer, which relocates the libraries at
  the same version — a case a version comparison gets wrong in both directions.
- **Libraries and sockets the new driver adds.** An existence check can only see
  paths the old spec already named, so additions are invisible to it by
  definition. Regeneration finds them: `/nvidia-persistenced/socket`,
  `/nvidia-fabricmanager/socket`, `/tmp/nvidia-cuda-mps`, `nvidia-imex`.
- **GPU topology**: adding, removing or replacing a GPU, MIG enable/disable, and
  DRM node renumbering (`/dev/dri/renderD128` and its GID are in the spec).
- **A podman or toolkit upgrade** moving the supported `cdiVersion`.

Nothing to do for: rebuilding or re-pulling the image, a reboot, or changing
`QUANT`, `GPU`, or any model argument.

## Rootless bind mounts: `download.sh` and `start.sh`

Mounting a host cache into a rootless container hits two unrelated faults that
produce the identical `PermissionError: [Errno 13] Permission denied`. All three
scripts now carry the fix; `download.sh` was the first to show it.

**Container UIDs are not host UIDs.** `download.sh` pins the container user with
`--user $(id -u):$(id -g)`, which is right under docker — it shares the host uid
namespace, so uid 1002 is uid 1002 and the cache stays user-owned rather than
root-owned. Rootless podman instead maps container uid 1..65535 into the
`$USER` subuid range (`rtx:655360:65536`), so container uid 1002 is host uid
656361 and cannot write a directory owned by `rtx`. `--userns=keep-id` maps the
invoking user through at the same uid and restores the intent. Container uid 0
is the one uid that maps to the host user without keep-id, which is why the same
mount looks writable the moment `--user` is dropped and is not evidence that
`--user` is harmless.

**SELinux labels on bind mounts are separate from DAC.** A bind mount keeps its
host label, `cache_home_t` for `~/.cache/huggingface`, and `container_t` cannot
write it. The mount option fixes it: `:z` relabels to the shared
`container_file_t:s0`. Use `:z`, not `:Z` — `:Z` assigns a per-container MCS
category, so `download.sh` and `start.sh` would each be locked out of the other's
relabel, and they share the same cache. `:z` is ignored on hosts without
SELinux.

Relabeling is recursive and persists on the host directory; `restorecon -R
~/.cache/huggingface` undoes it. Files still land owned by `rtx:rtx`:

```
-rw-r--r--. 1 rtx rtx system_u:object_r:container_file_t:s0 201279328 ...
```

`start.sh` needs no `--userns=keep-id`: it deliberately runs as container root so
the image's `/root/.cache` paths resolve, and under rootless podman container uid
0 is already the invoking user. Verified — its cache writes arrive as `rtx:rtx`.

## Docker behaviour

**No docker host was available, so the docker branch of these scripts is
reasoned about, not tested.** Everything below describes what the code does and
what was read in upstream sources; none of it was executed against dockerd.

Every podman-specific change is behind `qwen38_is_podman`, so a docker host
receives the same command line it did before, except where noted:

| | docker | rootless podman |
| --- | --- | --- |
| GPU | `--gpus device=$GPU` | `--device nvidia.com/gpu=all` |
| CDI provisioning | not run | spec + `containers.conf.d` drop-in |
| `download.sh` user namespace | none | `--userns=keep-id` |
| `--user $(id -u):$(id -g)` | unchanged | unchanged |
| cache mounts | **`:z` added** | **`:z` added** |

The two branches are not interchangeable and should not be "unified" on
`--device`. `--gpus` resolves through whichever mechanism the engine offers:
moby maps it onto CDI when a spec is present and falls back to the prestart hook
when none is. `--device` requires a spec in a directory that engine scans, and
this recipe generates its spec under `$HOME`, which rootful docker does not read.
Switching the docker branch would therefore add a root-time `/etc/cdi`
provisioning step, add a stale-spec failure mode, and raise the docker floor to
28.2.0 (released 2025-05-28, when CDI became default-on; the capability existed
from 25.0 but behind `--experimental`) for no benefit.

One change is deliberately unconditional: the `:z` mount option. It is a
standard volume mode that docker also accepts, and it is a no-op wherever
SELinux is off, which is the common docker deployment. Where docker runs on an
SELinux-enforcing host the relabel is required there too, so gating it would
only reintroduce the same bug on a different engine.

The added `HF_TOKEN` passthrough is likewise engine-independent and strictly
additive: the token was never forwarded before, so gated checkpoints could not
be fetched at all. It is passed by name (`-e HF_TOKEN`), so the secret does not
appear in the image history or in the container's argv.

### One caveat about the COPR build

The `@ai-ml` COPR package set installed on the reference host contains no
`nvidia-container-cli`: `rpm -q --whatprovides nvidia-container-cli` reports no
provider, `dnf repoquery` for it and for `libnvidia-container*` returns nothing
from any enabled repo, and no such file exists on disk. The prestart hook binary
is still installed and still references that name.

That is the observed state. What it implies for `docker --gpus`, which reaches
GPU access through that hook on older engines, was **not tested** and should not
be inferred from this document. Podman via CDI is unaffected, because CDI does
not use that binary; that is the path used throughout this doc and every number
in it.

## Host pinned memory for the PLE table

Single-GPU serving here is limited by host RAM, not VRAM. With `PLE_MMAP=0` the
worker page-locks the entire n-gram table
(`allocate_embedding_weight` calls `torch.empty(pin_memory=True)`) and reads it
over UVA. Failure appears about 40 seconds into engine initialisation as:

```
torch.AcceleratorError: CUDA error: out of memory
```

which reads like a GPU fault and sends you looking in the wrong place. Table
size is `ngram_vocab_size_base x ple_embed_dim x bytes-per-element`, taken from
the checkpoint headers rather than estimated:

| profile | PLE dtype | table in checkpoint | **actually pinned** |
| --- | --- | --- | --- |
| `exl3`, `nvfp4-redhatai` | BF16 | 102.40 GB = 95.40 GiB | **128 GiB** |
| `exl3-ple8`, `nvfp4` | FP8 E4M3 + one global scale | 51.20 GB = 47.68 GiB | **64 GiB** |

The last column is the one that has to fit in RAM. The token embedding table adds
1.18 GiB of checkpoint and 2 GiB of pin, and it is pinned in host RAM in both PLE
modes, so the whole request is 130 GiB for a BF16 profile and 66 GiB for FP8.

**PyTorch rounds every pinned allocation up to a power of two, and you cannot turn
it off.** `CachingHostAllocator.h` applies `PowerOf2Ceil()` to each request, gated
only on `pinned_max_round_threshold()` and `pinned_max_cached_size()`; both default
to `std::numeric_limits<size_t>::max()` (`AllocatorConfig.h:361,363`) and neither
has a config parser -- `libc10_cuda.so` defines exactly four,
`parsePinned{FreeCatchAll,NumRegisterThreads,ReserveSegmentSize,UseCudaHostRegister}`
-- so no `PYTORCH_CUDA_ALLOC_CONF` key reaches the rounding. A 95.4 GiB table
therefore costs 128 GiB, a 1.34x penalty, and 47.68 GiB costs 64 GiB, 1.34x.
Confirmed on a running engine rather than inferred: the `exl3` worker on a 995 GiB
host holds exactly two mappings above 1 GiB of anonymous `/dev/zero (deleted)`,
128.000 GiB and 2.000 GiB, totalling 130.000 GiB.

`start.sh` refuses before creating the container when that rounded demand lands
within `max(8 GiB, 5% of RAM)` of installed memory. It **warns** above 70% of
installed RAM, and separately when demand exceeds `MemAvailable`, which is a
snapshot -- a host can be momentarily under it while still serving fine, and
refusing there once blocked the FP8 profile on a host where it was running. It
reads the geometry from the checkpoint, so a checkpoint change needs no edit here.
Skip it with `QWEN38_SKIP_MEMCHECK=1`; it is skipped automatically for
`PLE_MMAP=1` (no table-sized pinned allocation) and for a caller-supplied
`--engram-config` (placement unknown).

**This replaces a 55%-of-RAM bracket that was the same rounding seen from the wrong
side.** The evidence behind it was that on a 125.68 GiB host "64 GiB pins fine and
70 GiB fails with 121 GiB still available": 64 GiB is already a power of two and
stays 64, while 70 GiB rounds to 128 GiB, which does not fit in 125.68 GiB. There
was never a 55% law, and comparing rounded demand with installed RAM explains both
outcomes plus the one that previously looked like under-reporting -- the live FP8
engine's 66.18 GiB of system `Shmem` against 48.87 GiB derived is exactly 64 + 2.
The old gate's advice, that BF16 needs "about 173 GiB", is likewise replaced by
138 GiB, which is the rounded demand plus the reserve.

Three alternatives remain ruled out, so do not chase them: cgroups
(`memory.max = max` both in-container and in the user slice), `RLIMIT_MEMLOCK`
(`Mlocked: 48 kB`, and 64 GiB pins at an 8 MiB soft limit), and commit accounting
(live `Committed_AS` 80.15 GiB against a 70.84 GiB `CommitLimit` while healthy).

Where the real boundary sits is still not probed, and is stated as judgement rather
than measurement. Recorded outcomes are 130 GiB pinned on 183 GiB (71.0% of RAM, the
qualification host, served fine), 66 GiB on 125.68 GiB (52.5%, serving now), and
130 GiB on 125.68 GiB (103.4%, impossible). Anything between 71% and 103% is
untested, which is why the warn tier sits at 70% and the refuse tier only where
arithmetic condemns the attempt.

Two consequences worth knowing when sizing or monitoring:

- **`RssShmem` and RSS both under-report pinned memory.** On the FP8 host the worker
  mapped 41.56 GiB of the 66.18 GiB, because driver-side pinned pages are counted in
  `Shmem` before any process maps them. About 24.6 GiB was not attributable to any
  readable process, and the remaining processes are root-owned, so closing that gap
  needs root. Size and monitor hosts from system `Shmem`, not from process RSS.
- **Reading a mapping's size is enough to audit the gate.** Sum the anonymous
  `/dev/zero (deleted)` mappings above 1 GiB in the worker's `/proc/<pid>/maps`
  *on the host* -- container PIDs are in a different namespace. Each should be a
  power of two; if the total matches `next_pow2(table) + next_pow2(embedding)` the
  preflight's arithmetic is right for that checkpoint.

## Checked and *not* a problem

- **`RLIMIT_MEMLOCK` = 8 MiB**, inherited into the container, looks alarming
  because `patches/qwen_host_embedding.py` allocates `pin_memory=True` buffers.
  Driver 615 does not charge pinned host memory against it: 16 MiB, 256 MiB and
  1 GiB pinned allocations all succeeded at the default limit, and a 64 GiB
  single allocation succeeded later. No `limits.d` or `DefaultLimitMEMLOCK`
  change is warranted. Pinned memory *is* a real constraint -- see "Host pinned
  memory for the PLE table" -- but this rlimit is not the thing enforcing it.
- **cgroup v2**, `user.max_user_namespaces` = 514446, and `--ipc=host` all work
  rootless as shipped; no `--shm-size` workaround needed.
- **`# syntax=docker/dockerfile:1.7`** is ignored by `podman build`, and podman
  5.8 supports the one BuildKit-ism actually used, the `RUN` heredoc at step 6.
- **Image tagging**: podman reports `localhost/qwen38-rtx:local`; the prefix is
  implicit when the scripts name `qwen38-rtx:local`.
- **Model access**: the `exl3` checkpoint is public. `HF_TOKEN` is optional and
  only raises rate limits, though gated checkpoints on other profiles need it.

## Checklist for a fresh Fedora 44 rootless podman host

As root, once, in this order -- see [Install (root, once)](#install-root-once)
for why each step is where it is, and for the verification output that belongs
with it:

```sh
# 0. prerequisite: RPM Fusion nonfree enabled, and Secure Boot kmod signing
#    enrolled if Secure Boot is on. Then be on the latest kernel:
dnf update -y
reboot

# 1. driver, from RPM Fusion -- akmods builds the kmod for the running kernel
dnf install -y akmod-nvidia xorg-x11-drv-nvidia-cuda
dnf mark user akmod-nvidia
reboot
#    verify before continuing (build can take minutes):
#      modinfo -F version nvidia            -> e.g. 615.71.09
#      grep "NVRM version" /proc/driver/nvidia/version   -> "Open Kernel Module"

# 2. CUDA toolkit -- optional; only needed to compile CUDA on the host. The
#    exclude is what keeps NVIDIA's driver off the RPM Fusion kernel module.
dnf config-manager addrepo --from-repofile=https://developer.download.nvidia.com/compute/cuda/repos/fedora44/$(uname -m)/cuda-fedora44.repo
dnf clean all
dnf config-manager setopt 'cuda-fedora44-x86_64.exclude=nvidia-driver*,cuda-drivers*,nvidia-modprobe,nvidia-persistenced,nvidia-settings,nvidia-libXNVCtrl*,nvidia-xconfig,nvidia-open,nvidia-imex,nvidia-kmod-common,libnvidia-fbc,kmod-nvidia-*-dkms,nvidia-fs*,nvidia-gds*,xorg-x11-nvidia*'
dnf install -y cuda-toolkit xorg-x11-drv-nvidia-cuda

# 3. container toolkit, last, and from the COPR the RPM Fusion CUDA howto names
dnf copr enable @ai-ml/nvidia-container-toolkit
dnf install -y nvidia-container-toolkit nvidia-container-toolkit-selinux   # also sets the SELinux boolean
systemctl enable --now nvidia-persistenced                                 # optional, removes first-request latency
```

Steps 0 and 1 are required. Step 2 is not: the image carries its own CUDA, so the
host toolkit matters only if something compiles against it here. The driver,
however, must be RPM Fusion's whichever way you install it -- that is what step 2
excludes and what the note after step 3 assumes.

As the deploying user:

```sh
./build.sh      # also generates ~/.config/cdi/nvidia.yaml and its containers.conf.d drop-in
./download.sh   # QUANT=exl3|exl3-ple8|nvfp4|nvfp4-redhatai
./start.sh
./stop.sh       # same QUANT as start.sh; see README for the grace period
```

Both `start.sh` and `stop.sh` take the container name from `QUANT`. A stopped
container left behind is removed automatically by the next `start.sh`; a running
one is refused unless `REPLACE=1`, and neither silently `SIGKILL`s the engine --
the runtime's 10 s default is shorter than this workload needs to shut down.

Verify the GPU reached the container before blaming the image:

```sh
docker run --rm --device nvidia.com/gpu=all --entrypoint nvidia-smi qwen38-rtx:local -L
# GPU 0: NVIDIA RTX PRO 6000 Blackwell Workstation Edition (UUID: GPU-4178...)
```

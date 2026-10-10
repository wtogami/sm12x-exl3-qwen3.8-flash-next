# Rootless podman on Fedora 44: host prerequisites

Scope: running `build.sh` / `download.sh` / `start.sh` through podman's docker
shim as an unprivileged user. Reference host: `gg`, Fedora 44 Workstation,
kernel 7.2.9, podman 5.8.7 / crun 1.28, SELinux enforcing, cgroup v2, btrfs
`/home`, RPM Fusion NVIDIA driver 615.71.09
(`xorg-x11-drv-nvidia-cuda-libs`), RTX PRO 6000 Blackwell.

## What needs root

| Item | Root? |
| --- | --- |
| `./build.sh` image build | no |
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

Fedora 44 and RPM Fusion do not package `nvidia-container-toolkit`;
`dnf list available 'nvidia-container-toolkit*' 'libnvidia-container*'` returns
nothing with `fedora`, `updates`, and `rpmfusion-nonfree-nvidia-driver`
enabled. It ships only from NVIDIA's own repo or the `@ai-ml` COPR:

```sh
dnf copr enable @ai-ml/nvidia-container-toolkit
dnf install nvidia-container-toolkit nvidia-container-toolkit-selinux
```

Verified: COPR `nvidia-container-toolkit-1.20.1-1.fc44`. At 1.20 the
`libnvidia-container1` / `libnvidia-container-tools` packages are gone; there
is no `nvidia-container-cli` binary and nothing needs it, because CDI discovery
runs through NVML in-process.

Keep the driver itself from RPM Fusion. That package is correctly scoped: it
supplies `/usr/lib64/libcuda.so.1`, `libnvidia-ml.so.1`,
`libnvidia-ptxjitcompiler.so.1` and the rest of the set the spec mounts. Only
the toolkit comes from elsewhere; mixing an NVIDIA-repo driver userspace with
the RPM Fusion kernel module is what breaks.

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

`build.sh` performs both steps on a podman host through `qwen38_ensure_cdi` in
`platform-config.sh`, and re-running it is a no-op.

### When the spec must be regenerated

`build.sh` regenerates automatically when the spec is missing, when the version
reported by `nvidia-smi --query-gpu=driver_version` no longer appears in the
spec, or when the `nvidia.com/gpu=all` entity is absent. The manual list:

- **Any driver update — mandatory, and the common case.** The spec pins 103
  absolute host paths embedding the driver version:
  `/usr/lib64/libcuda.so.615.71.09`,
  `/usr/lib64/libnvidia-ptxjitcompiler.so.615.71.09`,
  `/lib/firmware/nvidia/615.71.09/gsp_ga10x.bin`, and so on. Once the package
  is upgraded those paths do not exist and the container fails during CDI setup
  before any process starts. Also regenerate when switching driver *source*
  (RPM Fusion to a `.run` installer), which moves the libraries.
- **GPU topology changes.** Adding, removing, or replacing a GPU changes the
  index and UUID entities; enabling or disabling MIG replaces them wholesale.
  The spec also carries the DRM nodes (`/dev/dri/card1`, `/dev/dri/renderD128`)
  and their GIDs, so a change of render node ordering counts.
- **Enabling a service the spec probes for.** Generation records what it found
  and warns about what it did not: `/nvidia-persistenced/socket`,
  `/nvidia-fabricmanager/socket` (NVSwitch), `/tmp/nvidia-cuda-mps`, `nvidia-imex`.
  Turning one on later, e.g. `systemctl enable --now nvidia-persistenced`,
  needs a regeneration to pick up its socket.
- **A podman or toolkit upgrade** that moves the supported `cdiVersion`
  (currently 0.7.0) or changes hook handling.

No regeneration needed for: rebuilding or re-pulling the image (the spec is
host-side state), a reboot (it is a plain file), or changing `QUANT`, `GPU`, or
any model argument.

When in doubt, force it:

```sh
rm ~/.config/cdi/nvidia.yaml && ./build.sh
```

## Rootless bind mounts: `download.sh` and `start.sh`

Mounting a host cache into a rootless container hits two unrelated faults that
produce the identical `PermissionError: [Errno 13] Permission denied`. Both are
fixed for `download.sh`; `start.sh` needs the same treatment on its two mounts.

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

## Checked and *not* a problem

- **`RLIMIT_MEMLOCK` = 8 MiB**, inherited into the container, looks alarming
  because `patches/qwen_host_embedding.py` allocates `pin_memory=True` buffers.
  Driver 615 does not charge pinned host memory against it: 16 MiB, 256 MiB and
  1 GiB pinned allocations all succeeded at the default limit. No `limits.d` or
  `DefaultLimitMEMLOCK` change is warranted.
- **cgroup v2**, `user.max_user_namespaces` = 514446, and `--ipc=host` all work
  rootless as shipped; no `--shm-size` workaround needed.
- **`# syntax=docker/dockerfile:1.7`** is ignored by `podman build`, and podman
  5.8 supports the one BuildKit-ism actually used, the `RUN` heredoc at step 6.
- **Image tagging**: podman reports `localhost/qwen38-rtx:local`; the prefix is
  implicit when the scripts name `qwen38-rtx:local`.
- **Model access**: the `exl3` checkpoint is public. `HF_TOKEN` is optional and
  only raises rate limits, though gated checkpoints on other profiles need it.

## Checklist for a fresh Fedora 44 rootless podman host

As root, once:

```sh
dnf install -y @rpmfusion-nonfree-nvidia-driver xorg-x11-drv-nvidia-cuda-libs   # or the distro's equivalent
dnf copr enable @ai-ml/nvidia-container-toolkit
dnf install -y nvidia-container-toolkit nvidia-container-toolkit-selinux        # also sets the SELinux boolean
systemctl enable --now nvidia-persistenced                                      # optional, removes first-request latency
```

As the deploying user:

```sh
./build.sh      # also generates ~/.config/cdi/nvidia.yaml and its containers.conf.d drop-in
./download.sh   # QUANT=exl3|exl3-ple8|nvfp4|nvfp4-redhatai
./start.sh
```

Verify the GPU reached the container before blaming the image:

```sh
docker run --rm --device nvidia.com/gpu=all --entrypoint nvidia-smi qwen38-rtx:local -L
# GPU 0: NVIDIA RTX PRO 6000 Blackwell Workstation Edition (UUID: GPU-4178...)
```

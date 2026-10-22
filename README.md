# Qwen3.8 Flash Next on RTX PRO 6000 and DGX Spark

A TP=1 SM12x serving recipe with host-resident token embeddings and
mmap-backed (checkpoint-mapped) PLE n-gram tables, FP8 KV cache, vision, CUDA
graphs, and tuned MTP. RTX defaults to a 524288-token context via YaRN 2.0
scaling of the 262144-native window (`LONGCTX=1`); Spark serves the native
262144. The scheduler has 16 request slots, and C1 performance is the
priority for the defaults.

**How fast is it?** (one card each, TP=1, FP8 KV, MTP on)

| Single-stream decode (C1) | RTX PRO 6000 @ 600 W | DGX Spark (GB10) |
|---|---:|---:|
| **Typical** (mixed content) | **159.5 tok/s** | **31.1 tok/s** |
| Best case | ≈260 tok/s | ≈42 tok/s |

Context depth, concurrency and power limits move these numbers — the full
measurement record is in [docs/performance-comparison.md](docs/performance-comparison.md).

> **Current: vLLM v0.32.0 rebase (2026-10-18); last full comparison day
> 2026-10-07 (v0.31 image, v0.32 re-qualified at parity).**
> The runtime builds on the released official `vllm/vllm-openai:v0.32.0`
> digest (CUDA 13.0, multiarch); the model ships upstream as
> `vllm/models/qwen4_exp` (Qwen4Exp is the upstream codename for
> Qwen3.8-Flash-Next), so no custom vLLM base is needed. FP8 QSA KV (PR
> 55557) shipped upstream in v0.31.0; the checkpoint-mapped PLE backport is
> re-anchored at v0.32 as `patches/ple-mmap-pr58439-58835-v0.32.patch`
> (upstream PRs 58439+58835, still open, head `047fc1c`), and
> `patches/port-mrope-served-window.py` carries the fix for v0.32.0's
> M-RoPE cache shrink (PR 59853), which crashed any prompt past the
> checkpoint-native 262144 (upstream issue
> [#62383](https://github.com/vllm-project/vllm/issues/62383), fix filed by
> us as PR [#62409](https://github.com/vllm-project/vllm/pull/62409); it
> floors the cos/sin cache on `max_model_len` and supersedes the earlier
> blunt x4 workaround) — details in docs/qualification.md. RTX defaults: `exl3`, MTP3, YaRN
> 512K, FP8 KV, host-offloaded PLE table (`PLE_MMAP=0`; discrete RTX
> cannot dereference checkpoint mappings — Spark (GB10) defaults to mmap).
>
> **Current measurements** — the comparison docs were measured on image
> `8294c3c914c0` at 600 W (2026-10-07); same-day A/B confirmed v0.32.0
> parity (2026-10-18), and image `abbde425a717` carries the full
> re-qualification including the #62409 boundary legs (2026-10-19) —
> [docs/quality-comparison.md](docs/quality-comparison.md) (EXL3 vs NVIDIA
> NVFP4 vs the exl3-ple8 FP8-PLE variant; paired
> McNemar stats, 10-run tool-eval study) ·
> [docs/performance-comparison.md](docs/performance-comparison.md) (power
> ladder, SM clock-lock sweep, cross-checkpoint concurrency scaling) ·
> [docs/qualification.md](docs/qualification.md) (full dated record).
>
> **Historical:** era-specific measurements (older images and power
> regimes) are retained as provenance — never as current values —
> in [benchmarks/RESULTS.md](benchmarks/RESULTS.md) and
> [docs/tuning-history.md](docs/tuning-history.md).
> See [PROVENANCE.md](PROVENANCE.md).

| Profile (`QUANT`) | Checkpoint | PLE table format | Default MTP (RTX / Spark) |
|---|---|---|---:|
| `exl3` | [EXL3 K4.25](https://huggingface.co/wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-v1) | BF16, about 95 GiB | 3 / 2 |
| `exl3-ple8` | [EXL3 K4.25 PLE FP8](https://huggingface.co/wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-PLE-FP8-v1) | FP8 with shared scale, about 48 GiB | 3 / 2 |
| `nvfp4` | [NVIDIA NVFP4](https://huggingface.co/nvidia/Qwen3.8-Flash-Next-NVFP4) | FP8 with shared scale, about 48 GiB | 2 / 2 |

## Host RAM and checkpoint storage

On RTX the PLE table is page-locked in host RAM, so its size is a host
memory requirement, not a GPU one. Per profile, from measured boots:

| Profile | Checkpoint on disk | PLE table | Pinned (host RAM committed) |
|---|---:|---:|---:|
| `exl3` | ~168 GiB | BF16, ~95.4 GiB | **97.4 GiB** |
| `exl3-ple8` | ~120 GiB | FP8, ~47.7 GiB | **49.7 GiB** |
| `nvfp4` | ~124 GiB | FP8, ~47.7 GiB | **49.7 GiB** |

The pinned column includes the host token embeddings; each MTP draft token
adds roughly another 1.184 GiB. These are table sizes, not whole-server
memory requirements — leave additional headroom for model loading, the
server, and the OS. `start.sh` sizes the requirement from the checkpoint
and refuses before creating the container; `QWEN38_SKIP_MEMCHECK=1`
overrides. Spark in mmap mode locks nothing — its table is read through
the GB10's unified memory. See
[host pinned memory](docs/podman-rootless-fedora.md#host-pinned-memory-for-the-ple-table),
and the next section for hosts too small to pin at all.

## Streaming the PLE table on small RAM hosts (`PLE_STREAM`)

`PLE_STREAM=1` takes the table out of pinned host RAM entirely: rows are read
from the checkpoint through the page cache, copied by CPU into a staging arena
of a few MiB, and gathered on the device. `Shmem` settles near 2.2 GiB instead
of 49.7, so a host smaller than the table can serve it. Greedy output is
byte-identical to pinned; the cost is in rates, not results. `PLE_STREAM=1`
is the only knob: on a pinned-capable host you leave it unset; a `piecewise`
diagnostic value exists for experiments and is documented in
[docs/ssd-stream-findings.md](docs/ssd-stream-findings.md).

Measured on `gg` (FP8 checkpoint, one RTX card, cold cache per boot; all
arms in one job under one recorded 300 W power envelope; strict hash
`7c1421daade83d79` identical on every arm):

| Host RAM | Mode | prefill tok/s | decode tok/s | vs pinned |
|---|---|---:|---:|---:|
| ≥ ~50 GiB | pinned table (`exact`) | 11,347.3 | 139.0 | — |
| any | `PLE_STREAM=1`, uncapped | 10,963.3 | 134.7 | −3.4% / −3.1% |
| 32 GiB | `PLE_STREAM=1` | 11,084.4 | 135.3 | −2.3% / −2.7% |
| 16 GiB | `PLE_STREAM=1` | 11,073.1 | 135.1 | −2.4% / −2.8% |

The steady-state cost is about 3% on both rates at matched power. A RAM cap
is invisible in serving — refaulting is a boot-time effect — but changes
boot time (~171 s uncapped vs ~271–281 s capped). So 16 GiB total RAM is the
working minimum for streaming and 32 GiB comfortable: sizing buys boot
margin, not throughput. Below ~8 GiB the engine's fixed ~7 GiB anonymous
footprint leaves no usable cache and the start OOMs rather than slowing.
Full record, drivers and the knob's graph-regime details:
[`docs/ssd-stream-findings.md`](docs/ssd-stream-findings.md).

## EXL3 expert quantization and the `exl3-ple8` variant

EXL3 requires independent K4/K5 allocation for each expert's gate, up and down
projection. The pinned B12x fork supports this geometry and preserves all 5951
experts with unequal projection tiers across target and MTP layers. The
`exl3-ple8` profile keeps those expert weights and uses an FP8 materialized
PLE table. The shipping default is **`exl3`** with the platform PLE storage
mode (RTX: host-offloaded resident table; Spark: mmap). Choose `exl3-ple8` to
save about 47.7 GiB of checkpoint payload, reduce download/storage
requirements, or fit more PLE rows in the available file cache on a lower-RAM
system. The 2026-10-07 cross-checkpoint comparison
(`docs/quality-comparison.md`)
found the two statistically tied on GSM8K, IFEval and tool-calling: the
quantized expert weights carry the model's quality and the n-gram PLE table
tolerates FP8, so the half-size table is a memory win, not a quality tax
(its only measurable cost: one off-by-one orchid repetition miss, 4/5 vs
5/5).

The full historical matrices — settings, contract failures, tool points and
ranges for every profile, including this variant's mmap qualification and
quality checks — are in [benchmarks/RESULTS.md](benchmarks/RESULTS.md); tuning
sweeps and the kernel-choice measurement record are in
[docs/tuning-history.md](docs/tuning-history.md). Both are provenance from
earlier images and power regimes, not current measurements.

## Platforms

`bash build.sh` and `bash start.sh` detect the host architecture automatically:
arm64 selects DGX Spark SM121 settings; x86_64 selects RTX SM120 settings.
Both default to the original EXL3 K4.25 checkpoint with BF16 PLE; PLE storage
mode is platform-specific (table below).
Spark caps GPU memory utilization at **0.7** to leave unified memory available
for checkpoint pages and the host. A lower value is configurable.

| Recipe default | RTX SM120 | Spark SM121 |
|---|---|---|
| Native architecture | x86_64 | arm64 |
| GPU memory utilization | 0.94 | 0.7 maximum |
| EXL3 MTP draft tokens | 3 | 2 |
| Vocabulary projection | Native | B12x |
| PLE storage | host-offloaded resident table | BF16 mmap checkpoint mapping |
| Maximum context / request slots | 524288 (YaRN, default) or 262144 native / 16 | 262144 / 16 |

Use `B12X_VOCAB=0` for the native vocabulary projection or
`B12X_VOCAB=1` to opt the RTX build in. `PLE_MMAP` overrides the platform
default on either host (`--engram-config` on the command line overrides the
launcher entirely).

Qualification records for both platforms and all profiles:
[docs/qualification.md](docs/qualification.md). Runtime images are not
published and none are planned: build from source (next section).

## Run

A GPU container runtime is required: rootless **podman with the NVIDIA CDI
hook** is the reference path and fully documented
([docs/podman-rootless-fedora.md](docs/podman-rootless-fedora.md));
**docker** is supported via the docker branch of the scripts. Build and run
from the repository root;
the scripts select the native RTX or Spark defaults automatically:

```bash
bash build.sh
bash download.sh
bash start.sh
curl http://127.0.0.1:8001/health
```

There is no published image: **every deployment builds from source.**
`build.sh` is one command (~20–45 min cold, minutes on a warm cache) and is
the only way to get a runtime matching this repo's HEAD. For an offline
host, build anywhere and transfer the result peer-to-peer, e.g.
`podman save localhost/qwen38-rtx:local | zstd > qwen38.tar.zst` (load with
`zstd -d | podman load`), or `skopeo copy docker-archive:...`.

Current RTX test host: two RTX PRO 6000 Blackwell 96 GB cards (4x8-pin),
driver 615.78.08, on a Threadripper 9970X with ~996 GiB RAM. Spark test
host: a GB10 with ~121.63 GiB unified memory, driver 580.159.03. Checkpoint
weights are downloaded separately (`download.sh`).

```bash
# Stop before switching models to release the host tables.
bash stop.sh
QUANT=exl3-ple8 bash download.sh
QUANT=exl3-ple8 bash start.sh
# NVIDIA: use QUANT=nvfp4 (download.sh, start.sh and stop.sh alike).
# GPU=1 selects another GPU on a multi-GPU RTX host.
# TP_SIZE=2 runs one engine tensor-parallel across two GPUs; they must all be
# injected -- GPU=all or GPU=0,1 (unset GPU also works: docker defaults to the
# first TP_SIZE devices, podman to all). TP is capped by the 2 KV heads: TP=2
# puts one KV head on each GPU and is the largest slice without replication.
# TP_SIZE>1 also implies --enable-expert-parallel: the K4.25 EXL3 checkpoint
# cannot split the 640-wide MoE intermediate dimension at all (block-128
# Hadamard alignment), so routed experts split by count instead. The hard
# requirement is EXL3-specific: nvfp4 also runs with EP off at measured
# parity (2026-10-18/19 receipts), but EP stays the default on every profile.
```

Stopping and restarting is a two-step, and `QUANT` selects the container name in
`stop.sh` exactly as it does in `start.sh` (`qwen38-$QUANT`): stopping the wrong
profile reports the names that do exist rather than calling `stop` on nothing.
An idle engine takes about 14 s to shut down, just over the 10 s runtime default,
so both scripts pass a 90 s grace period (`STOP_TIMEOUT`) -- without it every stop
is escalated to `SIGKILL`. `start.sh` automatically removes a *stopped* container
left behind by a previous run, because that is the normal `stop.sh` then
`start.sh` sequence and the engine otherwise rejects the name. It refuses to do
this to a *running* one; `REPLACE=1` makes the handoff explicit. That is
deliberately not `--replace`, which would cut off in-flight requests silently and
would stop the container using the default 10 s grace.

```bash
bash stop.sh                                # stops qwen38-exl3
QUANT=exl3-ple8 bash stop.sh                # stops qwen38-exl3-ple8
QUANT=exl3-ple8 REPLACE=1 bash start.sh     # stop-and-replace in one step
```

The OpenAI-compatible endpoint is `http://127.0.0.1:8001/v1`, with served
aliases `qwen38-exl3`, `qwen38-exl3-ple8` and `qwen38-nvfp4`. Tool calls use
`qwen3_coder`; reasoning uses `qwen3`. Send
`"chat_template_kwargs":{"enable_thinking":false}` for the non-thinking mode
used in performance tests. The full tool-quality suite uses thinking enabled.

`GPU`, `PORT`, `CONTAINER_NAME`, `HF_CACHE`, `RUNTIME_CACHE`, `CPU_THREADS`,
`MAX_MODEL_LEN`, `MAX_NUM_SEQS`, `MAX_BATCHED_TOKENS`,
`GPU_MEMORY_UTILIZATION`, `PLE_MMAP`, `MTP_TOKENS` and `ENABLE_SCALE_OUT` can
override the defaults. `MTP_TOKENS=0` disables speculation. `ENABLE_SCALE_OUT`
matters only on RTX, where the launcher adds `--enable-scale-out` by default so
`/v1/chat/completions/render` answers instead of 403 (see the coding-leg note
under **Reproduce qualification**); set it to `0` on a box whose port is
reachable beyond the local network, since the flag also publishes
`/inference/v1/abort_requests` and the launcher sets no `--api-key`. Spark
ignores it — the pinned vLLM there has no such argument and renders ungated. Runtime/compiler caches persist
under `~/.cache/qwen38-rtx/<profile>`; checkpoint caches are mounted
read-only. The launcher uses the native multiprocessing executor and passes
the PLE storage mode through `--engram-config`.

Sixteen scheduler slots do **not** mean sixteen simultaneous full-context
requests (262144 native, 524288 with the YaRN default) fit in the KV pool.
The pool is profiled at startup and can differ between hosts even with the
same memory-utilization setting — typically a few multiples of the
configured context — and actual scheduling depends on request mix. Every
runtime receipt records the pool it measured; qualification runs include
both sixteen overlapping short-context streams and an exact full-context
boundary test.

## Mmap PLE

`PLE_MMAP=1` selects the checkpoint-mapped PLE table
(`--engram-config '{"cpu_offload": true, "checkpoint_mapped": true}'`).
The lookup kernel reads rows in place from read-only mappings of the
checkpoint's safetensors shards; a model-runner input-prep pass issues
readahead for the rows a step needs. There is no table-sized device or pinned
allocation. This is the upstream redesign from PRs
[#58439](https://github.com/vllm-project/vllm/pull/58439) and
[#58835](https://github.com/vllm-project/vllm/pull/58835) (stacked, at head
`047fc1c`), re-derived for the v0.32.0 base as
`ple-mmap-pr58439-58835-v0.32.patch` with strict base hashes.

**Platform support.** Direct checkpoint mapping requires a GPU that
dereferences pageable host memory through the host page tables
(`CU_DEVICE_ATTRIBUTE_PAGEABLE_MEMORY_ACCESS_USES_HOST_PAGE_TABLES`), which
the CUDA driver checks at startup. DGX Spark (GB10, unified memory) passes and
defaults to mmap. A discrete RTX PRO 6000 reports the attribute as 0 (measured
on driver 615.71.09), so the RTX default is `PLE_MMAP=0`: the upstream
host-offloaded resident table (`cpu_offload`). Setting `PLE_MMAP=1` on unsupported
hardware fails fast with the driver-reported attribute. Token embeddings
remain in host RAM in either mode. Mapped tables cover the checkpoint's
actual dtype: BF16 for `exl3`, FP8 E4M3 with its scalar scale for
`exl3-ple8` and `nvfp4`. The former per-knob `PLE_MMAP_*` environment
variables are gone; the prefetch/readahead tunables are internal upstream
defaults, and a custom `--engram-config` passed to `start.sh` overrides the
launcher's one entirely.

```bash
GPU=0 bash start.sh  # RTX default: exl3, host-offloaded BF16 PLE table
QUANT=exl3-ple8 GPU=0 bash start.sh  # FP8 PLE, smaller checkpoint/cache footprint
# Run one model at a time; use stop.sh with the same QUANT before switching.
# PLE_MMAP=1/0 overrides the platform default; Spark defaults to mmap.
```

The upstream test suite `tests/test_ple_pageable.py` (594 checks at the PR
head) is vendored and runs on GPU-equipped hosts. Mapped pages accumulate in
the OS file cache but remain clean and reclaimable — mmap is not a memory
leak and not a fixed memory limit. Benchmarks use the existing page cache
with explicit warmups, so benchmark numbers never establish cold-disk
throughput.

## MTP defaults

Draft tokens default to **3** for the EXL3 profiles on RTX and **2** for
`nvfp4` and on Spark — the best measured mixed-C1 blend per profile under
the C1-priority defaults. Adaptive draft policies (long draft at low
concurrency, one token at high) were swept and rejected. `MTP_TOKENS=0`
disables speculation; `MTP_TOKENS=1` trades C1 latency for higher C16
aggregate. The full sweep tables, the adaptive-policy reproducer, the
code-heavy workload-specific options, and Spark's tuning trail are in
[docs/tuning-history.md](docs/tuning-history.md); receipts and exceptions
in [docs/qualification.md](docs/qualification.md).

## Kernel choices

- **EXL3 mixed MoE:** B12x, including the Qwen H2560/I640 projection planner
  fix from the [fork](https://github.com/tpurtell/sparkinfer-glmrt/commit/c76a40ee684cb3ef7d2c223d56a9b9cff25a3a1e).
- **QSA attention:** native upstream Triton QSA with FP8 main-KV-cache
  support (PR 55557, shipped in v0.31.0). The FP8 cache is dequantized
  inside the kernel using the layer's host-side scales; the indexer side
  cache and GDN state are unchanged.
- **NVIDIA MoE:** native FlashInfer CUTLASS. A tested B12x bridge is
  available behind `B12X_NVFP4=1`, off by default.
- **Vocabulary projection:** native on RTX, B12x on Spark; `B12X_VOCAB`
  overrides the platform default.
- **HC and GDN:** native on both platforms.

The measured comparisons behind each choice:
[docs/tuning-history.md](docs/tuning-history.md).

The [Dockerfile](Dockerfile) pins the base image and B12x revision. The
[patches](patches) now carry the EXL3 namespace/MTP-mapping loader port, the
qflashrt FP8-PLE annotation for EXL3 hybrids, exact host token embeddings, the
optional B12x vocabulary projection and NVFP4 expert bridges, the M-RoPE
served-window cache floor (the fix we filed upstream as PR 62409), and the
strict-hash checkpoint-mapped PLE backport (upstream PRs 58439+58835
re-derived for v0.32.0). The QSA FP8 main-cache backport shipped upstream in
v0.31.0 and was dropped, as were the former MTP remap, FP8 draft weights,
CompressedTensors PLE selection, ModelOpt PLE selection and structured-output
ports (upstream code since v0.30/v0.31/v0.32).
[Provenance](PROVENANCE.md) distinguishes borrowed benchmark contracts from new
integration work. Model licenses apply separately from the recipe's [license](LICENSE).

## Reproduce qualification

The full matrix in one command — boots each profile via `start.sh` at the
chosen topology (`TP_SIZE=2` for tensor-parallel), runs every functional leg
including the past-native 280K/480K boundary probes, captures `runtime.json`
/`memory.json` evidence per profile, records per-step exit codes, and is
resumable:

```bash
RESULT_DIR=benchmarks/my-qual GPU=1 bash scripts/qualification-legs.sh
# native-window hosts (Spark): add QUANTS=exl3 BOUNDARY_TOKENS=
```

The individual suites below run against an already-running idle endpoint and
refuse to overwrite an existing result directory.

```bash
# With the exl3-ple8 server already running:
QUANT=exl3-ple8 MTP_TOKENS=3 RESULT_DIR=benchmarks/my-mmap \
  bash scripts/benchmark-suite.sh
# Spark default: QUANT=exl3 MTP_TOKENS=2, with a new result directory.
# NVIDIA: QUANT=nvfp4 MTP_TOKENS=2, with its endpoint and a new result directory.

# Install tool-eval-bench at the recorded revision, using its uv environment.
# TOOL_EVAL_DIR points to that checkout; results also persist in its SQLite DB.
TOOL_EVAL_DIR=/path/to/tool-eval-bench RESULT_DIR=benchmarks/my-mmap \
  MODEL=qwen38-exl3-ple8 bash scripts/tool-quality.sh
```

Every suite leg also writes `runtime.json` into `RESULT_DIR`: the image, the
checkpoint it was pointed at, and the launch facts a client receipt cannot know
-- which PLE table path (`host-offload` or `checkpoint-mapped`), the server's own
MTP draft length, and the batch budgets. `SKIP_RUNTIME_CAPTURE=1` omits it.

Only `benchmark-suite.sh` runs the coding leg (`PART=coding`);
`qualification-legs.sh` has no such step, so a qualification pass cannot notice a
broken server-side renderer. That is how the coding leg survived the move to
upstream vLLM untouched: the route it renders through,
`/v1/chat/completions/render`, is a scale-out endpoint there, and it answers
`403 Scale-out endpoints are disabled` unless the server was launched with
`--enable-scale-out`. The RTX launcher adds that flag by default
(`ENABLE_SCALE_OUT=1`; `test-content-vllm.py` uses the same route), Spark's
pinned vLLM neither has the flag nor needs it, and both branches are asserted by
`scripts/test-mmap-launcher.py`.

Tool-eval-bench is pinned at `cf54b4bfe705f12f71e8866f10730572497c8105`.
The complete reports preserve failed and partial cases. Vision and retrieval
are targeted checks, not broad capability benchmarks. Exact orchid repetition
is unreliable, and throughput figures include outputs that fail contracts.


## Historical results

Era-specific measurements (older images, power settings and hosts) are
provenance, not current documentation. They live in
[benchmarks/RESULTS.md](benchmarks/RESULTS.md) (generated from the archived
receipt directories by `scripts/summarize-results.py`),
[docs/tuning-history.md](docs/tuning-history.md) (MTP sweeps, kernel-choice
comparisons, release-era notes) and [docs/qualification.md](docs/qualification.md)
(the dated qualification ledger). Current measurements:
[docs/performance-comparison.md](docs/performance-comparison.md) and
[docs/quality-comparison.md](docs/quality-comparison.md).

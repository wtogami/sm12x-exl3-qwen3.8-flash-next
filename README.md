# Qwen3.8 Flash Next on RTX PRO 6000 and DGX Spark

A TP=1 SM12x serving recipe with host-resident token embeddings and
mmap-backed (checkpoint-mapped) PLE n-gram tables, FP8 KV cache, vision, CUDA
graphs, and tuned MTP. RTX defaults to a 524288-token context via YaRN 2.0
scaling of the 262144-native window (`LONGCTX=1`); Spark serves the native
262144. The scheduler has 16 request slots. C1 performance is the priority
for the defaults; C16 throughput tradeoffs are recorded below.

> **Current: vLLM v0.31.0 rebase, last full measurement day 2026-10-07.**
> The runtime builds on the released official `vllm/vllm-openai:v0.31.0`
> digest (CUDA 13.0, multiarch); the model ships upstream as
> `vllm/models/qwen4_exp` (Qwen4Exp is the upstream codename for
> Qwen3.8-Flash-Next), so no custom vLLM base is needed. FP8 QSA KV (PR
> 55557) shipped upstream in v0.31.0 and the vendored copy was dropped; the
> checkpoint-mapped PLE backport is re-derived at v0.31 as
> `patches/ple-mmap-pr58439-58835-v0.31.patch` (upstream PRs 58439+58835
> plus a pinned post-cut correction). RTX defaults: `exl3`, MTP3, YaRN
> 512K, FP8 KV, host-offloaded PLE table (`PLE_MMAP=0`; discrete RTX
> cannot dereference checkpoint mappings — Spark (GB10) defaults to mmap).
>
> **Current measurements** — image `8294c3c914c0`, 600 W, all four
> checkpoints measured 2026-10-07:
> [docs/quality-comparison.md](docs/quality-comparison.md) (EXL3 vs NVIDIA
> NVFP4 vs RedHatAI NVFP4 vs the exl3-ple8 FP8-PLE variant; paired
> McNemar stats, 10-run tool-eval study) ·
> [docs/performance-comparison.md](docs/performance-comparison.md) (power
> ladder, SM clock-lock sweep, four-way concurrency scaling) ·
> [docs/qualification.md](docs/qualification.md) (full dated record).
>
> **Historical:** the benchmark tables further down in this file (and the
> full set in [benchmarks/RESULTS.md](benchmarks/RESULTS.md)) predate the
> rebase (v0.1.0–v0.3.1-era images, 400 W RTX, 262144
> context) and are retained as provenance, not as a current measurement.
> See [PROVENANCE.md](PROVENANCE.md).

| Profile (`QUANT`) | Checkpoint | PLE table format | Default MTP (RTX / Spark) |
|---|---|---|---:|
| `exl3` | [EXL3 K4.25](https://huggingface.co/wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-v1) | BF16, about 95 GiB | 3 / 2 |
| `exl3-ple8` | [EXL3 K4.25 PLE FP8](https://huggingface.co/wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-PLE-FP8-v1) | FP8 with shared scale, about 48 GiB | 3 / 2 |
| `nvfp4` | [NVIDIA NVFP4](https://huggingface.co/nvidia/Qwen3.8-Flash-Next-NVFP4) | FP8 with shared scale, about 48 GiB | 2 / 2 |
| `nvfp4-redhatai` | [RedHatAI NVFP4](https://huggingface.co/RedHatAI/Qwen3.8-Flash-Next-NVFP4) | BF16, about 95 GiB | 3 / 2 |

EXL3 requires independent K4/K5 allocation for each expert's gate, up and down
projection. The pinned B12x fork supports this geometry and preserves all 5951
experts with unequal projection tiers across target and MTP layers. The
`exl3-ple8` profile keeps those expert weights and uses an FP8 materialized
PLE table. The shipping default is **`exl3`** with the platform PLE storage
mode (RTX: host-offloaded resident table; Spark: mmap). Choose `exl3-ple8` to
save about 47.7 GiB of checkpoint payload, reduce download/storage
requirements, or fit more PLE rows in the available file cache on a lower-RAM
system. The 2026-10-07 four-way comparison (`docs/quality-comparison.md`)
found the two statistically tied on GSM8K, IFEval and tool-calling: the
quantized expert weights carry the model's quality and the n-gram PLE table
tolerates FP8, so the half-size table is a memory win, not a quality tax
(its only measurable cost: one off-by-one orchid repetition miss, 4/5 vs
5/5).

The measured columns in this section, the § MTP tuning table, and the full
set in benchmarks/RESULTS.md are the historical record from the v0.1.0–v0.3.1
era (400 W RTX): the `exl3-ple8` profile's full benchmark matrix with mmap
enabled from v0.2.0, including its quality checks. The original
EXL3/NVIDIA columns are unchanged historical results, not new runs; they
differ in quantization, PLE precision and runtime, so they are not a
controlled mmap-on/off comparison.

Selected-profile results (historical, pre-rebase v0.3.1-era images): RTX
columns use one 400 W card; Spark uses one GB10.

| Measurement | EXL3 resident, MTP3 (v0.1.0) | NVFP4 resident, MTP2 (v0.1.0) | EXL3 PLE8 mmap, MTP3 | EXL3 mmap Spark (BF16 PLE), MTP2 |
|---|---:|---:|---:|---:|
| C1 seven-workload weighted decode, tokens/s | **152.82** | **151.11** | **147.78** | **31.11** |
| C1 greedy `merge_intervals` median, tokens/s | 202.99 | 185.55 | 197.74 | 38.12 |
| C1 sampled async coding task median, tokens/s | 185.55 | 166.92 | 183.45 | 36.21 |
| C16 sampled-prose aggregate median, tokens/s | 696.96 | 930.71 | 659.75 | 101.00 |
| Full-context boundary: 261888 input + 256 output | Pass | Pass | Pass | Pass |
| API tool constraints / retrieval probes | 16/16; 6/6 | 16/16; 6/6 | 16/16; 6/6 | 16/16; 6/6 |

These are different workloads, not interchangeable rates. Full matrices with
settings, contract failures, tool points and measurement ranges:
[detailed report](benchmarks/RESULTS.md). Tool evaluation there uses C8
(eight concurrent cases); its points are not a normalized comparison score.

## Platforms

`bash build.sh` and `bash start.sh` detect the host architecture automatically:
arm64 selects DGX Spark SM121 settings; x86_64 selects RTX SM120 settings.
Both default to the original EXL3 K4.25 checkpoint with BF16 PLE and mmap.
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

Spark's original BF16 PLE default completed full qualification; its column
appears in [benchmarks/RESULTS.md](benchmarks/RESULTS.md).
The RTX columns retain their existing measurements.
[Release v0.3.1](https://github.com/tpurtell/sm12x-exl3-qwen3.8-flash-next/releases/tag/v0.3.1)
provides native images for both platforms, including the reviewed speculative
reasoning and xgrammar termination fixes, plus the resident-mode loading repair.
The v0.3.1 checks cover all 220 tests on RTX, the new resident regressions on
both architectures, and full-model resident/mmap checks on RTX. The earlier
v0.3.0 structured-output qualification remains linked in [benchmarks/RESULTS.md](benchmarks/RESULTS.md).
The [RTX shipping-default structured-output qualification](benchmarks/rtx-structured-final/qualification.json)
adds a C8 tool run without changing the historical RTX table columns.

## Run

Install Docker with NVIDIA GPU access. Build and run from the repository root;
the scripts select the native RTX or Spark defaults automatically:

```bash
bash build.sh
bash download.sh
bash start.sh
curl http://127.0.0.1:8001/health
```

To use the pinned image instead of building, replace `bash build.sh` with
`bash pull.sh`. It selects the native image and installs the same local tag
used by `start.sh`. Immutable image digests are recorded in
[platform-config.sh](platform-config.sh).

| Platform | Image package | Status |
|---|---|---|
| RTX, linux/amd64 | `ghcr.io/tpurtell/rtx6k-exl3-qwen3.8-flash-next` | v0.3.1 (predates the v0.30.0 and v0.31.0 rebases) |
| Spark, linux/arm64 | `ghcr.io/tpurtell/spark-exl3-qwen3.8-flash-next` | v0.3.1 (predates the v0.30.0 and v0.31.0 rebases) |

The published packages above predate both rebases; `pull.sh` installs
them until rebased images are published. **Build with `build.sh` to get the
rebased runtime.** The Spark package currently requires registry
authentication while its visibility is private. The package owner can enable
public pulls in GitHub package settings.

Current RTX test host: one RTX PRO 6000 Blackwell 96 GB at its **600 W**
default limit (4x8-pin power), driver 615.71.09, on a Threadripper 9970X with
183 GiB CPU RAM; 2026-10-07 measurements under the v0.31.0 image use this
configuration. The historical 400 W columns (above and in
benchmarks/RESULTS.md) were measured on the same class of card at a 400 W
limit. Spark uses a GB10 with approximately
121.63 GiB shared CPU/GPU memory and driver 580.159.03.
Each target/draft token embedding adds approximately 1.184 GiB of host storage
beyond the PLE table. Leave additional memory for model loading, the server
and the operating system; 95/48 GiB are table sizes, not whole-server memory
requirements. Checkpoint weights are downloaded separately.

```bash
# Stop before switching models to release the host tables.
bash stop.sh
QUANT=exl3-ple8 bash download.sh
QUANT=exl3-ple8 bash start.sh
# NVIDIA: use QUANT=nvfp4; RedHatAI: QUANT=nvfp4-redhatai
# (download.sh, start.sh and stop.sh alike).
# GPU=1 selects another GPU on a multi-GPU RTX host.
```

The OpenAI-compatible endpoint is `http://127.0.0.1:8001/v1`, with served
aliases `qwen38-exl3`, `qwen38-exl3-ple8`, `qwen38-nvfp4` and
`qwen38-nvfp4-redhatai`. Tool calls use
`qwen3_coder`; reasoning uses `qwen3`. Send
`"chat_template_kwargs":{"enable_thinking":false}` for the non-thinking mode
used in performance tests. The full tool-quality suite uses thinking enabled.

`GPU`, `PORT`, `CONTAINER_NAME`, `HF_CACHE`, `RUNTIME_CACHE`, `CPU_THREADS`,
`MAX_MODEL_LEN`, `MAX_NUM_SEQS`, `MAX_BATCHED_TOKENS`,
`GPU_MEMORY_UTILIZATION`, `PLE_MMAP` and `MTP_TOKENS` can override the
defaults. `MTP_TOKENS=0` disables speculation. Runtime/compiler caches persist
under `~/.cache/qwen38-rtx/<profile>`; checkpoint caches are mounted
read-only. The launcher uses the native multiprocessing executor and passes
the PLE storage mode through `--engram-config`.

Sixteen scheduler slots do **not** mean sixteen simultaneous full-context
requests (262144 native, 524288 with the YaRN default) fit in the KV pool. The final tests include sixteen overlapping
short-context client streams and a separate exact full-context boundary test.
The KV pool is profiled at startup and can differ between hosts even with the
same memory-utilization setting; runtime receipts retain the actual capacity.
Measured startup KV pools were 796612 tokens for resident EXL3, 686817 for
resident NVIDIA and 880600 for EXL3 PLE8 mmap (3.04×, 2.62× and 3.36× the
configured context); actual scheduling also depends on
request mix. Use the context and concurrency tables in [benchmarks/RESULTS.md](benchmarks/RESULTS.md) to distinguish those cases.

## Mmap PLE

`PLE_MMAP=1` selects the checkpoint-mapped PLE table
(`--engram-config '{"cpu_offload": true, "checkpoint_mapped": true}'`).
The lookup kernel reads rows in place from read-only mappings of the
checkpoint's safetensors shards; a model-runner input-prep pass issues
readahead for the rows a step needs. There is no table-sized device or pinned
allocation. This is the upstream redesign from PRs
[#58439](https://github.com/vllm-project/vllm/pull/58439) and
[#58835](https://github.com/vllm-project/vllm/pull/58835) (stacked, at head
`47b9933db82d`), re-derived for the v0.31.0 base as
`ple-mmap-pr58439-58835-v0.31.patch` with strict base hashes plus one pinned
post-branch-cut upstream correction.

**Platform support.** Direct checkpoint mapping requires a GPU that
dereferences pageable host memory through the host page tables
(`CU_DEVICE_ATTRIBUTE_PAGEABLE_MEMORY_ACCESS_USES_HOST_PAGE_TABLES`), which
the CUDA driver checks at startup. DGX Spark (GB10, unified memory) passes and
defaults to mmap. A discrete RTX PRO 6000 reports the attribute as 0 (measured
on driver 615.71.09), so the RTX default is `PLE_MMAP=0`: the upstream
host-offloaded resident table (`cpu_offload`, the same v0.1.0-era behavior as
the historical RTX benchmark rows). Setting `PLE_MMAP=1` on unsupported
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

The old PR #54129 gather-based backport and its B12x-era tuning knobs are
removed with the rebase; its review tests targeted code paths the upstream
redesign no longer has (streamed-scale comparison). The upstream test suite
`tests/test_ple_pageable.py` (594 checks at the PR head) is vendored for
GPU-equipped hosts. Mapped pages remain clean and reclaimable in the file
cache. The historical mmap snapshots in [benchmarks/RESULTS.md](benchmarks/RESULTS.md) remain valid observations of the
pre-rebase runtime.

Mapped pages may accumulate in Linux's file cache, but remain clean and
reclaimable. An initial snapshot showed about 2.3 GiB resident across the
47.68 GiB table mappings, with no anonymous or dirty PLE mapping pages. This
is not a fixed memory limit. Benchmarks use an ext4 filesystem on a Samsung
9100 PRO 4TB NVMe, the existing OS page cache and explicit warmups; they do
not establish cold-disk throughput or a minimum host-RAM requirement.

## MTP tuning and C16 tradeoffs

These RTX v0.1.0 resident-mode development comparisons use the same seven C1 workloads (three measured
responses each). C8/C16 are shorter probes: 128 forced prose tokens, one measured
batch after warmup. They are distinct from the final 256-token, three-run client
matrix in [benchmarks/RESULTS.md](benchmarks/RESULTS.md). Small differences between runs are not a statistical proof of
superiority; defaults use the best observed mixed C1 result.

| Quant | Draft policy | C1 weighted blend, tokens/s | C8 aggregate | C16 aggregate |
|---|---|---:|---:|---:|
| EXL3 | Off | 92.76 | 465.99 | 762.88 |
| EXL3 | 1 | 131.54 | 561.14 | 861.92 |
| EXL3 | 2 | 149.16 | 532.52 | 718.39 |
| EXL3 | **3, default** | **152.97** | — | — |
| EXL3 | 4 | 150.63 | 408.90 | 486.12 |
| EXL3 | Adaptive 3 → 1 | 150.71 | 516.51 | 792.07 |
| NVFP4 | Off | 96.38 | 500.96 | 855.50 |
| NVFP4 | 1 | 130.35 | 597.43 | 915.16 |
| NVFP4 | **2, default** | **152.40** | 552.95 | 881.49 |
| NVFP4 | 3 | 149.46 | 549.68 | 544.11 |
| NVFP4 | Adaptive 2 → 1 | 146.04 | 585.89 | 858.97 |

Adaptive policies use the longer draft for 1–4 scheduled requests and one draft
token for 5–16. Neither improved the observed C1 blend. MTP1 is a measured option
for higher C16 throughput at a substantial C1 cost. The MTP4 EXL3 and MTP3 NVIDIA
C16 probes reached only 14 and 13 overlapping streams, respectively.

To reproduce the EXL3 adaptive comparison, suppress the automatic static
configuration and supply the measured scheduler policy explicitly:

```bash
QUANT=exl3 PLE_MMAP=0 MTP_TOKENS=0 bash start.sh --speculative-config \
  '{"method":"mtp","num_speculative_tokens":3,"num_speculative_tokens_per_batch_size":[[1,4,3],[5,16,1]]}'
```

For code-heavy C1 traffic, EXL3 MTP4 reached 217.18 tokens/s on the greedy
`merge_intervals` workload, versus 202.99 in the qualified MTP3 run. NVIDIA
MTP3 reached 202.67 versus 182.82 with MTP2. These are workload-specific options:
set `MTP_TOKENS=4` or `3` respectively. They are not the separate sampled async
coding task reported in [benchmarks/RESULTS.md](benchmarks/RESULTS.md). Full tuning receipts and exceptions are in the
[qualification ledger](docs/qualification.md) and [raw benchmarks](benchmarks).

Spark's BF16 mmap comparisons favor **MTP2** for the mixed C1 workload.
The initial readahead-off sweep measured 26.29, 27.32, 25.72 and 24.14 tokens/s
for MTP1 through MTP4. Targeted readahead raised the MTP2 blend to 29.21;
adding B12x vocabulary on the same host raised it to 30.75. The combined
MTP2 / readahead2048 / B12x vocabulary profile measures **31.11 tokens/s**
in the completed qualification.
[All Spark tuning receipts](benchmarks/spark-review/TUNING.md) retain the
C16 probes and same-host comparisons. The 128-range and 2048-range short
C16 probes measured 89.09 and 97.14 tokens/s; these single probes are distinct
from the final client matrix.

## Kernel choices

- **EXL3 mixed MoE:** B12x, including the Qwen H2560/I640 projection planner fix
  pushed to the [fork](https://github.com/tpurtell/sparkinfer-glmrt/commit/c76a40ee684cb3ef7d2c223d56a9b9cff25a3a1e).
- **QSA attention:** native upstream Triton QSA with FP8 main-KV-cache
  support (PR 55557), shipped in v0.31.0 itself — the v0.30-era vendored
  copy is gone. The FP8 cache is dequantized inside the QSA kernel with the
  layer's host-side scales; the indexer side caches and
  GDN state are unchanged. The former B12x QSA bridge is retired because v0.30.0
  rejects FP8 main caches and the merged upstream change supersedes the bridge;
  its pre-rebase measurements remain historical.
- **NVIDIA MoE:** native FlashInfer CUTLASS. Precise B12x won isolated component
  timings but lost the end-to-end C1 blend, 136.74 versus 152.40 tokens/s.
  `B12X_NVFP4=1` retains the tested optional bridge; it is off by default.
- **Vocabulary projection:** native on RTX; B12x on Spark. B12x did not improve
  the RTX mixed C1 blend, but improved the same-host Spark comparison from
  29.21 to 30.75 tokens/s. `B12X_VOCAB` overrides the platform default.
- **HC and GDN:** native on both platforms. Spark's native HC was faster at
  decode sizes; the optional B12x GDN path failed its B16/Q3 numerical check
  and showed no timing advantage in the earlier cases. RTX comparisons also
  retained native paths. Reproducers and failures remain in the repository.

The [Dockerfile](Dockerfile) pins the base image and B12x revision. The
[patches](patches) now carry the EXL3 namespace/MTP-mapping loader port, the
qflashrt FP8-PLE annotation for EXL3 hybrids, exact host token embeddings, the
optional B12x vocabulary projection and NVFP4 expert bridges, the
CompressedTensors PLE branch that lets RedHatAI's checkpoint start under
vLLM v0.31, and the strict-hash checkpoint-mapped PLE backport (upstream
PRs 58439+58835 re-derived for v0.31.0). The QSA FP8 main-cache backport
shipped upstream in v0.31.0 and was dropped, as were the former MTP remap,
FP8 draft weights, ModelOpt PLE selection and structured-output ports
(upstream code since v0.30/v0.31).
[Provenance](PROVENANCE.md) distinguishes borrowed benchmark contracts from new
integration work. Model licenses apply separately from the recipe's [license](LICENSE).

The v0.1.0 runtime receipts retain the original image IDs. The follow-on
v0.2.0 release adds mmap support and its reviewed validation fixes, with full
`exl3-ple8` mmap qualification on RTX; v0.3.0/v0.3.1 add native Spark support
and the resident-mode repair. The current branch completes two engine
rebases (v0.30.0, then released-v0.31.0) and carries the 2026-10-07 four-way
RTX comparison in `docs/` — rebased release images are not yet published.
Historical RTX evidence is retained.

## Reproduce qualification

Run against an otherwise idle endpoint. These scripts refuse to overwrite an
existing result directory.

```bash
# With the mmap-enabled exl3-ple8 server already running:
QUANT=exl3-ple8 MTP_TOKENS=3 RESULT_DIR=benchmarks/my-mmap \
  bash scripts/benchmark-suite.sh
# Spark default: QUANT=exl3 MTP_TOKENS=2, with a new result directory.
# NVIDIA: QUANT=nvfp4 MTP_TOKENS=2, with its endpoint and a new result directory.

# Install tool-eval-bench at the recorded revision, using its uv environment.
# TOOL_EVAL_DIR points to that checkout; results also persist in its SQLite DB.
TOOL_EVAL_DIR=/path/to/tool-eval-bench RESULT_DIR=benchmarks/my-mmap \
  MODEL=qwen38-exl3-ple8 bash scripts/tool-quality.sh
```

Tool-eval-bench is pinned at `cf54b4bfe705f12f71e8866f10730572497c8105`.
The complete reports preserve failed and partial cases. Vision and retrieval
are targeted checks, not broad capability benchmarks. Exact orchid repetition
is unreliable, and throughput figures include outputs that fail contracts.


## Historical measurement tables

The complete pre-rebase result tables (v0.1.0 EXL3/NVFP4 resident, v0.2.0
EXL3 PLE8 mmap, Spark BF16 mmap: seven content workloads, orchid repetition,
sampled prose, prefill matrix, context/decode scaling, the reference coding
task, tool checks and memory snapshots) live in
[benchmarks/RESULTS.md](benchmarks/RESULTS.md), generated from the raw
receipt directories by `scripts/summarize-results.py`. They were measured at
400 W, at 262144 context, on images predating the v0.30.0/v0.31.0 rebases —
provenance only. Current measurements:
[docs/performance-comparison.md](docs/performance-comparison.md) and
[docs/quality-comparison.md](docs/quality-comparison.md).

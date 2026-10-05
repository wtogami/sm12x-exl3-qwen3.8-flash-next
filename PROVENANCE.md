# Development provenance

The Dockerfile pins the Qwen vLLM base, released GLM EXL3 adapter source image,
and tpurtell/sparkinfer-glmrt commit. The EXL3 adapter preserves the source
implementation's per-projection K4/K5 weight preparation. Patches in this
recipe adapt namespaces, loading, MTP capacity and QSA execution.

`benchmark-decode.py`, `benchmark-prefill.py`, `test-content-vllm.py` and
`test-vision-vllm.py` originate from the local
`brandon-glm-5.3-flash/recipe/scripts` reference. The content runner retains
the seven semantic contracts and uses Qwen's `enable_thinking=False` template
instead of manually adding GLM's closing thinking marker. Benchmark schema
identifiers and the default model alias were adapted for Qwen. No GLM
performance results are copied into this recipe.

`code-agent-prompt.txt` preserves the async task-runner prompt from the
reference's `benchmark-dflash2-vllm.py`. `benchmark-code-agent-depth.py`
adapts its depth experiment to native Qwen non-thinking rendering and retains
all output tokens and client timings. It reports both the post-initial-burst
decode rate and the reference's N−1 rate; forced output length does not
establish generated-code correctness.

Kernel/bridge test receipts are not full-model benchmark results. Model loading
memory figures in the qualification ledger are startup observations, not
steady-state capacity guarantees. Recipe and borrowed vLLM/B12x code are
covered by their applicable source licenses; checkpoint weights retain their
own licenses and are downloaded separately.

The optional mmap PLE backport derives from vLLM PR #54129 at
`50a061f792f36364f5f95a93eee21f1e9d77f65e`. The vendored patch and upstream
regression tests retain Apache-2.0 attribution. The port preserves this
recipe's base model namespaces, resident PLE path and kernels, and corrects
scale validation defects demonstrated by independent failing regressions.
See `docs/mmap-review.md` for the exact scope, compatibility adaptations and
real-checkpoint/CUDA replay evidence. The full follow-on benchmark applies
only to `exl3-ple8` with mmap enabled; v0.1.0 results remain historical.

## Native Spark build and qualification

The recipe uses the same pinned multiarch vLLM base for native
linux/arm64 builds, selecting `sm_121a` for CuTe. The amd64 EXL3 source
stage supplies only its Python adapter. The per-projection expert allocation
and checkpoint revisions are unchanged. Spark defaults select MTP2 and
B12x vocabulary after component and serving comparisons; native HC, native
GDN and the existing mixed-expert tile policy are retained. The numerical
failure from the optional GDN comparison remains in the raw receipts.

Targeted mmap readahead defaults to a 2048-range limit on both platforms.
This is a new launcher/build default, not a change to historical RTX
measurements or to the environment baked into the old v0.2.0 image.
`benchmarks/spark-review/TUNING.md` links every completed C1 tuning run.

Full qualification is distributed by independent suite across four Sparks.
Each measured request or concurrent batch uses one TP=1 GB10. The assembly
script requires identical immutable image IDs, serving arguments and selected
environment settings across hosts, and records each result's originating host
and SHA-256. The completed qualification is in `benchmarks/spark-final`, including that
manifest, all four runtime captures and post-qualification memory snapshots.

## Structured-output corrections in v0.3.0

Both native images include the reasoning-end guard from vLLM commit
[c6e19b3be243](https://github.com/vllm-project/vllm/commit/c6e19b3be243)
and termination correction from [PR 52805](https://github.com/vllm-project/vllm/pull/52805).
They were reviewed against upstream and the adjacent GLM recipe at commit
`9c35641652670bd216a4ad29495c3edd41f0828c`, with strict Qwen source hashes.
[Review and negative controls](benchmarks/spark-structured-review/README.md)
record the port and regressions. Both builds run all fourteen regression tests.

Spark preserves every original numerical image layer. The RTX comparison hashes
all 5738 installed vLLM/B12x files: only the two structured-output Python files
differ from v0.2.0. Image defaults additionally enable readahead2048 and native
architecture settings. Existing performance receipts retain their original
image IDs and environments; these are not new RTX performance measurements.

## Resident PLE repair in v0.3.1

A full-model GPU1 reproduction with original EXL3 K4.25 BF16 PLE confirmed
that v0.3.0 resident mode fails at `ple_layer.py:691`. The mmap backport had
moved embedding access before the CPU-offload metadata-only worker branch.
The GPU worker deliberately has no table in this mode. The strict-hash repair
moves its metadata-only load ahead of embedding access, while preserving the
mmap reload guard before iterator consumption. This is a local compatibility
repair; numerical kernels, model revisions and defaults are unchanged.

The new BF16/FP8 metadata regressions fail against the old image and pass on
both native builds. All 220 tests pass on RTX. Full-model mode checks use the
locally available BF16 model on GPU1, with MTP3 and CUDA graphs. See the
[reproduction and qualification record](benchmarks/resident-v031-review/README.md).

## v0.31.0 rebase (this branch)

The runtime moves from `vllm/vllm-openai:v0.30.0` to the official
`vllm/vllm-openai:v0.31.0` digest
`sha256:a4a4c0437bf7240089da5f08aa370c4aee17ae5290f7a3b468825ee26c4c3a6b`
(multiarch, CUDA 13.0, torch 2.13.0+cu130).

Dropped as upstream code in v0.31.0: the QSA FP8 KV cache backport
(`qsa-fp8-pr55557-v0.30.patch`, its port and hashes) — the release ships
`fp8_e4m3` main-KV support on the QSA path natively, matching the vendored
patch line-for-line.

`ple-mmap-pr58439-58835-v0.31.patch` — the checkpoint-mapped PLE backport
(PR 58439 + stacked 58835, head `47b9933db82d`) re-derived against v0.31.0.
v0.31.0 carries the common/nvidia `ngram_embedding` split, so the shared-base
hunks now land in `qwen4_exp/common/ngram_embedding.py` and the CUDA-specific
pageable embedding, loader glue and `ple_pageable.py` in the nvidia module —
restoring the upstream PR's original file shape (the v0.30.0 patch had merged
them). Rebase adaptations: the nvidia module gains its own
`eager_break_during_capture` import (it moved to common);
`resolve_checkpoint_files` unpacks the release's 4-tuple `_prepare_weights`
return; `resolve_dp_shared_memory()` — reintroduced upstream with
`dp_shared_memory: bool | None` and `use_thp` — gains the
`not checkpoint_mapped` guard, and the ported
`tests/test_ple_pageable.py` now exercises both resolve paths. Upstream also
retired the `VLLM_PLE_CPU_OFFLOAD` default factory (plain
`cpu_offload: bool = True`); the launcher always passes an explicit
`--engram-config`, so behavior is unchanged.

Recipe-local ports (EXL3 registration/loader, host token embeddings, B12x
vocabulary projection, NVFP4 experts, structured-output AST regressions)
re-anchor on the v0.31.0 tree unchanged except that
`port-exl3-ple-fp8.py` now hooks `from_quant_config` in the common
`ngram_embedding.py` (moved from nvidia). The b12x pin (`c76a40ee`), the GLM
EXL3 source image pin (`48e254d9`), and the checkpoint pins are unchanged.

## v0.30.0 rebase

The runtime moves from the custom `vllm/vllm-openai:qwen38-flash-next` dev
build (upstream commit unknown, model at `vllm/models/qwen3_8_flash_next`) to
the immutable official `vllm/vllm-openai:v0.30.0` digest
`sha256:8a69ffad015f138d7170c4ddc429e230a3bc1c1719f67e14324749df200a4b90`
(CUDA 13.0.2, multiarch). The newest tag, v0.31.0rc1, has no published image.
Upstream merged the model as `vllm/models/qwen4_exp` (Qwen4Exp is the upstream
codename for Qwen3.8-Flash-Next) and the pinned checkpoints declare
`model_type: qwen4_exp`, so no custom base is needed. All performance tables
in the README predate this rebase.

Dropped as upstream code in v0.30.0: the XGrammar termination backport
(PR 52805), the speculative reasoning-end guard (c6e19b3be243), the delegating
tool-parser fix (ParserManager rewrite), the MTP `quantized_layers` remap and
`FP8_PB_WO` block-FP8 MoE redirect, and the ModelOpt mixed-precision FP8 PLE
selection. Their regression tests (`test-xgrammar-termination.py`,
`test-structured-output-reasoning.py`, `test-tool-constraints.py`) are kept
and now verify the upstream implementations inside the image.

Retargeted ports (exact-match anchors re-derived against the v0.30.0 tree):
EXL3 registration/loader (`deepseek_config` registry anchor, `qwen4_exp`
model types), host token embeddings, B12x vocabulary projection, optional
B12x NVFP4 experts (new `ModelOptMxFp8Config` section terminator), and the
EXL3 `qflashrt.fp8-ple.v1` PLE branch, which now hooks the upstream
`Qwen4ExpPLEEmbeddingMethod.from_quant_config` with suffix-based module
matching instead of the old fork's prefix rewrite.

Vendored upstream backports, applied with base-file sha256 pins and fuzz 0:

- `qsa-fp8-pr55557-v0.30.patch` — PR 55557 (merged main `dff1bde84dd6`, first
  released in v0.31.0): `fp8_e4m3` main KV cache on the QSA path. This replaces
  the B12x QSA bridge (`b12x_qsa_attention.py` and its port/tests are
  removed); the v0.30.0 owner otherwise rejects the recipe's FP8 KV default.
- `ple-mmap-pr58439-58835-v0.30.patch` — checkpoint-mapped PLE: PR 58439 plus
  its stacked cold-readahead follow-up 58835 (head `47b9933db82d`). v0.30.0
  predates the common/nvidia `ngram_embedding` split, so the shared-base hunks
   are merged into `nvidia/ngram_embedding.py` and the `EngramConfig`
   validation hunks are adapted to that release's config shape; `ple_pageable.py`
   and the model-state prefetch hook are transplanted unmodified. Recorded
   adaptations: `resolve_checkpoint_files` unpacks v0.30.0's 3-tuple
   `_prepare_weights` return (main returns 4); `verify_model_config` keeps the
   release's CUDA-alike gate plus the PR's CUDA-only mapped gate; upstream
   main's `resolve_dp_shared_memory`/`use_thp` auto-default is not ported
   (v0.30.0 has no callers) and the vendored test carries that trimmed case.
   The vendored upstream test `tests/test_ple_pageable.py` ships for GPU hosts.

The closed PR 54129 gather backport, its 6.8k-line test harness, the
resident-loading repair and the B12x QSA bridge target pre-rebase code that no
longer exists and were removed with this rebase; they remain in git history
and in the v0.3.1 evidence. Direct checkpoint mapping requires
`CU_DEVICE_ATTRIBUTE_PAGEABLE_MEMORY_ACCESS_USES_HOST_PAGE_TABLES`, verified
on this recipe's RTX PRO 6000 (driver 615.71.09) as 0, so checkpoint mapping is
Spark-only here and RTX defaults to the upstream host-offloaded resident
table. The `--engram-config` mapping and per-platform `PLE_MMAP` defaults are
recipe launcher work, not upstream code.

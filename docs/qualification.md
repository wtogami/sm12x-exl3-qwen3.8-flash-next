# Qualification ledger

Selection priority: the user clarified that C1 matters most. Final defaults
should favor measured C1 performance; retain C16 tuning and capacity results
as documented tradeoffs rather than optimizing the default primarily for C16.

**Superseded 2026-10-26: C1 is no longer the overriding selection priority.**
The deep-arrival residency measurements at the end of this ledger are the reason
— the 0.94/0.96/0.97/0.98 differences are invisible at C1 and reach 2x at
four-way deep load. Everything above stays as the record of how today's defaults
were chosen, but any decision in this file justified by "C1 matters most" is now
open for revisit, starting with `GPU_MEMORY_UTILIZATION` and the MTP draft length.

Status: development; no serving or performance claims yet.

## Required release gates

Both checkpoints must independently pass these gates on a single RTX PRO 6000
Blackwell 96 GB (TP=1):

- Original quantization preserved, including EXL3 K4/K5 per-projection allocation
  in target and MTP experts. Verify numerical behavior against original weights.
- N-gram tables fully resident in host RAM; token embedding table in host RAM.
- FP8 KV, vision enabled, MTP enabled, 262144 maximum context if capacity permits.
  Qualify concurrency 16, or at least 8, and document measured capacity limits.
- Tune fixed MTP depths and evaluate adaptive MTP on throughput and correctness.
- Seven content blends, orchid repeat, prefill matrix, context decode scaling,
  hard-mode tool evaluation, vision, long-context and graph/replay verification.
- Profile actual serving paths; select and qualify B12x kernels by numerical
  evidence and speed. Push any B12x work to tpurtell/sparkinfer-glmrt.
- Reproducible pinned Docker build, launch/download/stop scripts, complete README
  with raw benchmark evidence, clean-image verification, image publication and
  GitHub release. User will manually change package visibility after publication.

## Initial evidence (2026-09-07)

- Reference: `/home/tj/Developer/brandon-glm-5.3-flash/recipe`.
- B12x fork HEAD: `53f9d89c16f70e6580d0015de2934a882c61ed29`, merging Qwen
  support while retaining GLM and mixed EXL3 kernels. Local checkout: `.work/b12x`.
- Base image downloaded: `vllm/vllm-openai:qwen38-flash-next` at digest
  `sha256:fc120ece0a388cc0aa1caad4a9f1cd92113484ab7ec2fd0efadd62585be05bf8`.
  Torch `2.13.0+cu130`, vLLM `0.1.dev20073+g8e685d198`, CUTLASS `4.6.2`.
  Source copied to `.work/vllm` from idle inspection container `qwen38-source`.
  Model implementation: `vllm/models/qwen3_8_flash_next/nvidia/`; PLE already
  has a `VLLM_PLE_CPU_OFFLOAD` process path. No EXL3 module is present in base.
- Two idle RTX PRO 6000 GPUs, each 97887 MiB; 183 GiB total system RAM.
- Checkpoint header audit receipts in `benchmarks/*-checkpoint-audit.json`.
  These verify indexed tensors, projection metadata and packed shapes, not
  numerical inference correctness or weight digests.
- EXL3 revision `73a050c27b8c488c65acd6d1c74e45ff02be5fab`:
  76900493024 bytes outside names containing `ple`/`ngram`, 102466171160 bytes
  in those names; 5951 experts have unequal projection bitrates.
- NVFP4 revision `2061e0b0c5d92bdf7c8fbd4241bbc2af239d7e2d`:
  81373920992 bytes outside names containing `ple`/`ngram`, 51265925402 bytes
  in those names. These name-based groups include PLE auxiliary tensors and
  must not be treated as exact runtime residency measurements.
- EXL3 MTP uses mixed K4/K5; NVIDIA MTP metadata specifies FP8_PB_WO.
- Model config declares max_position_embeddings=262144, 512 experts, hidden
  width 2560, expert width 640, 48 target layers and one MTP layer.

## Next work

Inspect the downloaded vLLM model, PLE offload, MTP, NVFP4, and quant loader APIs.
Port the GLM recipe's mature EXL3 adapter and mixed-projection preparation to
Qwen namespaces and geometry. Establish independent single-GPU baseline servers,
then address correctness/capacity and profile before tuning. GLM-specific MLA,
mHC and DCP patches require architecture review, not mechanical reuse.

## Runtime integration progress

- `qwen38-rtx:dev1` builds from the pinned Qwen base plus the released GLM
  adapter artifact, preserving its mixed projection preparation. Build/import
  passed. `patches/port-exl3-qwen38.py` adds Qwen config types, MTP
  metadata aliases, and distinguishes individual EXL3 trellis tensors from
  fused expert-bank tensors in the modern vLLM loader. Serving remains unproven.
- NVIDIA baseline `qwen38-nvfp4-baseline` exited during construction with
  `NotImplementedError: Qwen3.8-Flash-Next QSA requires a BF16 main KV cache`.
  Requested TP1, FP8 KV, context 262144, 16 sequences, MTP3, batch tokens 2048,
  memory utilization 0.94, PLE CPU offload. Full local log:
  `.work/nvfp4-baseline.log`.
- FP8 QSA integration is required. B12x already contains BF16/FP8 E4M3 sparse
  GQA support with explicit K/V descales in `attention/qsa/_sparse_gqa.py`;
  its serving integration and correctness gates are outstanding.
- `qwen38-exl3-loader-dev1` is a diagnostic startup on GPU0, port 8001:
  MTP3, 16 sequences, eager mode, 8192 context, BF16 KV, PLE CPU offload.
  This isolates the EXL3 loader from the known FP8 construction failure and
  does not qualify the requested release configuration. Inspect current
  container state and logs before proceeding or restarting.

## QSA and mixed kernel qualification

- B12x `c76a40ee684cb3ef7d2c223d56a9b9cff25a3a1e` is pushed to the fork's
  master branch. It fixes the implicit mixed projection tile selection for
  Qwen H2560/I640: both FC stages use N128 when N256 cannot divide the
  projection. Matching FC1/FC2 CTA thread counts are preserved. Two planner
  tests passed, plus two SM120 K4/K5 numerical-versus-serial and CUDA graph
  replay tests, covering packed and direct routes. This is kernel evidence,
  not full-model correctness evidence. Logs: `.work/qwen-mixed-tests.log`.
- Before this fix, `qwen38-exl3-loader-dev1` loaded all 22 shards and prepared
  mixed target experts, then failed in its first forward with an invalid
  N256 projection tile. Log: `.work/exl3-loader-dev1.log`. The container exited.
- Four B12x QSA tests passed on GPU1: FP8 3008-token-page reference/graph
  replay, direct binary reuse, and BF16/FP8 high physical-page-offset cases.
  Command: `python3 -m pytest /opt/b12x/tests/attention/test_qsa_sparse_gqa.py
  -q -k 'fp8_3008_page or high_physical_page_offsets or reuses_direct_binary'`.
  Log: `.work/qsa-kernel-tests.log`.
- `qwen38-rtx:dev2` built successfully with an initial B12x sparse-GQA bridge.
  It retains the vLLM selector and cache writes, passes K/V descales, and
  shares scratch between sequential layers on each stream. This currently
  uses a pinned private B12x launch API; public planning and complete serving
  replay qualification remain outstanding.
- `qwen38-nvfp4-fp8-dev2` passed the FP8 QSA construction gate and loaded the
  target, then exited loading MTP: no `w2_weight_scale_inv` parameter for
  `mtp.layers.48.mlp.experts.0.down_proj.weight_scale_inv`.
  NVIDIA MTP's FP8_PB_WO needs both per-layer quant-config index remapping
  (checkpoint layer 0 to runtime layer 48) and block-FP8 expert support in
  ModelOpt mixed configuration. Log: `.work/nvfp4-fp8-dev2.log`.
- Next: rebuild with the pushed mixed geometry fix and qualify EXL3 forward;
  repair NVIDIA MTP block-FP8 loading; validate actual FP8 cache scales and
  serving outputs. Neither quant is yet serving successfully.

## MTP loader and bridge corrections

- `dev3` EXL3 loaded target plus mixed-projection MTP at layer 48, using
  73.17 GiB reported model memory. V2's MTP prefill reached 2048 rows while
  the old GLM draft arena allowed only concurrency-sized batches. The adapter
  now plans draft capacity from max_num_batched_tokens. This corrects an
  observed execution path; it is not an increase in configured concurrency.
- NVIDIA `dev4` loaded target and MTP, using 76.41 GiB reported model memory.
  The new ModelOpt patch remaps MTP quantized-layer metadata to layer 48 and
  dispatches FP8_PB_WO experts to native vLLM block-FP8 with dynamic activation
  quantization. Original E4M3 weights and BF16 2D scales are retained; no
  checkpoint rewrite. Numerical task-quality verification remains required.
- NVIDIA `dev4` then exposed a QSA bridge scratch validation error. The bridge
  now selects the unsplit direct path with no partial tensors above 64 rows.
  Eight bridge GPU checks passed: BF16/FP8 × rows 1,64,65,2048, dense attention
  oracle plus graph replay after query mutation. Receipt:
  `benchmarks/qsa-bridge-gpu.txt`; runner:
  `scripts/test-qsa-bridge.py`. Physical NHD layout and non-unit FP8
  descales are covered. These checks do not qualify complete model outputs.
- `dev5` probes were deliberately stopped after the bridge test identified
  the missing unsplit flag, before spending another startup on that known
  error. `dev6` incorporates the tested bridge fix and is building from
  `build.sh` (local log `.work/build-dev6.log`).
- Added executable build/download/start/stop scripts and immutable model
  profiles. They expose tuning controls and pin TP1/FP8/MTP/PLE offload.
  They are development commands, not a qualified release. Token embedding
  offload, complete host-residency checks, and all full-model release gates
  remain outstanding.

## Host embeddings and single-GPU PLE worker

- Native CUDA UVA allows the ordinary embedding gather to access pinned host
  memory. `qwen_host_embedding.py` applies it only to token embeddings, not
  the output head. A GPU test confirms `cudaPointerGetAttributes` reports
  host memory, exact lookups, and exact graph replay after token-ID mutation.
  Receipt: `benchmarks/host-embedding-gpu.txt`.
- `dev6` target+MTP memory profiling completed for both quants. The processes
  then stalled in real warmup; a live py-spy stack found the driver waiting
  during QSA kernel loading. Source inspection shows PLE spawn/wait methods
  are called only in `multiproc_executor.py`, not the default TP1 uniproc
  executor. Both probes had a registered PLE connector but no PLE CPU process.
  The GPU semaphore wait consequently had no producer. They were explicitly
  stopped after this diagnosis, not merely because observation timed out.
- `start.sh` now selects `--distributed-executor-backend mp` with TP1.
  This uses vLLM's native PLE worker lifecycle and still exposes exactly one
  GPU per server. `dev7` includes the verified host-token-embedding helper;
  `qwen38-exl3-fp8-dev7` and `qwen38-nvfp4-fp8-dev7` are the new probes.
- Ported the reference's generic decode/prefill, seven semantic content and
  vision harnesses, with provenance. Full benchmark execution remains pending.

## NVIDIA FP8 PLE and first serving diagnostic

- `dev7` revealed that native PLE dispatch recognized `Fp8Config` but not
  ModelOpt mixed metadata. NVIDIA's FP8 ngram table was allocated as BF16;
  concurrent EXL3 startup exhausted host RAM. The new patch selects the
  existing scalar-scale FP8 PLE implementation for the actual NVIDIA layer
  metadata. A storage test verifies FP8 CPU allocation, scalar scale loading,
  and byte-exact CPU lookup (`benchmarks/nvidia-ple-storage.txt`).
  This isolated test mocks tensor-parallel rank/world size; it does not test
  the complete serving lifecycle.
- `dev8` NVIDIA successfully starts its PLE CPU worker, loads target and
  MTP, and serves with host token embeddings, host FP8 PLE, FP8 KV, MTP3,
  TP1 and eager execution. An arithmetic smoke returned 42 for 19+23.
  Reported KV capacity is 661722 tokens: 16 scheduler slots do **not** imply
  16 simultaneous 262144-token requests (capacity ratio is about 2.52).
- The first seven-workload diagnostic is preserved in
  `benchmarks/nvfp4-dev8-seven-diagnostic.jsonl`. It has no warmup,
  one repetition and eager execution; it is not a release performance result.
  Code, greeting, topic and JSON pass the inherited validators. Math runs
  out of its 128-token budget before the final answer; fable has 173 words
  against a 140–170 requirement. Chinese explains the mechanism and fork
  example, but fails the validator's literal phrase check. These failures
  remain recorded; full numerical and task-quality qualification is pending.
- Added a streaming workload harness retaining complete responses, token IDs,
  usage and SSE chunk times. Decode timing excludes every token in the first
  burst; the conventional N−1 rate is also retained. Compiler caches now
  persist under `~/.cache/qwen38-rtx/<quant>` between recipe containers.
- EXL3 mixed K4/K5 projection support remains mandatory, including MTP.
  Its loader and geometry tests pass, but successful full serving and all
  requested benchmark suites still need qualification. Initial full-model
  tests run one quant at a time to respect host RAM capacity.
- The eager NVIDIA orchid diagnostic (one warmup, three measured runs) also
  fails exact-100 repetition quality: measured counts 101, 750, 750; the last
  two terminate at the 1500-token limit. Raw responses and timings are in
  `benchmarks/nvfp4-dev8-orchid-diagnostic.jsonl`. The roughly 90–94
  measured decode tokens/s are diagnostic throughput, not successful-task
  performance. MTP and target-only comparisons remain necessary.

## EXL3 first serving and vision

- `dev8` EXL3 now serves successfully on GPU0 with target and MTP retaining
  separate K4/K5 gate/up/down assignments. MTP layer48 reports K4 expert
  counts 440/392/320 and K5 counts 72/120/192 for these projections.
  Target plus MTP loading reports 71.99 GiB; host token offload is logged
  twice at 1.184 GiB each. The PLE worker verifies its BF16 table and starts.
- EXL3 reports 837333 KV tokens (3.19×262144), with 16 scheduler slots.
  The development image, arguments, GPU selection, and relevant startup
  evidence for both quants are in `benchmarks/dev8-runtime.json`.
- The eager, unwarmed EXL3 seven-workload diagnostic passes code, greeting,
  topic and JSON. Math again truncates at 128 tokens after correct
  intermediate calculations; fable has 176 words; Chinese fails the
  inherited literal phrase check. Original evidence is preserved in
  `benchmarks/exl3-dev8-seven-diagnostic.jsonl`.
- For subsequent runs, the math output budget is 256 tokens. The Chinese
  proxy now accepts 寫入觸發複製 and 寫入時觸發複製 as well as 寫入時複製;
  four bullets, fork and page checks remain. Positive variants and
  missing-term counterexamples were checked. This is a wording check,
  not comprehensive semantic grading. The fable criterion is unchanged.
- EXL3 correctly reads the ordered numerals in 1, 4 and 16 images with native
  nonthinking chat: `benchmarks/exl3-dev8-vision-diagnostic.json`.
  No maximum-image rejection claim is made; no explicit limit was configured.
- Added exact-depth C1 synthetic context/decode harness with retained SSE
  token IDs and timings. Launch supports MTP_TOKENS=0 for target-only control
  runs. Native batch-size speculative scheduling is available; confidence
  adaptive verification is DSpec-only in this pinned source. Tuning remains
  pending, and B12x currently reports heuristic MoE choices for Qwen geometry.
- EXL3 eager context probes complete at 2048, 8192, 32768, 131072 and
  261632 prompt tokens. The last point generates 128 further tokens and
  measures 40.780 s TTFT / 57.109 decode tokens/s. These are single, unwarmed
  synthetic probes, not retrieval quality or final performance measurements.
  Raw receipts: `exl3-dev8-context-diagnostic.jsonl` and
  `exl3-dev8-long-context-diagnostic.jsonl` under `benchmarks`.
  Both GPUs report a 400 W power limit during these probes.
- Eager short-context parallel continuations complete at C8 and C16 after
  one warmup per point, emitting 64 tokens per sequence. Single measured
  aggregate rates are 203.71 / 407.27 tokens/s using the global first-to-last
  SSE window and sum(N−1) numerator. This is a shared-prompt continuation
  load, not 16 independent long-prefill requests; timing arrays are retained
  in `benchmarks/exl3-dev8-concurrency-diagnostic.json`.
- EXL3 orchid exact-count results are 100/750/100 across three measured
  runs (one prior warmup produced 101). The 750 case hits the 1500-token
  output limit. Raw diagnostic responses are retained in
  `benchmarks/exl3-dev8-orchid-diagnostic.jsonl`; two successes do not
  qualify the failing repetition workload. The next probe enables CUDA
  graphs with the same image, checkpoint and MTP3 settings.

## QSA workspace on the graph capture stream

- `qwen38-exl3-fp8-graph-dev8` fails its first graph startup because vLLM
  warms QSA on one CUDA stream and captures on another. The bridge keyed
  scratch by stream and rejected allocation during capture.
- The bridge now uses PyTorch's graph-aware allocator for a new stream and
  retains the tensors for graph replay. Per-stream isolation remains.
  Eight BF16/FP8 × 1/64/65/2048-row GPU checks pass against the dense oracle
  with a separate capture stream and mutated-query replay. Receipt:
  `benchmarks/qsa-cross-stream-gpu.txt`. `dev9` contains this correction;
  full-model graph qualification still needs a successful rerun.
- Tool evaluation is prepared in an isolated checkout at the reference
  recipe's commit `cf54b4bfe705f12f71e8866f10730572497c8105`, version
  2.6.1.dev45, containing 88 public cases including Hard Mode. The older,
  locally modified checkout in `~/Developer/tool-eval-bench` is untouched.

## EXL3 graph-mode serving

- `qwen38-exl3-fp8-graph-dev9` starts successfully with piecewise and full
  CUDA graphs. Capture takes 7 seconds and 0.96 GiB; KV capacity reports
  850059 tokens (3.24×262144). Runtime evidence is in
  `benchmarks/exl3-dev9-graph-runtime.json`. The torch profiler is
  configured but was not activated during the content measurements.
- The seven-workload run uses one warmup and three measured repetitions,
  native nonthinking chat, temperature0, MTP3 and 400 W. Weighted decode is
  152.97 tokens/s; medians: code205.58, math214.97, fable117.65,
  greeting175.90, topic148.78, JSON167.21, Chinese127.58 tokens/s.
  19/21 content checks pass; two fables exceed the requested word count.
  This is a measured development configuration, not a final tuned release.
- Timed-suite MTP counters record 2137 accepted / 3813 draft tokens, across
  1271 drafts: 56.05% acceptance and mean acceptance length2.681.
  The harness waits 11 seconds (outside request timing) before and after
  timed runs for vLLM's 10-second statistics interval. Raw snapshots and
  all responses are in `benchmarks/exl3-dev9-graph-seven.jsonl`.
  Counters are suite-level and require exclusive access to the endpoint.
- A full 88-case tool run, with thinking enabled and evaluation parallelism8,
  is underway against this configuration. Its results, graph vision/context
  reruns, target-only controls, MTP tuning and NVIDIA graph qualification
  remain pending.

## Tool evaluation, retrieval and constraint enforcement

- EXL3 dev9's 88-case run scores 145/176 points (82/100 rounded): 64 pass,
  17 partial, 7 fail. Hard Mode alone scores 29/38: 13 pass, 3 partial,
  3 fail. Full traces and summary are preserved as
  `benchmarks/exl3-dev9-graph-tools.md` and `.json`; the benchmark
  also persisted its SQLite run in the isolated tool-eval checkout.
- All six graph retrieval checks pass: exact keys at 5%, 50%, 95% character
  positions in 8192-token and 240000-token filler archives. Actual long
  prompts are 240070–240072 tokens, including chat framing/instructions.
  Receipt: `benchmarks/exl3-dev9-graph-retrieval.jsonl`. This is a
  single-key synthetic test, not comprehensive long-context task quality.
- TC-45 exposed an API constraint bug, independently reproduced for required
  and named tool choice with thinking both on and off. The combined Qwen
  parser engine skips grammar adjustment when reasoning/tool adapters are
  collapsed. The new patch keeps DelegatingParser for structural-tag tool
  adapters. A real-tokenizer CPU regression verifies required/named grammar,
  unconstrained auto/none behavior, and reasoning plus automatic call parsing.
  Receipt: `benchmarks/tool-constraints-cpu.txt`. `dev10` builds with
  this correction; live enforcement still needs qualification and the old
  tool score remains a pre-fix result.
- A short profiled EXL3 code completion identifies mixed MoE and BF16 matrix
  kernels as the main GPU time consumers. Raw trace and kernel-only totals:
  `benchmarks/exl3-dev9-code-profile.trace.json.gz` and
  `exl3-dev9-profile-summary.json`. Profiled timings are not throughput results.
- Prefill harness invocations now use a unique nonce so repeating a run on
  the same server cannot accidentally reuse the prior run's prompt cache.

## NVIDIA graph-mode serving and live tool constraints

- `qwen38-nvfp4-fp8-graph-dev10` successfully starts with FlashInfer MoE
  autotuning and CUDA graphs. Capture uses 1.00 GiB and takes 9 seconds;
  reported KV capacity is 621001 tokens (2.37×262144). Both token tables
  and the FP8 PLE remain host-resident. Runtime receipt:
  `benchmarks/nvfp4-dev10-graph-runtime.json`.
- All 16 live API tool-constraint checks pass: required/named/auto/none ×
  thinking on/off × streaming/nonstreaming. Raw requests and responses are
  in `benchmarks/nvfp4-dev10-tool-constraints.jsonl`; runner:
  `scripts/test-api-tool-constraints.py`.
- The seven-workload blend (one warmup, three measured runs, MTP3, 400 W)
  records 149.46 weighted decode tokens/s. Medians: code202.67, math214.94,
  fable115.08, greeting181.44, topic149.99, JSON174.13, Chinese119.90.
  18/21 content checks pass; all three failures are overlong fables.
  MTP counters: 2010 accepted / 3879 draft tokens over 1293 drafts,
  51.82% acceptance and mean acceptance length2.555. Raw evidence:
  `benchmarks/nvfp4-dev10-graph-seven.jsonl`.
- NVIDIA graph vision passes at 1/4/16 images. Its single-probe synthetic
  context sweep completes at 8192, 131072 and 261632 prompt tokens plus128
  output tokens. The last point measures 27.865 s TTFT and238.73 decode
  tokens/s. These are synthetic diagnostics, not final context-quality
  results. Receipts: `nvfp4-dev10-graph-vision.json` and
  `nvfp4-dev10-graph-context-diagnostic.jsonl` under `benchmarks`.
- Independent-client C8/C16 probes complete with unique prompts and128
  forced output tokens each. Measured global-window rates are549.68/544.11
  tokens/s. C8 has8 overlapping first-to-last SSE intervals; C16 peaks at13,
  with three TTFTs delayed about2.1–2.3 seconds. Thus this MTP3/.94 memory
  configuration has not demonstrated16 simultaneous active sequences.
  C8's logged KV usage is59.3%, consistent with cache pressure at16.
  The client runner preserves each request's timing arrays. MTP2 and memory
  budget tuning will test whether16 active sequences fit.
- NVIDIA graph MTP3 orchid counts are102/102/750 in measured runs; all
  fail exact100, and the last hits the1500-token budget. Warmup also hits
  the budget. Raw responses and timed-suite MTP counters are preserved in
  `benchmarks/nvfp4-dev10-graph-orchid.jsonl`. These repetition rates
  must not be represented as successful-task throughput.

## NVIDIA MTP2 comparison and vocabulary-kernel candidate

- NVIDIA MTP2 at the same .94 memory fraction and dev10 image reports644874
  KV tokens, with0.91 GiB graph capture. The seven-workload blend measures
  152.40 weighted tokens/s versus149.46 for MTP3. Code slows182.82 versus
  202.67 tokens/s, while fable and Chinese improve. Suite acceptance is
  65.50%, mean acceptance length2.310. One fable is overlong; two Chinese
  answers explain COW correctly but fail the literal-phrase proxy. Original
  validator results remain preserved in `nvfp4-dev10-mtp2-seven.jsonl`.
- Crucially, the independent C16 test reaches16 overlapping stream intervals
  and881.49 aggregate tokens/s. C8 reaches552.95. This demonstrates the
  requested16-client capacity at short context with MTP2, unlike the MTP3
  probe's13 overlapping streams. The full88-case tool run is now underway
  with MTP2. Runtime and client receipts are named `nvfp4-dev10-mtp2-*`
  under `benchmarks`.
- A GPU0 microbenchmark tests B12x's existing planned BF16 vocabulary
  projection at Qwen's actual M1/K2560/N248320 geometry against native
  F.linear. Median CUDA time is772.04 versus823.78 microseconds (about6.3%
  lower), over7 interleaved graph measurements of20 calls each. Three
  mutated-input numerical checks pass; the full weights exceed L2. This
  kernel is not yet integrated into serving, and no end-to-end gain is
  claimed. Runner: `scripts/benchmark-vocab.py`; receipt:
  `benchmarks/bf16-vocab-k2560-n248320.txt`.
- NVIDIA MTP2's full tool run scores153/176 points (87/100):69 pass,
  15 partial,4 fail. Hard Mode scores32/38:15 pass,2 partial,2 fail.
  TC-45 now passes with an enforced calculator call. Full traces and summary
  are retained as `benchmarks/nvfp4-dev10-mtp2-tools.md` and `.json`.
- All six NVIDIA MTP2 retrieval checks pass at early/middle/late positions
  in8192- and240000-token filler archives. Actual long prompts contain
  240071–240073 tokens. Receipt: `nvfp4-dev10-mtp2-retrieval.jsonl`.
- `dev11` adds an opt-in vocabulary bridge (`B12X_VOCAB=1` in start.sh,
  disabled by default). It preplans/precompiles after loading and only
  replaces single-row BF16, unbiased projections at the exact checkpoint
  geometry. Native multi-row, biased and explicit FP32-head paths remain.
  The actual logits-processor bridge passes mutated graph replay, dtype and
  native-fallback checks (`benchmarks/vocab-bridge-gpu.txt`).
  A same-MTP2 serving comparison remains required before enabling it by default.

## Candidate defaults after serving comparisons

- Enabling the planned B12x vocabulary projection with NVIDIA MTP2 measures
  150.14 weighted tokens/s versus152.40 without it. Code improves186.14
  versus182.82, but Chinese and the overall blend regress; accepted draft
  fraction also changes63.55% versus65.50%. Three repetitions do not
  establish an overall win, so the option stays disabled by default. Raw
  evidence: `benchmarks/nvfp4-dev11-mtp2-vocab-seven.jsonl`.
- The B12x HC combine+norm API rejects vLLM's strided injection view at M>1.
  A second comparison includes the necessary contiguous packing. Native is
  faster at M1/4/16/64; at M2048 B12x is only slightly faster (79.38 versus
  81.27 microseconds). Mutated graph outputs agree with native within the
  stated tolerance. Native remains the serving choice; no HC bridge is
  installed. Runner: `scripts/benchmark-hc.py`; complete timings:
  `benchmarks/hc-native-vs-b12x.txt`.

## MTP1 and GDN comparisons

- NVIDIA dev11 MTP1 at .94 measures 130.35 weighted tokens/s on the same
  seven-workload suite, versus 152.40 for MTP2. Acceptance is 76.58%, but
  mean acceptance length is only 1.766. Independent C8/C16 probes improve
  to 597.43/915.16 tokens/s. These are one measured run after one warmup;
  a batch-size-dependent draft schedule remains a candidate, not a default.
  Receipts: `benchmarks/nvfp4-dev11-mtp1-*`.
- The public B12x GDN transaction was compared with native post-convolution
  GDN at QK16/V48, FP32 recurrent state, BF16 activations and sigmoid gating.
  Native was faster for B1 with 1/3/4 tokens and B8 with 3 tokens; for B8,
  native measured 30.43 microseconds versus B12x 42.99. These comparisons
  use contiguous inputs and restore state outside the timed CUDA events.
- The B16/3-token candidate failed numerical verification after mutated
  graph replay. The independent reference agrees with native (maximum
  output error 0.015625), while B12x has a maximum error of 1.684 and a
  state error of 0.06675. The runner stops without reporting B16 timing.
  No B12x GDN bridge is installed. Native remains the serving path based
  on both performance and correctness. The reproducible runner is
  `scripts/benchmark-gdn.py`; full measurements and failure evidence
  are in `benchmarks/gdn-native-vs-b12x.txt`.
- NVIDIA's no-MTP control measures 96.38 weighted tokens/s, with C8/C16
  independent clients at 500.96/855.50 tokens/s. Static MTP2 improves the
  blend by approximately 58%; MTP1 improves the C16 probe by approximately
  7%. Raw responses, client timings and runtime configuration are retained
  in `benchmarks/nvfp4-dev11-mtp0-*`.

## Adaptive draft schedule and precise NVFP4 MoE candidate

- NVIDIA's native dynamic schedule `[[1,4,2],[5,16,1]]` at maximum MTP2
  measures 146.04 weighted tokens/s, below static MTP2's 152.40. Its
  independent C8/C16 probes measure 585.89/858.97 tokens/s. It does not
  establish an overall advantage, so static MTP2 remains the leading profile.
  Runtime, raw responses and all client timings are retained as
  `benchmarks/nvfp4-dev11-adaptive21-*`.
- EXL3 MTP2 measures 149.16 weighted tokens/s, versus MTP3's 152.97.
  Code measures 179.04 versus 205.58. Independent C8/C16 probes measure
  532.52/718.39 tokens/s. Receipts: `benchmarks/exl3-dev11-mtp2-*`.
- The pinned fork's checkpoint-backed NVFP4 MoE benchmark compares layer 0,
  TP1, H2560/I640/E512/top10, shared activation scales, synthetic routes,
  three timing repetitions and 256 MiB L2 eviction per launch. Fast math
  fails the unchanged 0.9999 cosine threshold at six of seven shapes;
  FlashInfer also misses that oracle threshold and is reported separately
  as a reference warning. No tolerance was relaxed.
- Disabling fast math passes all seven candidate oracle checks, with
  cosine similarity 0.999958–0.999966. Precise B12x CUDA graph medians at
  M1/3/4/16/48/64/2048 are 47.1/73.7/98.3/264.2/591.9/692.2/1283.1
  microseconds, versus FlashInfer 61.4/100.4/118.8/311.3/632.4/738.6/1371.6.
  Full command configuration, error metrics and timing ranges are in
  `benchmarks/nvfp4-moe-fast-math.txt` and `nvfp4-moe-precise.txt`.
  These are component measurements; serving integration is still under test.
- The opt-in `B12X_NVFP4=1` bridge in dev13 passes all 21 mutated graph
  checks against the unchanged oracle, using the checkpoint's real weights
  and scales. The test exercises the patched FlashInfer expert class,
  BF16 input deferral, output dimensions and pointer-identical weight
  storage at M1/3/4/16/48/64/2048. Runtime scratch is shared serially across
  layers within each CUDA stream. Native routing and shared-expert handling
  remain in vLLM's modular pipeline. Full-model qualification is pending.
  Receipt: `benchmarks/nvfp4-moe-bridge-gpu.txt`; runner:
  `scripts/test-nvfp4-moe.py`.
- EXL3's no-MTP control measures 92.76 weighted tokens/s; MTP3 is about
  65% faster on the blend. C8/C16 target-only probes measure 465.99/762.88
  tokens/s. The C16 result exceeds static MTP2's 718.39, motivating a
  shorter-draft batch comparison. Receipts: `benchmarks/exl3-dev12-mtp0-*`.
- EXL3 MTP1 measures 131.54 weighted tokens/s and 141.99 median code
  tokens/s. Its C8/C16 probes improve to 561.14/861.92 tokens/s; all 16
  client streams overlap. This supports testing a shorter draft at larger
  batch sizes, while retaining longer drafts for low concurrency.
  Receipts: `benchmarks/exl3-dev13-mtp1-*`.
- EXL3 MTP4 improves C1 code/math medians to 217.18/236.29 tokens/s,
  versus MTP3's 205.58/214.97. Its weighted C1 blend is 150.63, slightly
  below MTP3's 152.97; fable and Chinese slow to 111.16/116.42. C8/C16
  probes measure 408.90/486.12, with only 14 overlapping C16 streams.
  The C1 workload tradeoff remains useful even though this is not the
  best measured mixed-workload default. Receipts:
  `benchmarks/exl3-dev13-mtp4-*`.
- EXL3's dynamic schedule `[[1,4,3],[5,16,1]]` measures 150.71 weighted
  C1 tokens/s and 202.67 median code tokens/s. C8/C16 probes measure
  516.51/792.07. It does not establish a C1 advantage over static MTP3,
  which is selected for final qualification. Receipts:
  `benchmarks/exl3-dev13-adaptive31-*`.

## Final EXL3 qualification

The `benchmarks/exl3-final/` receipts use dev13, static MTP3, CUDA
graphs, FP8 KV, .94 GPU memory fraction and GPU1 at 400 W. The CPU is an
AMD Ryzen Threadripper 9970X (32 cores/64 threads); host RAM is 183 GiB.

- C1 seven-workload blend: 152.82 tokens/s; code median: 202.99 tokens/s.
- Reference async task-runner coding task, temperature .2: C1 median
  185.55 tokens/s at the 159-token task prompt, and 190.37 tokens/s at
  261632 prompt tokens. These 256-token forced completions measure speed,
  not generated-code correctness. Full response text and token timings remain.
- Sampled prose at temperature .7, 256 tokens, three measured runs after
  two warmups: C1/C2/C4/C8/C16 medians are
  111.70/191.37/347.87/532.66/696.96 tokens/s. All 16 client streams overlap
  in every measured C16 run. Earlier 128-token tuning probes are separate
  experiments and should not be substituted for this full curve.
- Both the six-point prefill matrix and six-point synthetic context/decode
  curve completed through 261632 prompt tokens. The exact boundary probe
  also returned all 256 requested tokens after a 261888-token prompt:
  262144 total tokens, with 40.67 seconds TTFT and 227.99 decode tokens/s.
- All 16 API tool-choice checks, 1/4/16-image checks and six early/middle/late
  retrieval checks at 8K/240K filler lengths pass.
- Full 88-case tools: 152/176 points (86/100), with 67 pass, 18 partial and
  3 fail. Hard Mode: 32/38 points, with 14 pass, 4 partial and 1 fail. This
  run includes the parser fix; full traces and all partial/failing cases are
  preserved in `tools.md` and `tools.json`.
- Orchid measured counts are 750/750/101/100/102; only one of five meets the
  exact contract. The 750-word responses hit the 1500-token cap. Raw failures
  remain in `orchid.jsonl` and are not described as successful-task throughput.

## NVIDIA precise expert candidate and release profile extension

The dev13 precise B12x NVFP4 expert bridge passed its component oracle but
lost end-to-end at static MTP2: seven-workload weighted C1 decode was 136.74
versus 152.40 tokens/s with native FlashInfer. Draft acceptance was 64.10%
versus 65.50%; the small acceptance difference does not establish the cause
of the throughput loss. C8/C16 probes were 519.20/809.45 tokens/s. The optional
bridge remains off. Raw candidate receipts are `benchmarks/nvfp4-dev13-b12x-mtp2-*`.

The recipe now lives at the repository root. Historical raw receipts retain
the original command paths as provenance; current commands and documentation
use the root layout.

The requested `exl3-ple8` release profile pins
`wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-PLE-FP8-v1` at
`888306bd3996d6317758c07df50622829259ad17`. It retains EXL3 mixed projections
and substitutes NVIDIA's FP8 PLE table and shared scale. The loader honors
its explicit `qflashrt.fp8-ple.v1` metadata, scoped to the annotated table.
Qualification for this additional profile covers quality only; no separate
performance matrix or claim of measured performance equivalence is planned.

## NVIDIA final performance qualification

`benchmarks/nvfp4-final/` uses dev13, native FlashInfer experts, static MTP2,
CUDA graphs, FP8 KV and full host tables on GPU1 at 400 W. The weighted C1
blend is 151.11 tokens/s. The separate sampled reference coding task measures
166.92 tokens/s at C1 and 177.83 at a 261632-token prompt depth. Sampled prose
C1/C2/C4/C8/C16 medians are 113.24/213.17/365.63/620.24/930.71 tokens/s with
full stream overlap at each concurrency, using 256 output tokens, two warmups
and three measurements. These final results supersede short tuning probes for
reporting the selected profile.

API tool constraints pass 16/16, numbered-image tests pass at 1/4/16 images,
and all six retrieval checks pass. Exact context-boundary usage is 261888
prompt plus 256 output tokens, with 29.328 s TTFT and 194.70 decode tokens/s.
Seven content contracts pass 17/21; orchid is exact in 1/5 measured runs
(counts 102, 101, 101, 100, 101). Full tool results will be recorded separately.

The additional dev14 loader change selects FP8 PLE only when an EXL3 checkpoint
has the explicit hybrid annotation. Original EXL3 retains unquantized PLE;
the NVIDIA ModelOpt branch is unchanged and its dev14 storage regression
passes (`benchmarks/nvidia-ple-storage-dev14.txt`).

NVIDIA's final full tool run scores **149/176 (85/100)**, including **29/38**
Hard Mode points (13 pass, 3 partial, 3 fail in Hard Mode). The earlier tuning
run scored 153/176; the final run is reported without selecting the better
score. `benchmarks/nvfp4-final/tools.json` and `tools.md` retain all traces,
and `benchmarks/RESULTS.md` renders both completed performance profiles.

## EXL3 FP8-PLE quality qualification

The dev14 `exl3-ple8` profile loads both materialized PLE parameters (FP8
weight and scalar scale), preserves the 71.99 GiB GPU model allocation, and
captures graphs with static MTP3. Its host table payload is approximately
48 GiB rather than the BF16 parent's approximately 95 GiB. Header audits
confirm identical mixed-projection tier counts and 5951 unequal-tier experts.

Quality checks pass 16/16 tool API constraints, all 1/4/16-image probes and
all six retrieval cases at 8K/240K filler depths. Content contracts pass
18/21 and exact orchid repetition passes 2/5. The full tool suite scores **149/176 (85/100)**, with **32/38** Hard Mode
points. Overall counts are 65 pass, 19 partial and 4 fail. The evaluator flags
TC-33 internal-data request handling and TC-42 forbidden extra tool parameters.
These findings are retained in the full report rather than treated as passes. No performance matrix is run for this extra profile. *(Superseded for v0.31.0 by § exl3-ple8: the FP8-PLE variant — current image, three-way protocol.)*

The dev14 NVIDIA release-image regression also passes all 16 tool API checks;
receipts are `benchmarks/nvfp4-release-api-dev14.jsonl` and its runtime JSON.

## Release v0.1.0

The final original-EXL3 smoke run on dev14 passes all 16 tool API checks,
including streaming/non-streaming and thinking on/off. All three profiles
have now exercised the exact published image. Original EXL3 is left serving
on GPU1, port 8001, as `qwen38-exl3-release-dev14`; the other model test
containers are stopped. The user's `qflashrt-quant-wip` container is untouched.

The image is published as
`ghcr.io/tpurtell/rtx6k-exl3-qwen3.8-flash-next:v0.1.0` and `:latest`, with
immutable digest
`sha256:9dab4b0b3ce01eab748f3d264cabfc206be467e596e6470b1be68a2ddcfe6840`.
Registry inspection confirms its linux/amd64 manifest. The release archive
and manifest record the source and evidence hashes. GHCR package visibility
is the user's manual post-publication step.

## Release v0.2.0: mmap PLE

The reviewed PR #54129 port and scale-validation corrections pass 218 component
and runner tests, real-checkpoint shard-edge checks, and mutable CUDA graphs.
See [the review](mmap-review.md) for scope and receipts. Mixed EXL3 per-projection
K4/K5 support remains required and intact.

The new full matrix covers only `exl3-ple8` with mmap, MTP3, SERIAL=128 and FP8 KV.
The C1 seven-workload blend is 147.78 tokens/s; sampled async coding is 183.45
tokens/s and C16 sampled prose is 659.75 aggregate tokens/s. Content contracts
pass 18/21 and exact orchid repetition passes 2/5. All 16 API checks, 1/4/16-image
checks, six retrieval probes and the exact 262144-token boundary pass.
Tool evaluation at C8 completes all 88 cases with 146/176 points, including
27/38 Hard Mode points. All failed/partial responses remain in the receipts.
The evaluator reports no safety-warning entries; that is not a broader safety assessment.

After the performance suite, 128 clean checkpoint mappings span 47.68 GiB, with
19.45 GiB mapped resident pages and no anonymous or dirty mapped PLE pages.
This is a cache snapshot, not a low-RAM or cold-disk measurement.

The shipping default becomes `exl3` with mmap enabled, preserving BF16 PLE.
This defaults-only change adds no measurements. The benchmark and release
images have identical filesystem layers. All six profile/mode combinations
and the omitted-configuration default pass inert launcher checks. Original
EXL3/NVFP4 columns remain explicitly historical v0.1.0 resident-mode evidence.
The README integrates the new profile into all final tables, shows medians
without ranges and presents C8 tool points without normalized scores.
The historical resident PLE8 quality receipts remain available separately.

The published image is `ghcr.io/tpurtell/rtx6k-exl3-qwen3.8-flash-next:v0.2.0`,
with digest `sha256:bb252820ade1b6aa1316c45485186db90fe67f1d7ea48826169bf92f634d2e73`.
The GHCR package is public. The qualified mmap PLE8 server remains on GPU1,
port 8001; the shipping default is configured independently of that live test.

## Spark qualification and v0.3.0

Native arm64 qualification and tuning are complete,
with the original EXL3 BF16 PLE mmap profile. See
[Spark qualification and tuning](spark-work.md) and the
[complete four-profile report](../benchmarks/RESULTS.md). The historical RTX
receipts above remain unchanged.

Both v0.3.0 native images include the reviewed structured-output corrections.
See [Spark backport review](../benchmarks/spark-structured-review/README.md)
and [RTX update and qualification](../benchmarks/rtx-structured-review/README.md).
The RTX shipping BF16 mmap/MTP3 profile passes API16/16 and 29/29 live JSON
canaries, then completes C8 tools with 148/176 points and 29/38 Hard Mode points.
There are zero evaluator request errors and no FSM/termination errors in its
server log. This scoped requalification adds no RTX performance measurements.

## v0.3.1 resident mode repair

The v0.3.0 image was reproduced failing at `ple_layer.py:691` with
`QUANT=exl3 PLE_MMAP=0` on GPU1. Moving the resident GPU worker's metadata-only
load before mmap embedding access restores this path. The earlier inert
launcher checks verified arguments, but did not catch this model-loading bug.
The new regressions test actual loading without a GPU-owned embedding, and
full-model checks exercise both resident and mmap with the same corrected image.
See [v0.3.1 evidence](../benchmarks/resident-v031-review/README.md).
Historical performance and quality tables retain their original image IDs.

## v0.31.0 rebase (this branch)

Image `localhost/qwen38-rtx:local` at `23fa4d1b560d`, based on the official
`vllm/vllm-openai:v0.31.0` digest `sha256:a4a4c0437bf7…` after dropping the
now-upstream QSA FP8 backport and re-deriving the mmap PLE patch into the
release's common/nvidia split (see PROVENANCE.md). Qualified in
LONGCTX=1 (512K YaRN) serving mode with the 2048-token batch default:

- In-image assert-imports and both CPU AST suites pass; the reasoning-guard
  fake models the 0.31 copy-based draft-row inheritance. `test_ple_pageable`
  passes 25 with 5 Spark-only skips on the RTX PRO 6000. Launcher and
  platform-config regressions pass.
- Core stability suite: api-tools matrix 24/24 pass, vision pass, orchid
  10/10, seven 20/21 (sole failure `fable` run 0 at 183 words against the
  140..170 contract — a documented pre-rebase checkpoint behavior band),
  decode sweep C1 128.3, C2 220.4, C4 372.4, C8 593.4, C16 865.3 tok/s.
  Zero server errors.
- Cross-context retrieval: 9/9 needles exact across 131072, 240000 and
  480000 filler tokens at 5/50/95% positions
  ([receipts](../benchmarks/retrieval-v031.jsonl)).
- Context-depth ladder (uncached, C1, 3 runs): TTFT 12.9/28.3/58.1 s at
  131072/261632/523264 tokens; decode's high-acceptance mode 242–262 tok/s
  across depths ([receipts](../benchmarks/context-v031-ladder.jsonl)).
  The scheduler's speculative-decoding note about the 2048 batch budget
  showed no visible cost at these depths.

Note: the earlier v0.30.0 receipts (decode C1 124.8/C16 854.6; ladder TTFT
15.1/32.1/67.5 s at an 8192-token batch budget) are not comparable to this
section's numbers. After they were taken, the host's cooling was improved
and its power limit raised, so any cross-release deltas measured before and
after that change conflate hardware envelope with software. A same-day
cross-release A/B would be required for attribution.

v0.31.0 startup notes: the stricter env validation flagged the recipe's
EXL3 trellis knobs as unknown `VLLM_*` variables; they were renamed to the
non-reserved `EXL3_` prefix in this section's follow-up commit, at every
read site in the copied adapter and in the Dockerfile and capture tooling.
The scheduler logs a speculative-decoding note about the 2048 batch budget;
no degradation was observed at any ladder depth on this host configuration.

## 300 W power-limit comparison (2026-10-06)

The v0.31.0 qualification above was measured while the RTX PRO 6000 ran at a
600 W power limit. Both measurement days' engines were otherwise identical
(image `452cdc32105f`, LONGCTX=1 512K YaRN, 2048-token batch budget). Re-run
at the current 300 W limit:

- Quality unchanged: seven 20/21 (same `fable` word-count band), orchid 9/9,
  api-tools 16/16, vision pass, zero server errors.
- Context ladder TTFT medians (600 W -> 300 W): 12.9 -> 22.3 s (+72.7%) at
  131072; 28.3 -> 46.7 s (+64.9%) at 261632; 58.1 -> 97.3 s (+67.4%) at
  523264. Decode's high-acceptance mode fell ~10-12%.
- Decode sweep medians (600 W -> 300 W): C1 128.3 -> 114.7 (-10.6%),
  C2 -12.7%, C4 -16.7%, C8 -20.9%, C16 865.3 -> 595.0 tok/s (-31.2%).

Power scales throughput superlinearly with concurrency (bandwidth and clock
floors compound at C16) but only mildly at C1. Long-context prefill pays a
flat ~1.7x. Receipts: [power-300w-20261006](../benchmarks/power-300w-20261006/).
This also re-anchors the earlier caveat: the Oct 1 (v0.30.0) receipts were
taken before the envelope change and remain non-comparable without a
same-day A/B at matched limits.

## Power-envelope tuning study (2026-10-07, 4x8-pin rewire)

With four 8-pin connectors the card's VBIOS-declared limit returned to 600 W
(12VHPWR sense pins decode cable capability; a 2x8-pin adapter declares
300 W). Re-running the 523K/131K ladder across limits and clock policies:

| Config | 131K TTFT | 523K TTFT | Load power |
|---|---|---|---|
| `-pl 300` hard | 22.3 s | 96.4 s | 300 W |
| `-lgc 1250` + `-pl 360` | 21.9 s | 93.9 s | ~305 W |
| `-lgc 1300` + `-pl 360` | 20.8 s | 89.4 s | mean 323 W, peak 339 W |
| `-pl 330` unlocked | 21.1 s | 89.6 s | ~330 W |
| `-pl 360` unlocked | — | 82.6 s | 360 W (pinned, ~1462 MHz) |
| 600 W default | 13.1 s | 57.4 s | up to 600 W |

Findings: throughput tracks **sustained average power**, not the shape of
the limit. A clock-locked soft limit (headroom between operating point and
clamp for transients) matched a plain cap at equal average power to within
noise; the lock added nothing. Under the hard 300 W cap the governor is
already quiescent (clock spread p10-p90 = 60 MHz), so there is no
oscillation to damp and no undervolt-equivalent gain from `-lgc`.
Recommendation: set `-pl` to the wanted sustained-wattage directly; skip
clock locks. 330 W sits at the knee: 7% faster than 300 W for ~10% more
power. Receipts: [power-tuning-20261007](../benchmarks/power-tuning-20261007/).
`nvidia-smi -pl` does not persist across reboots; persist via a boot unit
if a cap should survive restarts.

## SM clock-lock sweep (2026-10-07)

Follow-up to the tuning study above, which tested locks only at two points
chosen to *match* a 360 W cap. Here `-lgc` is the primary control: the
power limit stays at the 600 W maximum so the lock is the intended limiter.
Points: free-running reference + {1300, 1600, 1900, 2200, 2450, 2700,
2900} MHz, interleaved low/high order, 90 s settle per point; per point a
131K warmup, 2x 523K + 2x 131K prefills (256-token outputs) and a
sustained C1 decode block (`benchmark-decode.py`, 4096 tokens x 3 runs).
A 2 Hz sampler logs power.draw / clocks.sm / temperature and run windows
are attributed exactly, so mean-window power and energy/token are
measured, not inferred from the cap. Both engines (EXL3, nvidia NVFP4),
same night, MTP3, YaRN 512K, image `8294c3c914c0`. Runner
`scripts/clock-sweep.py`, analysis `scripts/analyze-clock-sweep.py`,
receipts: [clock-sweep-20261007](../benchmarks/clock-sweep-20261007/).

TTFT medians; W = mean over the 523K prefill window; J/1k = energy per
1000 prefill tokens; decode = sustained short-prompt C1 (the core-suite
metric; the ladder's ~261 tok/s is a different, post-prefill measurement).

EXL3 (free-running reference: eff 2365 MHz):

| Lock | eff MHz | 131K s | 523K s | pf W | J/1k pf | dec tok/s | dec W |
|---|---|---|---|---|---|---|---|
| 1300 | 1297 | 20.8 | 89.4 | 317 | **54.1** | 95.7 | 228 |
| 1600 | 1590 | 17.7 | 75.8 | 375 | 54.4 | 106.4 | 256 |
| 1900 | 1877 | 15.3 | 65.7 | 452 | 56.7 | 115.8 | 289 |
| 2200 | 2107 | 14.1 | 60.2 | 512 | 58.9 | 123.2 | 322 |
| 2450 | 2250 | 13.6 | 57.7 | 542 | 59.8 | 127.0 | 344 |
| 2700 | 2331 | 13.5 | 56.5 | 581 | 62.7 | 129.8 | 403 |
| 2900 | 2318 | 13.6 | 57.2 | 581 | 63.4 | 132.4 | 513 |
| free | 2365 | 13.6 | 56.0 | 580 | 62.1 | 132.1 | 515 |

nvidia NVFP4 (free-running reference: eff 2616 MHz):

| Lock | eff MHz | 131K s | 523K s | pf W | J/1k pf | dec tok/s | dec W |
|---|---|---|---|---|---|---|---|
| 1300 | 1297 | 15.2 | 67.2 | 291 | **37.3** | 105.9 | 231 |
| 1600 | 1590 | 13.1 | 57.7 | 339 | 37.4 | 117.2 | 256 |
| 1900 | 1884 | 11.6 | 51.0 | 402 | 39.2 | 124.8 | 286 |
| 2200 | 2170 | 10.6 | 46.5 | 462 | 41.1 | 129.8 | 316 |
| 2450 | 2369 | 10.2 | 44.5 | 492 | 41.8 | 130.2 | 335 |
| 2700 | 2587 | 9.8 | 42.3 | 573 | 46.3 | 136.6 | 394 |
| 2900 | 2586 | 9.9 | 42.5 | 576 | 46.8 | 134.9 | 497 |
| free | 2616 | 9.8 | 42.1 | 575 | 46.3 | 136.0 | 493 |

Findings:

- **`-lgc` is a ceiling, not a pin.** Requests above the power-cooled
  ceiling throttle to it: the EXL3 ceiling is ~2300-2365 MHz (even the
  2200 lock averages 2107 under sustained prefill), while nvidia's FP4
  GEMMs draw less per watt of clock and hold ~2590. Locks at 2700/2900
  are indistinguishable from free-running, and 2450 is slightly *worse*
  on prefill (57.7 s vs 56.0 s) — locking near/above the ceiling trades
  DVFS's smooth clock choice for a boost-then-throttle oscillation.
- **TTFT tracks clock sub-linearly** (~clock^0.75-0.8 prefill; decode
  more memory-bound at ~clock^0.4-0.55 but decidedly *not* clock-blind).
- **Prefill energy optimum is a flat basin at 1300-1600 MHz**: EXL3
  54.1-54.4 J/1k (−12.8% vs free-running 62.1), nvidia 37.3-37.4
  (−19.3% vs 46.3). GreenLLM's mid-band optimum confirmed; the curve is
  a basin plus linear rise, not a sharp U.
- **Decode is where locks win outright**: at 1300-1600 MHz the card
  draws ~230-256 W instead of ~493-515 W free-running, for 72-86% of
  the speed — decode energy per token −38.8% (EXL3) / −40.0% (nvidia),
  exceeding the H200 study's ≤32%. Their lock-dominates-caps direction
  confirms on Blackwell workstation; their "decode flat above ~1590 MHz"
  does not — free-running decode here sustains 2365-2616 MHz at ~500 W
  (MoE expert GEMMs + MTP3 speculation are compute-bound enough to keep
  scaling with clock). The 2200 lock is the interactive compromise:
  −6.7%/−4.6% decode speed for −33%/−33% decode energy.
- **Lock vs cap frontier** (cap points from the same-day ladders; their
  measured draw is unknown but bounded above by the cap — free-running
  prefill draws ~96% of 600 W and hard caps hold within a few W of the
  limit): for nvidia locks dominate caps — lock1600 (57.7 s, 37.4 J)
  beats pl330 (62.9 s, ≤39.7 J) on both axes and lock2200 (46.5 s,
  41.1 J) is 6% faster than pl450 (49.7 s, ≤42.8 J) at equal-or-better
  energy. For EXL3 locks and caps converge within ~1 J/1k at matched
  operating points (lock1300 54.1 J vs pl330/pl300 corrected to
  ~54-56) — the tuning study's "shape of the limit doesn't matter"
  holds for trellis-heavy prefill, but with FP4 GEMMs the driver's DVFS
  becomes measurably wasteful and a clean lock recovers it.
- Community ~310 W "sweet spot" claims for this card land inside the
  1300-1600 MHz basin (291-375 W draw), and the "raise the limit, limit
  the clocks" recipe is exactly this sweep — confirmed.
- **Cross-quant headline**: nvidia locked at 1900 MHz prefill is *faster*
  than EXL3 free-running at full power (51.0 s @ 402 W vs 56.0 s @
  580 W); locked at 1600 MHz it stays within 3% of EXL3's unlocked
  prefill using 58% of the power (339 vs 580 W).
- Lock only at or below the ceiling: locked at 2900 (over the ceiling),
  sustained decode draws ~100 W more than at the 2700 lock for the same
  speed on both quants — the overdriven boost target costs voltage and
  buys nothing.
- Reference jitter: the morning free-running reference (57.4 s median)
  vs the evening sweep's (56.0 s) differ by 2.4% at identical config —
  treat single prefill legs as ±3%.

Operating points: batch/energy-first serving lock 1300-1600 MHz (prefill
−13/−19% J, decode −39/−40% J); interactive lock 2200 MHz (~5% energy
margin at ~7% speed cost, free-running decode power halved);
speed-first leave 600 W free-running (locks above 2450 do nothing).

## NVFP4 (RedHatAI) vs EXL3 comparison (2026-10-07)

A condensed three-way performance summary with HF card links lives in
[performance-comparison.md](performance-comparison.md); this section keeps
the full receipts.

Checkpoint: `RedHatAI/Qwen3.8-Flash-Next-NVFP4` @ `c8f2fb1b` — MoE expert
GEMMs only in NVFP4 (1x16 blocks, fp8e4m3 scales); attention, dense, GDN,
MTP heads, and the 102.5 GB PLE table stay BF16 (host-offloaded).
GPU-resident weights ~81 GB, fits the 96 GB card at 0.94 utilization with
room for the 512K YaRN KV cache.

Engine fix: v0.31's `Qwen4ExpPLEEmbeddingMethod.from_quant_config` raised
`NotImplementedError` for any non-FP8 quant config, blocking startup under
`CompressedTensorsConfig`. `patches/port-nvfp4-ple-ct.py` adds a branch
returning the unquantized PLE method for CompressedTensors configs (their
targets cover Linear projections only, so the PLE table is never in
scope). Image `8294c3c914c0`; EXL3 path re-verified after the rebuild.

512K YaRN: works — retrieval 12/12 (8K/131K/240K/480K filler x 3
positions) at both MTP levels.

MTP level: MTP3 (the recipe's recommendation) beats MTP2 — decode
+21..25%, TTFT -1.5..-2.3%.

Ladder, YaRN 512K, MTP3, TTFT s / 523K decode C1 tok/s. All three builds
measured 2026-10-07 (EXL3 330/300 W re-measured same day; 600 W reference
same day; 131K EXL3 330/300 W from the tuning study above; nvidia ladder
measured same day):

| Power | EXL3 131K | EXL3 523K | EXL3 decode | nvidia 131K | nvidia 523K | nvidia decode | RHA 131K | RHA 523K | RHA decode |
|---|---|---|---|---|---|---|---|---|---|
| 600 W | 13.1 s | 57.1 s | 261 | 9.5 s | 42.9 s | 261 | 9.7 s | 43.3 s | 262 |
| 450 W | 15.5 s | 69.1 s | 260 | 11.3 s | 49.7 s | 263 | 11.0 s | 49.6 s | 264 |
| 330 W | 21.1 s | 89.5 s | 244 | 14.4 s | 62.9 s | 259 | 13.9 s | 62.8 s | 251 |
| 300 W | 22.3 s | 97.8 s | 226 | 15.6 s | 68.4 s | 248 | 15.4 s | 68.8 s | 242 |

Findings: both NVFP4 builds beat EXL3 on prefill at every power level
(native FP4 expert GEMMs on Blackwell) — nvidia 131K TTFT 9.5 s vs EXL3
13.1 s at 600 W, and the gap widens at 523K (42.9 s vs 57.1 s). The two
NVFP4 builds are near-identical (within 0.5 s on TTFT at every level).
Decode is a wash at 600 W (~261 tok/s all three) but degrades least under
caps for nvidia (-5.0% 600->300 W), then RHA (-7.6%), then EXL3 (-13.4%).
nvidia at 300 W matches EXL3 at 450 W on prefill (68.4 s vs 69.1 s) — the
same speed for 150 W less. Receipts:
[nvfp4-comparison-20261007](../benchmarks/nvfp4-comparison-20261007/)
(nvidia ladder: `nvfp4-nvidia-ladder.jsonl`).

Core quality suite (both YaRN 512K, MTP3, 600 W, same day):

| Check | EXL3 | NVFP4 |
|---|---|---|
| api-tools | 16/16 | 16/16 |
| vision (1/4/16 images) | pass | pass |
| seven (21 timed runs) | 20/21 | 19/21 |
| orchid (exact x100 repetition) | 5/5 (100 every run) | 0/5 (750, 101, 750, 99, 110) |
| decode C1/C2/C4/C8/C16 tok/s | 125.0 / 225.3 / 382.5 / 583.5 / 871.7 | 128.3 / 222.0 / 362.2 / 586.9 / 601.8 |

The seven failures are all the documented fable word-count band (140..170;
EXL3 183 words once, NVFP4 176/179 words twice) — no other contract
failures on either side. Two NVFP4-specific findings: exact-repetition
quality regressed (orchid 0/5; two runs looped to the 1500-token cap,
750 = 1500/2 tokens per occurrence), and aggregate decode at C16 drops
31% (601.8 vs 871.7 tok/s) while C1-C8 stay within noise — the NVFP4
MoE path does not scale to high concurrency the way EXL3 does. Receipts:
[quality-nvfp4-vs-exl3-20261007](../benchmarks/quality-nvfp4-vs-exl3-20261007/).
*(Partly superseded: the nvidia core numbers quoted in this section were
pre-rebase dev13 receipts, and the NVFP4 orchid loops and the C16
comparison are replaced by § Protocol-clean core suites + 10-run tool-eval.)*

## Deep quality: EXL3 vs NVIDIA NVFP4 vs RedHatAI NVFP4 (2026-10-07)

A condensed summary with HF card links lives in
[quality-comparison.md](quality-comparison.md); this section keeps the
full receipts.

The deep-quality comparison covers both NVFP4 builds. The standard
`nvidia/Qwen3.8-Flash-Next-NVFP4` checkpoint (132.7 GB, ModelOpt
MIXED_PRECISION: NVFP4 experts on 48 layers, FP8 block-128 MTP experts,
FP8 per-tensor PLE n-gram table in a dedicated shard) loads through the
stock `ModelOptMixedPrecisionConfig` path — the FP8 PLE resolves via
`quantized_layers` directly to `Qwen4ExpPLEFp8EmbeddingMethod` — so no
engine patch or image rebuild was needed. The RedHatAI CompressedTensors
build (174 GB; NVFP4 experts only, 102.5 GB BF16 PLE host-offloaded)
loads through the `port-nvfp4-ple-ct.py` fix; the `nvfp4` profile was
temporarily pointed at it for the leg (core-suite convention) and
restored afterwards. The nvidia profile pin was re-pinned from `2061e0b0`
(unreachable after the repo's super-squash; content verified identical)
to `fc694b54`.

GSM8K (1319, 5-shot) and IFEval (541) via lm_eval 0.4.13
`local-chat-completions` + `--apply_chat_template` (server-side Qwen3
template, reasoning parser strips thinking), 16 concurrent requests,
`max_gen_toks=3072`, thinking on, temperature 0, offline. All legs:
YaRN 512K, MTP3, 600 W, same image, same day; 10-problem GSM8K smoke
10/10 on both NVFP4 legs before the battery.

| Suite | Metric | EXL3 | NVFP4 (nvidia) | NVFP4 (RedHatAI) |
|---|---|---|---|---|
| GSM8K | flexible-extract | 0.91964 | 0.89538 | 0.91964 |
| GSM8K | strict-match | 0.91812 | 0.89310 | 0.91888 |
| IFEval | prompt strict | 0.79852 | 0.79113 | 0.78743 |
| IFEval | inst strict | 0.80576 | 0.79856 | 0.79257 |
| IFEval | prompt loose | 0.81885 | 0.81331 | 0.80407 |
| IFEval | inst loose | 0.81894 | 0.81295 | 0.80336 |

GSM8K correct counts (flexible/strict): EXL3 1213/1211, nvidia 1181/1178,
RedHatAI 1213/1212. Per-problem (flexible): EXL3 and RedHatAI agree on
1217/1319 with a symmetric 51/51 swap — identical totals, different error
sets (~102 problems). nvidia's gap is a net loss, not just different
errors: 70 problems EXL3 gets right that nvidia misses, 38 the other way
(net -32, outside the ~0.8 pt stderr). All three correct on 1110
(84.1%); none correct on 49 (3.7%).

Paired statistics (exact McNemar on the per-item files, so item
difficulty cancels): the nvidia GSM8K deficit is real — -2.43/-2.50
pts, discordant 38/70, p=0.003 — while EXL3 vs RedHatAI is a dead tie
(51/51 flexible swap, p=1.0). On IFEval all twelve pairwise metric
comparisons tie (gaps 0.4-1.6 pts, paired CI ±2.0-2.7 pts, p≥0.22).

Engine jitter was measured with an EXL3 repeat leg the same evening
(same engine instance, no restarts, identical recipe): GSM8K flips
41-44/1319 items per run (per-run sd ~0.35 pt); IFEval flips 6.3% of
prompts / 7.1% of instructions (per-run sd ~0.65-0.76 pt). The repeat
moved EXL3 GSM8K -0.4 pts (91.58/91.36) and IFEval +0.7 to +1.8 pts
(prompt strict 80.59) — the two single legs sat on opposite edges of
their bands, so r2's IFEval lead over the NVFP4 builds actually grew
(+1.5/+1.9 prompt strict vs nvidia/RedHatAI).

Findings: EXL3 and RedHatAI are statistically tied on GSM8K; nvidia
NVFP4 is genuinely lower (-2.4 pts, ~7× the measured per-run jitter).
On IFEval EXL3 leads both NVFP4 builds (0.6-1.9 pts across the two
runs) — directionally consistent but never significant; single-run
IFEval gaps under ~2 pts are noise at 541 items. The two NVFP4 builds
trade differently: nvidia is the fastest (GSM8K 445 s / IFEval 673 s
vs EXL3 492/689) but loses GSM8K quality; RedHatAI keeps EXL3-level
GSM8K quality but is the slowest (550/862 s, +24%/+28% vs nvidia) —
consistent with its 2× larger PLE table (102.5 GB BF16 vs 51.2 GB FP8
in pinned host). The direction matches the core suite's NVFP4
regressions (orchid exact repetition, seven) on both builds. Receipts:
`paired-analysis.txt` and the EXL3 `gsm8k-r2-results.json` /
`ifeval-r2-results.json` in
[quality-deep-20261007](../benchmarks/quality-deep-20261007/); analysis
code `scripts/analyze-paired-quality.py`.

Tool-eval-bench (88 cases, 19 Hard Mode, thinking on, temperature 0,
parallel 8, pinned `cf54b4b` v2.6.1.dev45) completes the three-way
quality picture. The harness is deterministic (fixed scenarios,
deterministic mocks/noise/evaluators), but vLLM at temperature 0 is not
reproducible run-to-run under parallel 8, so each quant was run 5 times
on the identical release config (v0.31.0 image, MTP3, YaRN 512K, 600 W,
2026-10-07, same engine instance per quant, no restarts between
repeats). The earlier single-run records — nvidia 155, EXL3 152, RHA
149 — were individual draws from this jitter (nvidia's 155 sat 6.6 pts
above its 5-run mean; EXL3's 152, 4.8 above); the 400 W dev9 EXL3
record (145) is excluded as a different engine/config.

| Quant | 5 runs (pts/176) | Mean | 95% CI | Hard Mode (of 38) |
|---|---|---|---|---|
| EXL3 | 152 145 148 147 144 | 147.2 (84/100) | ±3.9 | 28.2 (±2.0) |
| NVFP4 (nvidia) | 150 149 144 148 151 | 148.4 (84/100) | ±3.4 | 28.6 (±1.9) |
| NVFP4 (RedHatAI) | 147 150 151 151 150 | 149.8 (85/100) | ±2.0 | 29.4 (±3.2) |

No pairwise difference is significant: Welch t on the 5-run totals gives
nvidia vs EXL3 +1.2 pts (p=0.53), nvidia vs RedHatAI −1.4 (p=0.36),
EXL3 vs RedHatAI −2.6 (p=0.15); Holm-corrected, all three pairs are
tied. Per-scenario (n=88, paired on 5-run means) agrees: mean gaps
≤0.03 pts/scenario, 55-60 of 88 scenarios tied, p≥0.31. 22-28 of 88
scenarios flip verdict across the 5 repeats per quant (the borderline
set), and Hard Mode means (28.2/28.6/29.4 of 38) are likewise within
noise.

Findings: on tool/agentic quality the three quants are statistically
indistinguishable — the single-run impression that "NVFP4 leads EXL3"
(and nvidia's 34/38 Hard Mode) was run-to-run variance, not a quant
effect. RedHatAI happens to have the highest mean and the tightest
spread (±2.0) but that too is within noise. The honest statement:
EXL3 ≈ nvidia ≈ RedHatAI on tool quality at 84-85/100; the observed
2-3 pt gaps would need ~20 runs per quant to resolve. Receipts:
[tool-eval-repeats-20261007](../benchmarks/tool-eval-repeats-20261007/)
(5 tools.json + tools.md per quant with per-run traces, plus
analysis.txt); single-run records in
[quality-deep-20261007](../benchmarks/quality-deep-20261007/); EXL3 dev9
record in [exl3-dev9-graph-tools](../benchmarks/exl3-dev9-graph-tools.md).

## exl3-ple8: the FP8-PLE variant (2026-10-07)

To find out whether NVIDIA's GSM8K deficit could be explained by its FP8
PLE table alone, the `exl3-ple8` profile changes only that axis:
identical K4.25 EXL3 trellis experts and calibration pipeline as the
live EXL3 checkpoint, with the PLE n-gram table materialized in FP8
(~48 GiB payload vs ~95 GiB BF16). Checkpoint
`wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-PLE-FP8-v1` @ `888306bd`
(128.3 GB). (MTP-expert format can't be a confound: spec-decode
verification is lossless.)

This was the profile's first boot under the v0.31.0 image; it came up
clean (71.9 GiB GPU allocation, static MTP3 capture, retrieval 6/6 at
8K/240K). Full protocol-match with the three-way legs (image
`8294c3c914c0`, MTP3, YaRN 524288, 600 W, temp 0, thinking on,
16-way concurrency).

**GSM8K — the FP8 table does not reproduce NVIDIA's deficit.** ple8 scored
0.9196 and 0.9242 (flexible-extract; mean **0.9219**, above EXL3's 0.9177
mean and RHA's 0.9196), not near NVIDIA's 0.8954. Paired exact McNemar:

| Pair | Discordant | p |
|---|---|---|
| ple8-r1 vs EXL3-r1 | 27/27 | 1.0000 |
| ple8-r2 vs EXL3-r2 | 40/29 | 0.2284 |
| ple8 (both runs) vs nvidia | 74/42, 70/32 | **0.0038, 0.0002** |
| ple8 vs RedHatAI | 53/53, 51/45 | 1.0000, 0.6101 |

FP8 PLE on trellis experts does not reproduce the deficit: ple8 ties
EXL3 and beats nvidia significantly. The three-way pattern plus this
single-variable comparison puts the GSM8K regression on NVIDIA's
**ModelOpt NVFP4 expert pipeline**, not the PLE format (RHA's
CT-quantized NVFP4 experts with BF16 PLE also tie EXL3).

**IFEval — ties.** ple8 means 0.8124 prompt-strict / 0.8172 inst-strict /
0.8345 prompt-loose / 0.8321 inst-loose (best of the four on all four
metrics, within noise). All six ple8-vs-EXL3 pairings tie (p≥0.11);
the ple8-r1 vs nvidia/RHA pairings hit p=0.02-0.04 but r2 pairings all
tie (p≥0.05) — consistent with the established IFEval noise floor
(single-run gaps <2 pts are not resolvable at n=541).

**Core suite** (same protocol): api-tools 16/16, vision pass, retrieval
6/6, seven 18/21 (all fable-band, same failure mode as the others),
orchid **4/5** (occurrences 100,100,99,100,100 — the miss is off-by-one,
not a loop). Decode C1–C16 120.8/210.0/356.9/595.2/861.0 — a wash with
EXL3's fresh leg. So on exact repetition the FP8 PLE table costs ~1 run
(4/5 vs EXL3's 5/5) while NVFP4 experts cost more (nvidia 3/5, RHA 1/5):
the PLE format has a small, real effect on the repetition axis only.

**Tool-eval-bench:** 5 runs at 148.4 pts mean, then extended to 10 with
the other quants (next section) — ple8 is the top mean (149.7) but
statistically tied with all three.

**Eval-leg wall time:** GSM8K 500 s / IFEval 699 s vs EXL3's 492/689 —
changing only the PLE format on the EXL3 path did not move wall time,
which refines the earlier two-factor attribution of RHA's eval-leg lag
(see performance-comparison.md § End-to-end eval legs).

Finding: the FP8 PLE table is not what lowers GSM8K — swapping only the
PLE format on EXL3 experts kept EXL3-level quality (ple8 0.9219, ≥ the
0.915 threshold set before the run). NVIDIA's deficit against RedHatAI
reproduces on the expert-quantization axis, not the PLE table; FP8 PLE's
own measurable cost is one off-by-one orchid miss. Receipts:
[ple8-quality-20261007](../benchmarks/ple8-quality-20261007/) (core
suite, GSM8K×2 + IFEval×2 with per-item samples, tools run-001..005).

## Protocol-clean core suites + 10-run tool-eval (2026-10-07)

Two fixes applied the same night:

1. **Pre-rebase core-suite results are discarded.** The nvidia core numbers
   in the earlier three-way tables came from the Sept-29 dev13 receipts
   (`benchmarks/nvfp4-final/`) — NVIDIA's NVFP4 build, but on the
   pre-rebase v0.30-era engine with the pre-re-pin checkpoint revision
   (`2061e0b0`, superseded by `fc694b54`), under the generic `nvfp4`
   profile name with no served-model-name stamp in the receipts. Fresh
   core suites were run for **all four quants** under the current
   protocol with model names stamped in every file.
2. **Tool-eval repeats doubled from 5 to 10 per quant** (40 runs total)
   to tighten the CIs.

Core suite (fresh, model-name-stamped, YaRN 512K, MTP3, 600 W, temp 0):

| Check | EXL3 | nvidia NVFP4 | RedHatAI NVFP4 | exl3-ple8 |
|---|---|---|---|---|
| api-tools | 16/16 | 16/16 | 16/16 | 16/16 |
| vision (1/4/16) | pass | pass | pass | pass |
| seven (21 timed) | 20/21 | 18/21 | 19/21 | 18/21 |
| orchid (exact ×100) | **5/5** | 3/5 | 1/5 | 4/5 |
| decode C1/C2/C4/C8/C16 | 127.5/219.8/359.5/592.2/**855.7** | 133.7/239.8/415.9/637.1/**992.3** | 123.6/214.7/383.9/574.7/**594.5** | 120.8/210.0/356.9/595.2/**861.0** |

All seven failures on every quant are the documented fable word-count band
(140–170 words). The orchid picture replaces the pre-rebase rows: every
NVFP4-quantized build loses exact repetition to some degree (nvidia 3/5,
RHA 1/5), the FP8-PLE variant loses one run (4/5). The miss mode in the
clean legs is off-by-one counts (99–102); the 1500-token runaway loops of
the earlier RHA leg (and the dev13-era nvidia record) did not recur in
any of the four clean legs, so loop-to-cap is treated as a rare
engine-jitter mode rather than a deterministic quant regression —
consistent with GSM8K-scale flip rates at temp 0. C16 decode lead
flips attribution cleanly: nvidia 992.3 > ple8 861.0 ≈ EXL3 855.7 >>
RHA 594.5.

Tool-eval-bench, 10 runs/quant (mean of 176; 95% t-CI):

| Quant | Mean | sd | 95% CI | Hard Mode /38 |
|---|---|---|---|---|
| EXL3 | 146.8 | 2.70 | [144.9, 148.7] | 28.1 |
| nvidia NVFP4 | 148.0 | 3.53 | [145.5, 150.5] | 28.9 |
| RedHatAI NVFP4 | 149.4 | 2.95 | [147.3, 151.5] | 30.2 |
| exl3-ple8 | 149.7 | 2.79 | [147.7, 151.7] | 30.7 |

Welch t + Holm over all six pairs: **every pair ties** (min raw
p=0.030 for EXL3 vs ple8, Holm-adjusted p=0.178). At 10 runs the
resolvable gap is ~2.5 pts; observed spreads are ≤2.9 pts, so the
honest statement is a four-way tie at 83–85/100 with ple8 and RedHatAI
directionally (not significantly) ahead. The old "2-3 pt gaps need ~20
runs" note above was right — 10 runs per quant halved the CIs and the
gaps stayed inside them.

Receipts: [core-suite-20261007](../benchmarks/core-suite-20261007/)
(exl3/rha/nvfp4 core suites with served-model-name stamps),
[tool-eval-repeats-20261007](../benchmarks/tool-eval-repeats-20261007/)
(`{exl3,rha,nvfp4,ple8}/run-01..10` with traces + analysis).

## v0.32.0 rebase (2026-10-18)

Runtime base moved to `vllm/vllm-openai:v0.32.0` (tag digest `befccf0e…`),
branch `v0.32-rebase`, image `72f854abdf83`
(`localhost/qwen38-rtx:v0.32-test`). The host gained a second RTX PRO 6000
since the Oct-7 flight; every leg here ran on **GPU index 1** (600 W via
`sudo nvidia-smi -pl 600`; driver 615.78.08), GPU 0 reserved. Port changes:

- mmap PLE backport re-anchored onto v0.32.0 from PR 58439 head `047fc1c`
  (main merged at `4e4eab7`); the PR's `vllm/` diff applies byte-clean —
  all four base files verified identical between the PR parent, the git tag
  and the installed image tree (`ple-mmap-base-hashes.json`).
- EXL3 `is_fused` port anchor updated for upstream's new `named_expert`
  condition, preserving both gates.
- `port-nvfp4-ple-ct.py` dropped: v0.32 routes CompressedTensors/INC/Quark/
  ModelOpt-mixed-precision PLE layers natively, with per-layer scheme
  scoping.
- Structured-output canary re-anchored from `_get_constraint_start` to the
  `_get_constraint_bounds`/`ConstraintBounds` refactor; 7/7 before rebuild.
- **`port-mrope-positions-factor.py` added — v0.32.0 regression found.**
  Upstream 59853 sizes the M-RoPE cos/sin cache at `max_position_embeddings×1`
  when multimodal pruning is off; the Qwen4Exp path indexes deeper, so any
  prompt past the checkpoint-native 262144 hard-faults the worker (illegal
  memory access on the first 2048-token chunk past the boundary; observed
  under both GDN prefill backends — the reporting kernel varies because CUDA
  errors are async). Bisection receipts: 260067-token probe passes, 280069
  kills the engine; restoring the v0.31 `×4` slack passes 280069 and 480071.
  The port restores v0.31 sizing. Reported upstream as
  [vllm-project/vllm#62383](https://github.com/vllm-project/vllm/issues/62383)
  (filed independently with an H200 TP2+EP YaRN-4.0 repro and an
  MTP-cudagraphs factorial bisect; the crash-step fingerprint
  `num_computed_tokens=[262400]` matches ours exactly). Our boundary/Xid
  evidence and the one-line-revert datapoint are queued there as a comment.
  The reporter's RoPE-cache rule-out rests on an unverified "positions_factor
  resolves to 4" assumption; our stock-image test shows the opposite.

The `nvfp4-redhatai` profile was **retired** in this rebase (C16 scaling
collapse de-recommended it — see the repeat-block receipts in
`benchmarks/c16-repeat-20261007/` — the checkpoint was deleted, and its
startup port is now upstream). Oct-7 four-way receipts and comparison rows
remain the historical record; the Oct-18 flight skipped its suite.

### Per-profile re-qualification (GPU 1, YaRN 512K, MTP3, 600 W)

Receipts: `benchmarks/v032-rebase-20261018/{exl3,exl3-ple8,nvfp4}-core/`,
`exl3-longctx-gauntlet/`.

| Check | exl3 | exl3-ple8 | nvfp4 | Oct-7 v0.31 reference |
|---|---|---|---|---|
| Retrieval (incl. 480K for exl3) | 12/12 | 6/6 | 6/6 | 12/12 / 6/6 / 6/6 |
| API tool constraints | 16/16 | 16/16 | 16/16 | 16/16 all |
| Vision | pass | pass | pass | pass all |
| Structured-output live | 29/29 | 29/29 | 29/29 | (canary passed at build) |
| Seven contracts | 19/21 | 18/21 | 19/21 | 20/18/–/18 (fable-band class) |
| Orchid | 4/5 | 5/5 | 3/5 | 5/4/3/1 — all misses off-by-one class |
| Tool-eval-bench (1 run) | 146 (30 HM) | 147 (32 HM) | 147 (30 HM) | 10-run means 146.8/149.7/148.0, sd ≤3.5 |

Every step exited 0; all scores land within the Oct-7 replication bands.

### Same-day A/B versus v0.31 (GPU 1, 600 W, interleaved by quant)

Receipts: `benchmarks/v032-rebase-20261018/ab-v031-vs-v032/`
(v0.31 baseline image re-tagged `qwen38-rtx:v0.31-20261014`).

| Quant | C1–C16 decode (v0.32 vs v0.31) | TTFT 131K / 523K | GSM8K |
|---|---|---|---|
| exl3 | +5.1 / −2.1 / +1.8 / −0.3 / +2.8 % | 12.9 / 57.8 vs 13.0 / 57.9 s | 0.9242 vs 0.9181 |
| nvfp4 | +2.2 / −1.5 / +0.6 / −1.0 / −2.6 % | 9.8 / 43.7 vs 9.7 / 43.3 s | 0.8976 vs 0.8976 |
| exl3-ple8 | +3.4 / −1.2 / −2.2 / −4.0 / +1.2 % | 13.0 / 57.7 vs 13.0 / 57.9 s | — |

**Parity, no regression.** Every decode point is inside the run-to-run
jitter documented on Oct 7; TTFT matches within 0.4 s. The headline
QSA-GEMM speedups in the v0.32 release notes (1.45–1.68× on the merged
projection GEMM) do not surface end-to-end at these depths — prefill time
here is dominated by other work, so the NVFP4 prefill lead stands.
nvfp4's GSM8K deficit is **unchanged** under the new ModelOpt
mixed-precision detection (identical 0.8976): the −2.4 pt gap belongs to
the expert pipeline itself, not to the loader fix.

### TP2+EP re-check on v0.32 (2026-10-19, 2x300 W)

Same protocol as the Oct-13 TP2 qualification (GPU entities 0+1, `TP_SIZE=2`
implying expert parallel, port 8125, MTP3, YaRN 512K, clients mode,
3-run decode + 5-depth prefill), now on the v0.32.0 image. Receipts:
`benchmarks/v032-rebase-20261018/tp2/`. This was the **first-ever nvfp4
TP2 boot** on this host; it started clean under EP.

| TP2+EP @ 2x300 W | C1 | C2 | C4 | C8 | C16 | TTFT 128K |
|---|---|---|---|---|---|---|
| exl3 v0.32 | 180.1 | 292.7 | 453.3 | 735.4 | 981.9 | 14.9 s |
| nvfp4 v0.32 | **184.4** | 306.0 | 465.9 | 773.5 | **1223.4** | 11.2 s |
| exl3 v0.31 (Oct-13 ref) | 172.7 | 279.9 | ~458 | — | — | — |

- exl3 v0.32 vs v0.31 TP2: +4.3% C1, parity elsewhere — same story as the
  TP1 A/B.
- nvfp4 TP2 sets recipe-best decode at every concurrency: C1 184.4 beats
  the single-card 600 W best (133.7) by +38%; C16 1223.4 beats the prior
  best (1017.2) by +20% at equal total power.
- Prefill stays power-bound, as Oct 13 showed: TP2 at 2x300 W does not beat
  single-card 600 W (128K: 11.2 s vs 9.8 s on v0.32 TP1).
- Plain TP (EP off) for nvfp4: **tested 2026-10-19, it loads and serves**
  (`ENABLE_EXPERT_PARALLEL=0 TP_SIZE=2` boots in 3 min, smoke passes, zero
  weight rejections) - the Oct-13 "TP implies EP" default was justified by
  EXL3's block-128 wall only; nvfp4's block-16 experts slice 640->320
  cleanly and even the FP8 block-128 MTP draft experts load. Performance vs
  the EP run above, same day (`nvfp4-tp2-plain-*` receipts): C1 179.9
  (-2.4%), C2 301.5 (-1.5%), C4 471.9 (+1.3%), C8 800.5 (+3.5%), C16 1228.8
  (+0.4%); TTFT within 3% at every depth. All inside the Oct-7 jitter
  bands: EP costs nothing for nvfp4, so the safe EXL3-driven default stays.

## The real fix: served-window cache floor, PR 62409 (2026-10-19)

The 2026-10-18 diagnosis in the section above ("the Qwen4Exp path indexes
deeper than the token position") was wrong in its mechanism, though right
that the cache size was the lever. Instrumented ctor stacks (print of
`cache_max_position_num` + construction stack at every
`MRotaryEmbedding.__init__`) on the stock v0.32.0 image show the true
chain: `get_rope()`'s yarn branch **discards the caller's `max_position`
and passes `original_max_position_embeddings` — the PRE-scaling window —
as the class base**, and `MRotaryEmbedding` never multiplies it by the
YaRN scaling factor. The blanket 4x (Qwen2.5-VL era, `bf3b79efb`
2025-02-05) had been covering that gap for any factor <= 4; PR 59853
(`a98247ab4`, first release v0.32.0) removed the cover. Three proofs the
model-side story was wrong: (a) sizing the cache at the `qsa.py`
`get_rope` call site from `max_model_len` is a provable NO-OP — the ctor
still printed 262144 (the factory overrides it); (b) `--hf-overrides`
forcing `max_position_embeddings` cannot move the base at either nesting
level (the YaRN serving window is *derived* = native x factor, per
`ModelConfig`; the top-level form fails boot validation outright);
(c) flooring only `vllm_config.model_config` fixes the TARGET instance
but leaves the MTP drafter at 262144 — still crashes, because at
draft-build time the current config resolves to the draft's own
ModelConfig. The faulting consumer is the drafter's cos/sin table under
graph replay, matching #62383's MTP x cudagraph factorization (H200)
exactly; host `dmesg` across all our faults: Xid 31 FAULT_PDE
ACCESS_TYPE_VIRT_READ.

Fix filed upstream by us: **PR
[#62409](https://github.com/vllm-project/vllm/pull/62409)** (base
`main`, commit `2c2c57288`):
`cache_max_position_num = max(base * positions_factor, target
max_model_len)`, the target resolution mirroring 59853's own
`mrope_positions_factor()`. `Fixes #62383`. Native-window deployments
are bit-identical to stock (verified: single shared instance, 262144
rows, no overrides). The independent #62383 reporter had ruled out the
rope cache on un-verified grounds; our ctor prints supersede that.

Recipe changes: `patches/port-mrope-served-window.py` carries the exact
62409 hunk and **replaces** the blunt `port-mrope-positions-factor.py`
(x4 workaround), which is deleted. Verified inside the candidate image:
`qwen3_vl.py` back at stock `return 4 if pruned else 1`, zero workaround
markers, mrope.py floor present. Image
`qwen38-rtx:v032f-candidate` (`abbde425a717`), built 2026-10-19 from
prod + this port.

TP=1 re-qualification, GPU 1 @ **600 W** (host idle-power setting for
this stage; receipts `benchmarks/v032f-pr62409-20261019/tp1/`). New in
every leg: the **boundary retrieval leg** (280K + 480K x 3 positions
each) that unpatched v0.32.0 has NEVER survived anywhere, and that all
three profiles pass with needle hits:

| profile | test-quant-profiles | api | vision | retr 8K/240K | boundary 280K/480K | structured | seven | orchid | tools |
|---|---|---|---|---|---|---|---|---|---|
| exl3 | exit 0 | 16/16 | pass | 6/6 | **6/6** | pass | 19/21 (159.5 wtps) | 5/5 | 143/176 |
| exl3-ple8 | " | 16/16 | pass | 6/6 | **6/6** | pass | 19/21 (161.3) | 5/5 | 147/176 |
| nvfp4 | " | 16/16 | pass | 6/6 | **6/6** | pass | 18/21 (162.1) | 4/5 | 148/176 |

seven/orchid counts match the Oct-18 (300 W) bands even at the higher
power cap; nvfp4's 18/21 + 4/5 is its standing jitter class.

TP2 re-qualification, both GPUs @ **300 W**, MTP3, EP default-on, port
8125 (receipts `.../tp2/`). Same suite, same boundary legs — the first
TP2 qualification ever run PAST the 262144 boundary (Oct-13/18 TP2
testing predates the boundary legs):

| profile | api | vision | retr 8K/240K | boundary 280K/480K | structured | seven | orchid | tools |
|---|---|---|---|---|---|---|---|---|
| exl3 | 16/16 | pass | 6/6 | **6/6** | pass | 19/21 (219.3 wtps) | 5/5 | 152/176 |
| exl3-ple8 | 16/16 | pass | 6/6 | **6/6** | pass | 19/21 (224.1) | 5/5 | 143/176 |
| nvfp4 | 16/16 | pass | 6/6 | **6/6** | pass | 17/21 (221.3) | 1/5 | 151/176 |

`exl3-ple8` at TP2 is first-ever territory: boots and full-passes.
`nvfp4` TP2 orchid 1/5 is the one watch item (nvfp4 was already the
weakest orchid profile at TP1, 3/5 on Oct-18; its boundary/tools legs at
TP2 are strong) — recorded, not gating; re-check on next nvfp4 touch.
Decode/throughput ladders deliberately not re-run: this section changes
only cache sizing, verified parity-class behaviorally by the suites;
perf claims would need the same-card ladder protocol.

Promotion: `:local` = v032f image; rollback tags `v0.32-20261018` and
`v0.31-20261014` unchanged. When (if) 62409 merges, the port retires
with the usual "remove at next rebase" rule.

### GPU memory headroom ladder on `gg` (phase 1 2026-10-21, phase 2 2026-10-23, TP=1, one card)

The question is the one `0.94` has always dodged: the launcher sets
`GPU_MEMORY_UTILIZATION=0.94`, and the engine's own boot line says that
under-fills the card — on the `exl3` boot below it prints
`--kv-cache-memory=20258575360` (18.87 GiB) as the fill-the-card figure against
the 14.13 GiB it was given. Two phases on `gg` (125.68 GiB RAM, one RTX PRO
6000 Blackwell 96 GB): `scripts/util-ladder.sh` boots each util and records pool
size and boot margin; `scripts/util-stress.sh` then runs the four
memory-riskiest runtime shapes — 16-image vision batch, structured output, 240K
deep prefill, 16-way concurrency — under a 2 s VRAM sampler and records the peak.

| profile | image | util | KV pool | KV tokens | boot free | stress peak | final free | OOM lines |
|---|---|---|---|---|---|---|---|---|
| exl3-ple8 | `:utiltest` | 0.94 | 14.55 GiB | 977,726 | 5,254 MiB | — | — | — |
| exl3-ple8 | | 0.95 | 15.50 | 1,040,074 | 4,312 | 93,040 MiB | 4,302 | 0 |
| exl3-ple8 | | 0.96 | 16.45 | 1,105,255 | 3,312 | 94,022 | 3,320 | 0 |
| exl3-ple8 | | 0.97 | 17.41 | 1,167,603 | 2,372 | 94,982 | 2,360 | 0 |
| exl3-ple8 | | 0.98 | 18.35 | 1,232,785 | 1,368 | 95,966 | 1,376 | 0 |
| exl3 | `:local` | 0.94 | 14.13 | 949,386 | 5,362 | — | — | — |
| exl3 | | 0.98 | 17.94 | 1,204,445 | 1,476 | 95,868 | 1,474 | 0 |

- **Nine boots, five stress cells, zero OOM or CUDA-error lines**, every risk
  shape passing, and the 240K deep-prefill needle hash `574f8fae2625a5f178dd`
  identical across all of them — and across the same leg run on `burst` at TP2
  the day before, a different host, topology and PLE width.
- **The pool is the margin; runtime growth adds nothing to it.** Final free
  after the stress shapes matches boot free within 8 MiB in every cell (0.98:
  1,368 → 1,376 for ple8, 1,476 → 1,474 for exl3). So the util number can be
  read directly as the headroom you keep, which is what makes the ladder
  extrapolable instead of anecdotal.
- **Host headroom does not move with util.** `exl3` pins 95.368 GiB at load and
  MemAvailable reads 16.7 GiB at 0.94 and 17.2 GiB at 0.98. On the BF16 profile
  the host is decided by the pin, not by the KV pool: the reload-after-reboot
  risk this host carries is page cache against a 179.4 GB checkpoint, and
  `GPU_MEMORY_UTILIZATION` is not part of that arithmetic.
- **Profile parity, with a caveat.** exl3's pool at 0.98 lands 0.41 GiB under
  ple8's, consistent with its 0.24 GiB heavier weight load (72.25 vs 72.01 GiB)
  plus cross-boot spread — but the two profiles were measured on different
  images (`:local` = `73d126ef5a75`, `:utiltest` = `2971cbb9dea3`), so this is
  parity, not a measured profile penalty.

Decision: `GPU_MEMORY_UTILIZATION` stays at 0.94. The measurement says 0.98 is
safe — +26% KV tokens on both widths with no new failure mode — but shipping it
is a serving-policy call, and 1.4 GiB of boot margin leaves little for a second
tenant or a larger graph budget on a host that also has to reload a cold
179 GB checkpoint. Recorded as an option, not taken.

Two driver fixes found by running them. `util-stress.sh` ended its concurrency
burst with a bare `wait`, which also waits on the sampler subshell — a
900-iteration, 30-minute cap — so the trailing `kill $samp` was unreachable and
every cell idled out the sampler window with the engine already finished (the
0.98 cell completed its work at 19:11 and recorded at 19:39). Both drivers were
hardcoded to `exl3-ple8`, which was right when that was the only profile `gg`
could pin at all and is not right now that `PLE_PIN_STRATEGY=exact` puts the
BF16 table at 95.368 GiB instead of round2's 128 GiB, so they take
`QUANT`/`IMAGE`/`PORT`/`OUT`/`UTILS` with the old values as defaults; `OUT` must
stay one directory per profile because the stress phase reads its cell list from
that directory's `results.tsv`. The ladder also records MemAvailable and the
loader's pinned size per cell now, which is the only column that says anything
about the BF16 profile's actual constraint.

Receipts:
[gg-util-ladder-ple8-20261023](../benchmarks/gg-util-ladder-ple8-20261023/),
[gg-util-ladder-exl3-20261023](../benchmarks/gg-util-ladder-exl3-20261023/) —
TSV, probe/stress JSON and sampler output. `*.log` stays out of the tree per
`.gitignore` (the engine logs behind the residency findings below are the one
exception, kept deliberately: they are in
`gg-deep-concurrency-20261024/mechanism/`).

#### Deep-arrival residency: 0.96 is worse than 0.94, and pages are why

Pool tokens divided by request length is not a residency guarantee. Four
concurrent 245,248-token prompts each generating 16,384 tokens demand
1,046,528 resident tokens, so by that arithmetic 0.96 (1,076,915) fits and 0.94
(949,386) does not. Measured, 0.96 is 1.7× *slower* than 0.94. `exl3` on `gg`,
one card, YaRN 524288, MTP3, FP8 KV, four sequences per cell:

| util | pool tokens | = 3200-token pages | attention pages for 4 × 261,632 | evictions | wall |
|---|---|---|---|---|---|
| 0.94 | 949,386 | 296 | 328 | 1 | 366 s |
| 0.95 | 1,011,734 | 316 | 328 | 1 | 375 s |
| 0.955 | 1,045,742 | 326 | 328 | 1 | 381 s |
| 0.96 | 1,076,915 | 336 | 328 | **7** | **618 s** |
| 0.965 | 1,108,089 | 346 | 328 | **10** | **722 s** |
| 0.97 | 1,139,263 | 356 | 328 | 0 | 282 s |
| 0.98 | 1,204,445 | 376 | 328 | 0 | 285 s |

Reproduced, not luck: 0.96 evicts seven times in four independent samples (walls
618/617/621/615 s) and it is always the same arrival, evicted, re-prefilled, and
returning at 37–40 tok/s while its peers run 86–104.

The cause is in the boot log. This model is hybrid — 36 of 48 layers are GDN
linear attention — and the KV manager equalizes the two page shapes:

```
Setting attention block size to 3200 tokens to ensure that attention page size is >= mamba page size.
Padding mamba page size by 0.25% to ensure that mamba page size and attention page size are exactly equal.
kv lcm block sizes 3200
```

Residency is therefore counted in 3200-token pages, not tokens, and two things
follow. First, the token arithmetic misjudges even the direction: 0.955 is 786
tokens "short" of demand but 2 whole pages short, so it behaves exactly like
0.94 — the engine admits three sequences, parks the fourth in the wait queue
(peak_waiting 2), and queueing is nearly free: one eviction, 366 s. Second, a
sequence occupies pages for its per-layer state as well as its attention, which
is why 0.96 can hold the 328 attention pages four sequences need and still
thrash. Bounding the extra per-sequence cost from the observed clean/dirty
boundary (346 pages thrash, 356 clean) gives 5–7 pages, i.e. 16,000–22,400
tokens of state per sequence.

That estimate does not close across every shape, and the discrepancy is on the
record: the two-sequence 523,776-token cell runs clean at 0.96's 336 pages, which
implies about 4 state pages per sequence rather than 5–7. The likeliest reason is
that eviction depends on peak *co-residency*, not on the sum of final lengths —
with serialized deep prefill (42 s per 245K, 89 s per 524K at
`max_num_batched_tokens=2048`) the first sequence can finish generating before its
peers reach full length, so the 512K × 2 cell may never demand 342 pages at once.
So: page quantization is measured and certain, the per-sequence state allowance is
bracketed but not pinned, and any capacity plan built on `pool_tokens ÷
request_length` is built on the wrong unit. The engine's own printed
`Maximum concurrency for 524,288 tokens per request: 2.05x` is exactly that ratio,
and it overstates residency on this architecture.

What the concurrency grid says overall, `short-output/`, `residency/`,
`repeat-245k/`, `band-245k/`, `mechanism/` under
[gg-deep-concurrency-20261024](../benchmarks/gg-deep-concurrency-20261024/):

- **Output length decides what the probe measures.** With 128-token responses no
  two sequences are ever resident — a response lives ~1 s against a 42–89 s deep
  prefill — so all 18 cells showed zero evictions and identical walls at every
  util, even 1.7× oversubscribed. Long outputs are what make residency bind.
- **Two full-window sequences need ≥ 0.96**: 0.94 evicts once, second TTFT 338 s,
  wall 419 s; 0.96 and 0.98 evict never, 183 s and 282 s, agreeing to 0.5 s.
- **Four full-window sequences are 1.9–2.2× oversubscribed on one card at any
  util** — 6/3/2 evictions at 0.94/0.96/0.98, 923/589/585 s. The queue absorbs it
  and nothing errors; this is a TP2 question.
- **Nothing anywhere in this campaign failed, refused an request, or produced an
  OOM line.** Every consequence of pressure was latency and eviction.

Decision as recorded on 2026-10-25, on this evidence alone: `GPU_MEMORY_UTILIZATION`
stays 0.94, because the whole 0.98 gain was deep concurrent load and the C1 cost looked
nil — a reading that rested on a 177 s vs 178 s figure which was actually the 245K × 2
control cell (two concurrent sequences), not a single-stream measurement. Superseded by
the corrected C1 A/B, the ceiling ladder, and the v0.32 re-validation below.

### Re-validation on the v0.32 image (burst, 2026-10-27)

The gg result above is one image. Because the band is page quantization, and page
sizing is a property of the engine build, the same 16-cell grid was re-run on burst's
`:local` (`abbde425a717`, v0.32 line) on the same card type — TP=1, YaRN 524288, MTP3,
FP8 KV, 16,384-token outputs. Receipts:
[burst-deep-concurrency-20261026](../benchmarks/burst-deep-concurrency-20261026/).

| wall s / evictions | 0.94 | 0.96 | 0.97 | 0.98 |
|---|---|---|---|---|
| 245K × 2 | 171.1 / 0 | 171.6 / 0 | 171.4 / 0 | 171.5 / 0 |
| **245K × 4** | 356.7 / 1 | **441.1 / 4** | **276.0 / 0** | 277.1 / 0 |
| 512K × 2 | 408.7 / 1 | 270.9 / 0 | 270.8 / 0 | 270.2 / 0 |
| 512K × 4 | 905.3 / 6 | 550.6 / 2 | 553.2 / 2 | 567.1 / 2 |

Three separate answers, and they disagree in a way that matters:

- **The trap survives.** 0.96 is still worse than the 0.94 default on the mid-depth
  four-way cell (441 s vs 357 s), and still much worse than 0.97 (276 s).
- **The mechanism survives.** Every boot prints `Setting attention block size to 3200
  tokens` and `kv lcm block sizes 3200`, identical to v0.31.
- **The numbers do not survive.** Pools are 303 / 344 / 363 / 384 pages against gg's
  296 / 336 / 356 / 376 — ~7–8 pages gained from a non-KV footprint that is 0.22 GiB
  *smaller*. Extra pages bought nothing at 0.96; the clean edge arrived at the same
  utilization.

So any rule written as a page count expires with the image, and the portable statement
is the structural one: never choose a pool that admits N sequences for prefill but
cannot hold N at full length plus state. To keep that honest in code rather than prose,
`scripts/check-residency-geometry.py` reads the live engine's `vllm:cache_config_info`
labels and is a gating leg of `scripts/qualification-legs.sh`: attention block size ≠
3200 fails outright (the arithmetic is void), pool drift beyond a tolerance fails with
instructions to re-run the band probe, and an unreachable engine fails rather than
passing. Qualified geometry for both images is recorded in
[benchmarks/RESIDENCY-GEOMETRY.json](../benchmarks/RESIDENCY-GEOMETRY.json). Pool size
is `kv_cache_size_tokens ÷ effective_attention_block_size`; the `num_gpu_blocks` label
counts a different pool on this hybrid model (a 303-page boot reports 342) and comparing
pools across topologies is meaningless — TP2 at 0.98 holds 2,143 pages by construction.

### Where the ceiling actually is

`GPU_MEMORY_UTILIZATION` is a commanded fraction, not a measured limit: the pool is
whatever the fraction allows, so pool numbers say nothing about what the engine could
have claimed. The engine prints its own ceiling at every boot — `to fully utilize gpu
memory: 19.19 GiB` — and a ladder run up to refusal brackets it: **0.98 boots (18.26
GiB / 1,227,117 tokens), 0.99 boots (19.21 GiB / 1,289,465), 1.00 refuses** with
`Free memory on device cuda:0 (94.5/95.06 GiB) on startup is less than desired GPU
memory utilization (1.0, 95.06 GiB)`. Claimable device memory is therefore **94.50 of
95.06 GiB**; the missing 0.56 GiB is driver and firmware. Receipts:
[burst-ceiling-20261027](../benchmarks/burst-ceiling-20261027/).

Two consequences. 0.98 is *not* maximal — it takes ~95% of the claimable pool — and it
is not at a cliff edge either, since 0.99 boots. And because the refusal is arithmetic
on free-at-startup, a co-tenant holding more than `total − util × total` prevents the
engine from starting at all: at 0.98 that tolerance is ~1.4 GiB, at 0.94 it is ~5.7 GiB.
Every boot in this campaign saw the identical `94.51/95.06 GiB`, i.e. an exclusively
held, warm device; no sample exists with a co-tenant or a cold page cache.

### C1 A/B at 0.94 vs 0.98

An earlier line in this ledger claimed 0.94 and 0.98 were identical at C1 on the
strength of a 177 s vs 178 s figure. That figure was the 245K × 2 control cell — two
concurrent sequences — and was not a single-stream measurement. Corrected and measured
properly on burst (prefill ladder, C1 decode, seven and orchid timed suites, same boot,
one util per boot):

| leg | 0.94 | 0.98 | delta |
|---|---|---|---|
| C1 decode, tok/s | 120.7 | 118.4 | −1.9% |
| 128K prefill TTFT / tps | 17.80 s / 7192 | 18.03 s / 7101 | +1.3% / −1.3% |
| 261,632 prefill TTFT / tps | 37.63 s / 6953 | 37.59 s / 6960 | −0.1% / +0.1% |
| seven blend, tok/s | 148.6 | 148.1 | −0.3% |
| orchid median, tok/s | 239.2 | 238.6 | −0.3% |

No leg failed. The largest single-leg difference is 1.9%, on one card of a pair whose
other member ran a residency grid concurrently.

### Runtime VRAM growth: what the headroom is actually for

Headroom below the ceiling exists to absorb growth during the engine's lifetime, so the
question is the peak device demand, not the boot-time fraction. The original gate
(`util-stress.sh`) could not answer it: it deliberately skipped 0.94 — the only level
with slack — and at 0.98 the KV pool absorbs runtime demand as an eviction, so device
usage is flat *by construction*; its receipts hold 79 samples of which 78 are the
identical value.

`scripts/util-peak.sh` measures against slack instead: boot at 0.94, sample from before
boot through the last phase, report each phase's increment against the engine's own
profiled activation peak (that is the correct comparator — the pool is sized as
`util × total − weights − non-torch − profiled activation − graphs`, so required
headroom above the profile is `max(0, peak − baseline − profiled_activation)`).

| phase | card 0 | card 1 |
|---|---|---|
| baseline, util 0.94 | 92,082 MiB | 91,570 MiB |
| vision, 16 images | +0 | +0 |
| structured output | +0 | **+490 MiB** |
| single 240K prefill | +0 | +0 |
| 4 concurrent deep prefills | +0 | +0 |
| all-hands (vision + structured + 2 deep) | +0 | — |
| 16-way decode at the graph bucket | not run | +0 |

Verdict line from the run: `growth over baseline 0 MiB | engine profiled activation
1290 MiB | headroom above the engine's own profile: none required`. Two caveats stated
plainly. The `+490 MiB` on one card and `+0` on the other for the same phase is the
tell: device usage is a high-water mark, so a zero means *no new physical segment was
demanded* — the torch cache absorbed it — not that nothing was allocated. And the
16-way decode leg failed to run on card 0 (`benchmark-decode.py` requires
`--mtp-tokens`, argparse exited 2, and the phase row still printed with the previous
peak: a table that looks complete with one phase fabricated). Both fixed.

`PYTORCH_NO_CUDA_MEMORY_CACHING=1`, the obvious way to expose peak demand, **does not
work on this engine**: the loader then reports `Model loading took 0.0 GiB`, the memory
profile is computed against a phantom, and CUDA graph capture dies
(`benchmarks/burst-peak-nocache-20261027/`). The replacement is a positive control —
allocate 1.5 GiB inside the engine container and require the sampler to record it,
aborting the run if it cannot — because every row in the table above is a negative
result, and a sampler that cannot see growth would produce exactly this table.

### TP2 at 0.98: what deep concurrency actually needs

Two Max-Q cards, `TP_SIZE=2`, util 0.98, same probe
([burst-tp2-0.98-20261027](../benchmarks/burst-tp2-0.98-20261027/)):

| cell | TP1 best (any util) | TP2 @ 0.98 |
|---|---|---|
| 245K × 4 | 276 s / 0 evictions | **182.9 s / 0** |
| 512K × 4 (2.05× oversubscribed) | 550–905 s / 2–6 evictions | **315.1 s / 0** |

Pool 6,858,253 tokens (13.08× the window), non-KV footprint 38.3 GiB per rank. The
caution: boot free is 93.98/95.05 GiB on the Max-Q pair versus 94.51/95.06 on the
Workstation Edition, so per-rank margin at 0.98 is ~0.83 GiB — tighter than the 1.4 GiB
on the card this default was qualified on. Four full-window sequences is a TP2
question at any single-card utilization; raising util is not a substitute for the
second card.

## Greedy decoding on this stack is not batch-invariant

Found while building the eviction-identity test, and more consequential for this ledger than
the question that surfaced it. `exl3`, v0.32, FP8 KV, MTP3, `temperature=0`, fixed `seed=1234`
(`scripts/test-eviction-identity.py` plus ad-hoc probes):

| condition | distinct outputs | max per-token logprob drift |
|---|---|---|
| same prompt, run alone ×5 | 1 (bit-identical) | **0.000** |
| same prompt + 1 concurrent filler request ×5 | 1 | 0.452 |
| + 3 filler ×5 | 1 | 0.469 |
| + 8 filler ×5 | 1 | 0.323 |
| + 16 filler ×5 | 1 | 0.385 |
| 512-token prompts, concurrent, repeats | **2** | — |
| 245K-token filler prompts, concurrent, 3 boots | 1 (a period-23 cycle) | — |

Three things follow, and the first two are the ones that matter for anyone reading this
ledger's numbers.

**Repeatability holds only in isolation.** An uncontested request is bit-exact. Add a single
concurrent request and every emitted token's logprob moves by up to half a nat. Note that the
drift does *not* scale with batch size (16 fillers drift *less* than 1) — it is driven by batch
*shape* selecting a different kernel and reduction order, not by contention or load.

**Token streams are more stable than logprobs, but not invariant.** At 4,096 tokens of depth,
five repeats under four different batch shapes produced one output. At 512 tokens of depth, the
stream itself differed. The distinction is not depth: what survived was a highly deterministic
continuation, where sub-nat perturbation cannot flip an argmax, and what flipped was a
low-margin choice. Which side a given workload falls on is not knowable in advance, so a
divergence seen under load is not automatically a defect.

**This bounds what the eviction-identity test can claim.** Judging an evicted sequence against
a *solo* reference would report a correctness veto for behaviour present in every deployment —
and the first version of that test did exactly that, "failing" a run with zero preemptions. The
only usable control is another boot at the same utilization with the same cold prompts, so
ordinary cross-run variance is measured rather than assumed. Where the two rates are equal, the
honest answer is INCONCLUSIVE, not PASS.

Operational consequence for this recipe: legs that assert on generated content across runs
(`retrieval`, `boundary`, the seven/orchid suites, `api-tools`) are comparing outputs produced
under different batch schedules. Assert on the answer-bearing span, use a fixed seed *and* a
fixed request mix, and treat a token-level mismatch as something to reproduce under isolation
before treating it as a regression. Do not gate on logprob equality; on this engine it is
meaningless.

## What protects the 0.98 default, and from what

Two distinct hazards were investigated, and they behave differently enough that the ledger
kept conflating them.

**Runtime device-memory growth** — torch demanding physical VRAM beyond its reserved KV pool.
Measured with `scripts/util-peak.sh` (above): 0 MiB on one card, +490 MiB on another, against a
1.29 GiB profiled activation reserve, with the sampler's positive control proving it could see
a deliberate 1.5 GiB. The KV pool is a fixed reservation, so growth can only be satisfied from
what the utilization fraction left behind: **1,395 MiB at 0.98 on the Workstation Edition card**
(measured, printed by `start.sh`), versus ~5.2 GiB at 0.94.

**Boot-time refusal** — the engine will not start when free-at-startup < util x total. This is
the sharper of the two, because it is a hard failure rather than a slowdown, and the tolerance
at 0.98 is that same 1,395 MiB: a neighbour holding ~1.5 GiB prevents the engine from starting
at all, where 0.94 would have tolerated ~5.7 GiB.

Since co-tenancy is unsupported, `start.sh` now enforces the second one at launch rather than
discovering it seven minutes deep as a CUDA error pointing at the GPU: it prints the budget
arithmetic per device and refuses in under a second, naming the holder. `ALLOW_CO_TENANT=1`
opts out. That converts "raise the default and pray nobody else touches the card" into "raise
the default and get told immediately if the card is not yours", which is why the 0.97-vs-0.98
margin question is now settled in favour of the concurrency win.

Measured since this section was first written: allocator fragmentation under sustained concurrent
load. 75 minutes at 0.98 with a 0.94 reference alongside: the peak stepped 494 MiB once and moved
0 MiB across the remaining 25 rounds, so there is no creep and no segment-accumulation effect at
this window. Still unmeasured: cold-page-cache restart latency -- reclassified below, because it
cannot move the reservation but it does move restart time, and every boot duration quoted in this
ledger has quietly been a warm-cache one.

Two claims this section originally carried were wrong, and are retracted here rather than edited
away. First, "needs root to drop caches; no passwordless sudo": `burst` grants
`sudo /usr/local/bin/ple-drop-caches` without a password, along with `compsize`, `dmesg` and
`nvidia-smi`. I ran `sudo -n true`, watched it fail, and generalised to "no sudo here" -- while
`docs/ssd-stream-findings.md` had not only recorded the grant but recorded that drawing this
conclusion earlier was a mistake. Second, the checkpoint is not largely absent from the page
cache; it is 100 % resident. The 60 MiB-of-95-GiB reading behind that belief measured the *nvfp4*
profile's directory on a host holding several checkpoints, and `vmtouch` without `-f` on a
Hugging Face snapshot reports the symlink entries rather than the blobs they point at, so it
returns ~0 % for a model that is entirely in RAM. Both artifacts manufacture a confident "already
cold, so it cannot matter". `scripts/boot-latency.sh` resolves the checkpoint by sourcing
`model-profiles.sh` instead of re-deriving a path, always passes `-f`, and refuses to label a boot
cold or warm until it has asserted the residency it is claiming.

## Verdict: `GPU_MEMORY_UTILIZATION` default 0.94 → 0.98

Adopted decision rule (2026-10-26, owner): C1 stays important, and 0.98 becomes the
default if it is (a) safe under worst-case memory conditions, (b) not measurably worse
at C1, and (c) clearly better under concurrent deep load. **Adopted 2026-10-27:
`DEFAULT_GPU_MEMORY_UTILIZATION` is now 0.98 on RTX.**

- **(b) satisfied**: ≤1.9% on every C1 leg, above.
- **(c) satisfied on two images**: 245K × 4 goes 357 s → 276 s (gg) and 357 s → 277 s
  (burst) with zero evictions instead of one; 512K × 2 goes 409 s → 270 s; and 0.98
  clears the 0.96–0.965 trap band on both. Scope of that win, narrowed by the soak: it is
  a win against *co-residency*, not against concurrency in general. With 512-token
  outputs, where four deep sequences never need to be resident at depth simultaneously,
  0.94 completed each round in 123 s against 0.98's 146 s -- admitting three and queueing
  the fourth hands each prefill the whole 2,048-token batch budget, whereas admitting four
  splits it -- with zero evictions either way. The default still favours 0.98, because the
  shape that fails at 0.94 fails by thrashing (up to 2.6x with evictions) while the shape
  where 0.94 leads loses 16% of wall clock and nothing else. But read (c) as "under long
  concurrent generations", which is what it was measured under.
- **(a) satisfied as measured, and now measured at the shipped utilization**: 0.99 boots, so 0.98 is
  not at a cliff edge. Sustained load -- 75 minutes, 25 rounds of structured decode + 4 concurrent
  deep prefills + vision, with the 0.94 default soaking alongside on the same host -- stepped the
  peak by **494 MiB once**, on the constrained-decode path, and moved 0 MiB across every subsequent
  round. Identical to the MiB at both utilizations, so the step belongs to the FSM path and not to
  the fraction. And it lands *inside* the reservation: the plateau is 61 MiB above the 0.98 budget
  and 48 MiB above the 0.94 one, because the 1.29 GiB profiled activation reserve sits within
  `util x total`. Runtime growth therefore does **not** consume the 1,395 MiB remainder, which an
  earlier version of this section asserted it did -- the remainder is precisely what the engine left
  alone. The positive control passed on the same boot at a size derived from the memory actually
  free (608 MiB held against 1,352 MiB free; 1,163 MiB of rise recorded), which is what licenses
  reading the flat rows as real zeros rather than a blind sampler. No OOM line in any cell of any
  run in this campaign. Receipts: `benchmarks/burst-soak-20261027/`.

0.97 is not the answer even though it is clean: it is the boundary itself (356 pages on
gg against a modelled 4 × 89 = 356 requirement — zero margin), and 0.98 costs nothing
extra that was measured. 0.96 and 0.965 remain forbidden, now measured on both images.

### Open items

Ordered by what they could still change.

1. ~~**Sampler positive control**~~ — **done, passed** (1,553 MiB recorded while 1.5 GiB
   was held in the engine container; the probe now aborts instead of reporting phases if
   it ever fails). Its result is what makes the flat growth rows interpretable. Re-run on the
   sustained-load boot, at a size derived from the memory actually free: 1,163 MiB of rise against
   608 MiB held at 0.98, 2,091 against 1,536 at 0.94. What was open underneath it -- co-tenant
   boot margin -- is now refused at launch rather than sampled, and the remaining cold-page-cache
   question is latency, not margin, which is item 3.
2. **Output identity under eviction — six attempts, no verdict, and I recommend stopping
   here.** Same prompt and seed, once with zero evictions and once in the thrash regime,
   comparing token ids. It is the only candidate *correctness* veto over any utilization at or
   above the band, and it has not produced a verdict I would act on. Each attempt invalidated
   itself on a *different* axis, which is the useful output: comparing against a *solo* run
   reports a veto for ordinary batch-shape variance (the first version failed a run with zero
   preemptions); empty token streams score as agreement (four empty comparisons "passed"); a
   filler continuation is a period-23 repetition cycle in which agreement is guaranteed by
   construction (run 10: 10 evictions, unanimous across three boots, 22 distinct tokens in
   65,536 — caught by the entropy gate); and the served model is **chat-tuned**, so a plain
   text-completion probe answers with a reasoning block or with immediate end-of-text, which is
   what aborted run 11 at its smoke gate before any real leg booted. Fixing that last one means
   driving the harness through the chat endpoint with a single system turn and no history, i.e.
   a seventh campaign.
   **I recommend against it**, for two reasons. First, given the measured cross-run variance,
   INCONCLUSIVE is the modal outcome: two clean boots are likely to differ by as much as the
   evicting boot does, which is a statement about test power, not about eviction. Second, a
   FAIL would not change the default. Raising utilization only ever *reduces* eviction
   frequency — 0.98 measured zero evictions at 245K x 4 where 0.94 had one — so this default
   cannot make the behaviour worse than the default it replaced. A genuine mismatch would be a
   content defect in the preempt-and-restore path, present at 0.94, 0.96 and 0.98 alike, and
   reportable upstream; the remedy would be admission control, not a different number.
   What remains worth doing is the opposite axis: item 3, the boot-margin measurement, is the
   only open item that could argue for *lowering* the default.
3. **Cold-page-cache restart latency at 0.98.** Every boot in this campaign saw an exclusively
   held, warm device (`94.51/95.06 GiB` free). The co-tenant half of this item is closed: since
   sharing is unsupported, `start.sh` now measures the margin and refuses at launch rather than
   absorbing a neighbour, and the number it prints on these cards is **1,395 MiB** at 0.98. The
   cold-cache half is reclassified here, because it had been mis-scoped as a headroom question:
   free-at-startup is a *device* quantity, and 22 burst boots read 94.50/95.06 GiB while host-side
   available memory ranged 662 to 893 GiB -- a 231 GiB swing with a 10 MiB device response. Page
   cache cannot move the reservation. What it does move is how long a restart takes, and that has
   never been measured: the recipe's checkpoint (`Qwen3.8-Flash-Next-EXL3-K4.25-v1`, 168 GiB over
   37 files) is **100 % resident** on this host, so every boot duration quoted in this ledger is a
   warm-cache boot, unlabelled as such. `scripts/boot-latency.sh` measures the cold/warm pair and
   asserts the cache state before trusting either arm.
4. **MTP draft length re-sweep under concurrent deep load.** The 3/2 defaults were chosen
   as the best *C1* blend, and C1 is no longer the overriding priority; draft tokens also
   consume per-sequence residency, so util and MTP were never swept together. Marked
   pending in the README.
5. **Prefix caching on/off** — the `align` mamba cache mode is chosen because prefix
   caching is on, so disabling it changes per-sequence residency and may move the band.
6. **`MAX_NUM_SEQS` sweep at 0.98** — if state costs ~5–7 pages per sequence, 16 slots
   spend up to ~112 of 384 pages on state alone.
7. **Band map at a second shape** to pin the per-sequence state allowance (bracketed 5–7
   pages on gg, contradicted by ~4 in the 512K × 2 cell).
8. Spark remains unverified by this campaign (its owners' reports stand); gg's provider
   entry on this host is stale relative to the live server config.

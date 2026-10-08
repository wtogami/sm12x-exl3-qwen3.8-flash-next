# Quality Comparison: EXL3 vs NVIDIA NVFP4 vs RedHatAI NVFP4 (+ FP8-PLE variant)

Four checkpoints of **Qwen3.8-Flash-Next** ([base model](https://huggingface.co/Qwen/Qwen3.8-Flash-Next))
measured head-to-head on one RTX PRO 6000 Blackwell on 2026-10-07, all under
one serving stack: rebase-qualified engine image `8294c3c914c0` (vLLM
v0.31.0 fork), MTP3 speculative decoding, YaRN 512K context, 600 W power
limit, temperature 0 with thinking enabled, offline evaluation. All legs are
same-day and every receipt is stamped with its served model name.

**Headline: on tool-calling all four are statistically tied (83–85/100,
ten runs each). On GSM8K the NVIDIA build is genuinely lower (−2.4 pts);
EXL3, RedHatAI, and the FP8-PLE variant are indistinguishable. The
exl3-ple8 comparison (identical EXL3 experts, FP8 PLE table) shows the FP8
table format costs no quality, so NVIDIA's deficit belongs to its ModelOpt
NVFP4 expert pipeline. Instruction following favors the two non-NVIDIA
builds directionally but never significantly.**

## Checkpoints

| Quant | HF card (measured revision) | Method | Checkpoint size |
|---|---|---|---|
| EXL3 K4.25 v1 | [wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-v1](https://huggingface.co/wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-v1) [`73a050c`](https://huggingface.co/wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-v1/tree/73a050c27b8c488c65acd6d1c74e45ff02be5fab) | GPTQModel-fork calibrated mixed K4/K5 EXL3 routed experts (avg 4.25 bpw), BF16 PLE n-gram table retained | 179.4 GB |
| NVIDIA NVFP4 | [nvidia/Qwen3.8-Flash-Next-NVFP4](https://huggingface.co/nvidia/Qwen3.8-Flash-Next-NVFP4) [`fc694b5`](https://huggingface.co/nvidia/Qwen3.8-Flash-Next-NVFP4/tree/fc694b54fb0174e0913e6adf86691ef85a4ead47) | Model Optimizer MIXED_PRECISION: W4A4 NVFP4 main experts, FP8 block-128 MTP experts, per-tensor FP8 PLE; rest BF16 | 132.7 GB |
| RedHatAI NVFP4 | [RedHatAI/Qwen3.8-Flash-Next-NVFP4](https://huggingface.co/RedHatAI/Qwen3.8-Flash-Next-NVFP4) [`c8f2fb1`](https://huggingface.co/RedHatAI/Qwen3.8-Flash-Next-NVFP4/tree/c8f2fb1b9869f686b214782036123b10ff96d14a) | LLM Compressor (compressed-tensors): NVFP4 MoE experts only; everything else, incl. the 102.5 GB PLE table, BF16 | 174 GB |
| exl3-ple8 (FP8-PLE variant) | [wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-PLE-FP8-v1](https://huggingface.co/wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-PLE-FP8-v1) [`888306b`](https://huggingface.co/wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-PLE-FP8-v1/tree/888306bd3996d6317758c07df50622829259ad17) | Same EXL3 K4.25 trellis experts as row 1; **PLE table only** in FP8 (~48 GiB payload vs ~95 GiB BF16) | 128.3 GB |

## Text quality (lm_eval 0.4.13, `local-chat-completions`, thinking on, temp 0)

| Suite | Metric | EXL3 | NVIDIA | RedHatAI | exl3-ple8 | Verdict |
|---|---|---|---|---|---|---|
| GSM8K (1319, 5-shot) | flexible-extract | 0.9196 / 0.9158 | 0.8954 | 0.9196 | **0.9196 / 0.9242** | NVIDIA −2.4 pts, real; others tied |
| GSM8K | strict-match | 0.9181 / 0.9136 | 0.8931 | 0.9189 | 0.9189 / 0.9234 | same direction |
| IFEval (541) | prompt strict | 0.7985 / 0.8059 | 0.7911 | 0.7874 | **0.8189 / 0.8059** | tie (within noise) |
| IFEval | inst strict | 0.8058 / 0.8213 | 0.7986 | 0.7926 | **0.8225 / 0.8118** | tie |
| IFEval | prompt loose | 0.8189 / 0.8299 | 0.8133 | 0.8041 | **0.8410 / 0.8281** | tie |
| IFEval | inst loose | 0.8189 / 0.8369 | 0.8130 | 0.8034 | **0.8381 / 0.8261** | tie |

EXL3 and ple8 show two runs (their jitter reference legs); NVIDIA and
RedHatAI one each. Paired exact-McNemar statistics on the per-item files
(item difficulty cancels):

- NVIDIA vs EXL3 GSM8K −2.43 pts, 38/70 discordant, **p=0.003** — a
  genuine deficit, ~7x the measured engine jitter.
- EXL3 vs RedHatAI GSM8K: dead tie (symmetric 51/51 swap, p=1.0).
- **ple8 vs EXL3 GSM8K: tie on all four pairings** (27/27 p=1.0; 40/29
  p=0.23; crosses p=0.64/0.51).
- **ple8 vs NVIDIA: ple8 wins** (74/42 p=0.004; 70/32 p=0.0002).
- All IFEval pairwise comparisons tie or are borderline on a single run
  only (paired CI ±2.0–2.7 pts at n=541).

## BF16 vs FP8 PLE tables (exl3 vs exl3-ple8)

To find out whether NVIDIA's GSM8K deficit could be explained by its FP8
PLE table alone, `exl3-ple8` changes only that axis: identical K4.25
quantized experts, PLE n-gram table in FP8 (~48 GiB host payload) instead
of BF16 (~95 GiB).

The two are statistically equivalent on every quality axis — GSM8K 0.9219
vs 0.9177 (means of 2, McNemar ties on all four pairings), IFEval ties,
tool-calling tie (10 runs each). The quantized expert weights carry the
model's quality and the n-gram PLE table tolerates FP8, so the FP8 table
halves the PLE host footprint at no measurable quality cost; only one
difference surfaced, a single off-by-one orchid miss (4/5 vs 5/5). The
same comparison localizes NVIDIA's −2.4 pt GSM8K deficit: exl3-ple8 runs
NVIDIA's PLE table format at EXL3 quality, so the deficit belongs to
NVIDIA's ModelOpt NVFP4 experts — RedHatAI's CT-quantized NVFP4 experts
(with BF16 PLE) tie EXL3 as well.

## Tool-calling quality (tool-eval-bench, 88 cases incl. 19 Hard Mode)

The harness is deterministic but vLLM temp-0 serving is not, so each quant
was run **ten times** on its engine instance:

| Quant | 10 runs (pts/176) | Mean | 95% CI | Hard Mode mean |
|---|---|---|---|---|
| EXL3 | 152 145 148 147 144 149 144 149 144 146 | 146.8 (83/100) | [144.9, 148.7] | 28.1 / 38 |
| NVIDIA NVFP4 | 150 149 144 148 151 142 149 154 145 148 | 148.0 (84/100) | [145.5, 150.5] | 28.9 / 38 |
| RedHatAI NVFP4 | 147 150 151 151 150 151 146 155 148 145 | 149.4 (85/100) | [147.3, 151.5] | 30.2 / 38 |
| exl3-ple8 | 151 149 144 149 149 147 154 151 152 151 | 149.7 (85/100) | [147.7, 151.7] | 30.7 / 38 |

Welch t + Holm over all six pairs: **every pair ties** (smallest raw
p=0.030, EXL3 vs ple8, Holm-adjusted p=0.178). At 10 runs the resolvable
gap is ~2.5 pts; the observed 2.9-pt spread sits inside it. ple8 and
RedHatAI are directionally ahead of EXL3 but not significantly.

## Structural / repetition checks (core suite)

Protocol-identical legs for all four quants (served model names stamped in
every receipt):

| Check | EXL3 | NVIDIA | RedHatAI | exl3-ple8 |
|---|---|---|---|---|
| API tool constraints | 16/16 | 16/16 | 16/16 | 16/16 |
| Vision 1/4/16 images | pass | pass | pass | pass |
| Seven content contracts (21 timed) | 20/21 | 18/21 | 19/21 | 18/21 |
| Orchid exact ×100 repetition | **5/5** | 3/5 | 1/5 | 4/5 |

All seven-suite failures are the same documented fable word-count band
(140–170 words); the NVFP4 builds overshoot slightly more often. The
distinctive regression is **exact repetition** (orchid): NVIDIA −2 runs,
RedHatAI −4, FP8-PLE variant −1, all misses off-by-one counts (99–102
occurrences). Earlier legs showed occasional 1500-token runaway loops on
NVFP4; none recurred in the four clean legs, so loops are treated as rare
engine jitter, not deterministic behavior.

## Noise floor (measured, repeat legs)

| Suite | Items flipping per repeat | Per-run score sd |
|---|---|---|
| GSM8K | 3.1-3.3% | ~0.35 pts |
| IFEval | 6.3% prompts / 7.1% instructions | ~0.7-0.8 pts |
| tool-eval-bench | 25-32% of the 88 scenarios | 2.7-3.5 pts (10-run pooled) |

Consequently: GSM8K gaps >= ~1 pt are resolvable with one run per side
(paired); tool-eval gaps >= ~2.5 pts are resolvable at 10 runs per quant;
IFEval single-run gaps under ~2 pts are noise by construction.

## Relation to vendor-card claims

- RedHatAI's card reports average accuracy recovery 99.1% vs NVIDIA 98.0%
  across a coding/knowledge battery — the same ordering our independent
  GSM8K result finds (RedHatAI = EXL3 = ple8 > NVIDIA).
- NVIDIA's card evaluates GPQA/HLE/AIME etc.; its math axis (AIME 2026) is
  saturated at 100 for every NVFP4 build and cannot discriminate. The
  non-saturated GSM8K does: NVIDIA sits 2.4 pts low.
- The EXL3 card's own validation is held-out perplexity (3.879 EXL3 vs
  3.873 BF16 framework reference); the task-level results above are the
  missing end-to-end confirmation it called for.

## Bottom line

- **EXL3 ≈ RedHatAI ≈ exl3-ple8** on every quality axis measured (GSM8K,
  IFEval, tool-calling 10-run stats, retrieval, vision, tool constraints).
- **NVIDIA NVFP4** is the smallest (132.7 GB), fastest-prefilling and
  best-C16-scaling build, and loses the least exact repetition of the two
  NVFP4 builds (orchid 3/5) — but still costs a statistically real −2.4 pt
  GSM8K regression against all three others. The cause is localized: not
  the FP8 PLE table (exl3-ple8 runs that format at EXL3 quality) but
  NVIDIA's ModelOpt NVFP4 expert pipeline. FP8 PLE's own quality cost is
  one off-by-one orchid miss (4/5 vs 5/5); its host-memory win stands.
- RedHatAI pays nvidia's ~2x PLE host memory (102.5 GB BF16 vs 51.2 GB
  FP8 pinned; EXL3 carries the same BF16 table) for its quality —
  exl3-ple8 shows the FP8 PLE format is *not* what costs quality, so the
  BF16 table is a choice, not a requirement.
- Full performance comparison:
  [performance-comparison.md](performance-comparison.md).

Receipts:
[quality-deep-20261007](../benchmarks/quality-deep-20261007/) (GSM8K/IFEval
per-quant results + paired analysis),
[ple8-quality-20261007](../benchmarks/ple8-quality-20261007/) (FP8-PLE
variant: core suite, GSM8K×2, IFEval×2 with per-item samples, tools ×5),
[core-suite-20261007](../benchmarks/core-suite-20261007/) (protocol-clean
four-way core suites),
[tool-eval-repeats-20261007](../benchmarks/tool-eval-repeats-20261007/)
(4 quants × 10 runs with traces),
[nvfp4-comparison-20261007](../benchmarks/nvfp4-comparison-20261007/)
(performance ladder).

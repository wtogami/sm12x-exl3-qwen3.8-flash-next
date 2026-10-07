# Quality Comparison: EXL3 vs NVIDIA NVFP4 vs RedHatAI NVFP4

Three quantizations of **Qwen3.8-Flash-Next** ([base model](https://huggingface.co/Qwen/Qwen3.8-Flash-Next))
measured head-to-head on one RTX PRO 6000 Blackwell on 2026-10-07, all under
one serving stack: rebase-qualified engine image `8294c3c914c0` (vLLM
v0.31.0 fork), MTP3 speculative decoding, YaRN 512K context, 600 W power
limit, temperature 0 with thinking enabled, offline evaluation.

**Headline: on tool/agentic quality all three are statistically tied
(84-85/100). On GSM8K the NVIDIA build is genuinely lower (-2.4 pts); EXL3
and RedHatAI are indistinguishable. Instruction following (IFEval) favors
EXL3 directionally but never significantly.**

## Checkpoints

| Quant | HF card (measured revision) | Method | Checkpoint size |
|---|---|---|---|
| EXL3 K4.25 v1 | [wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-v1](https://huggingface.co/wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-v1) [`73a050c`](https://huggingface.co/wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-v1/tree/73a050c27b8c488c65acd6d1c74e45ff02be5fab) | GPTQModel-fork calibrated mixed K4/K5 EXL3 routed experts (avg 4.25 bpw), BF16 PLE n-gram table retained | 179.4 GB |
| NVIDIA NVFP4 | [nvidia/Qwen3.8-Flash-Next-NVFP4](https://huggingface.co/nvidia/Qwen3.8-Flash-Next-NVFP4) [`fc694b5`](https://huggingface.co/nvidia/Qwen3.8-Flash-Next-NVFP4/tree/fc694b54fb0174e0913e6adf86691ef85a4ead47) | Model Optimizer MIXED_PRECISION: W4A4 NVFP4 main experts, FP8 block-128 MTP experts, per-tensor FP8 PLE; rest BF16 | 132.7 GB |
| RedHatAI NVFP4 | [RedHatAI/Qwen3.8-Flash-Next-NVFP4](https://huggingface.co/RedHatAI/Qwen3.8-Flash-Next-NVFP4) [`c8f2fb1`](https://huggingface.co/RedHatAI/Qwen3.8-Flash-Next-NVFP4/tree/c8f2fb1b9869f686b214782036123b10ff96d14a) | LLM Compressor (compressed-tensors): NVFP4 MoE experts only; everything else, incl. the 102.5 GB PLE table, BF16 | 174 GB |

## Text quality (lm_eval 0.4.13, `local-chat-completions`, thinking on, temp 0)

| Suite | Metric | EXL3 | NVIDIA | RedHatAI | Verdict |
|---|---|---|---|---|---|
| GSM8K (1319, 5-shot) | flexible-extract | **0.9196** | 0.8954 | **0.9196** | NVIDIA -2.4 pts, real |
| GSM8K | strict-match | 0.9181 | 0.8931 | 0.9189 | same direction |
| IFEval (541) | prompt strict | **0.7985** | 0.7911 | 0.7874 | tie (within noise) |
| IFEval | inst strict | **0.8058** | 0.7986 | 0.7926 | tie |
| IFEval | prompt loose | **0.8189** | 0.8133 | 0.8041 | tie |
| IFEval | inst loose | **0.8189** | 0.8130 | 0.8034 | tie |

Paired exact-McNemar statistics on the per-item files (item difficulty
cancels): NVIDIA vs EXL3 GSM8K -2.43 pts, 38/70 discordant, **p=0.003** —
a genuine deficit, ~7x the measured engine jitter. EXL3 vs RedHatAI GSM8K
is a dead tie (symmetric 51/51 swap, p=1.0). All 12 IFEval pairwise
comparisons tie (p>=0.22, paired CI ±2.0-2.7 pts).

## Tool-calling quality (tool-eval-bench, 88 cases incl. 19 Hard Mode)

The harness is deterministic, so each quant was run **5 times** on the same
engine instance to average out vLLM's temp-0 scheduling jitter:

| Quant | 5 runs (pts/176) | Mean | 95% CI | Hard Mode mean |
|---|---|---|---|---|
| EXL3 | 152 145 148 147 144 | 147.2 (84/100) | ±3.9 | 28.2 / 38 |
| NVIDIA NVFP4 | 150 149 144 148 151 | 148.4 (84/100) | ±3.4 | 28.6 / 38 |
| RedHatAI NVFP4 | 147 150 151 151 150 | 149.8 (85/100) | ±2.0 | 29.4 / 38 |

No pairwise difference is significant (Welch t + Holm, p>=0.15;
per-scenario paired gaps <=0.03 pts). The single-run ranking from an
earlier round (155 > 152 > 149) was pure run-to-run variance.

## Structural / repetition checks (core suite)

| Check | EXL3 | RedHatAI | NVIDIA |
|---|---|---|---|
| API tool constraints | 16/16 | 16/16 | 16/16 |
| Vision 1/4/16 images | pass | pass | pass |
| Seven content contracts | 20/21 | 19/21 | 17-18/21 (pre-rebase leg) |
| Orchid exact x100 repetition | **5/5** | 0/5 (two 1500-tok loops) | 1/5 (pre-rebase leg) |

All seven-suite failures are the same documented fable word-count band
(140-170 words); the NVFP4 builds overshoot slightly more often. The
distinctive NVFP4 regression is **exact repetition** (orchid): both NVFP4
builds lose EXL3's perfect 5/5, sometimes looping to the token cap.

## Noise floor (measured, EXL3 repeat legs)

| Suite | Items flipping per repeat | Per-run score sd |
|---|---|---|
| GSM8K | 3.1-3.3% | ~0.35 pts |
| IFEval | 6.3% prompts / 7.1% instructions | ~0.7-0.8 pts |
| tool-eval-bench | 25-32% of the 88 scenarios | 1.6-3.1 pts |

Consequently: GSM8K gaps >= ~1 pt and tool-eval gaps >= ~5 pts are
resolvable with the run counts used here; IFEval single-run gaps under
~2 pts are noise by construction.

## Relation to vendor-card claims

- RedHatAI's card reports average accuracy recovery 99.1% vs NVIDIA 98.0%
  across a coding/knowledge battery — the same ordering our independent
  GSM8K result finds (RedHatAI = EXL3 > NVIDIA).
- NVIDIA's card evaluates GPQA/HLE/AIME etc.; its math axis (AIME 2026) is
  saturated at 100 for every NVFP4 build and cannot discriminate. The
  non-saturated GSM8K does: NVIDIA sits 2.4 pts low.
- The EXL3 card's own validation is held-out perplexity (3.879 EXL3 vs
  3.873 BF16 framework reference); the task-level results above are the
  missing end-to-end confirmation it called for.

## Bottom line

- **EXL3 ≈ RedHatAI** on every quality axis measured; RedHatAI pays ~2x
  PLE memory (102.5 GB BF16 vs 51.2 GB FP8 pinned host) for it.
- **NVIDIA NVFP4** is the smallest (132.7 GB) and fastest-prefilling build
  (131K TTFT 9.5 s vs EXL3 13.1 s) but costs a statistically real −2.4 pt
  GSM8K regression and shares the exact-repetition weakness.
- All three tie on tool calling — the axis where NVFP4 expert GEMMs
  plausibly had the most to lose, they don't.
- Quality-equivalence comes with a performance caveat in the other
  direction: EXL3 scales far better at C16 decode (871.7 vs 601.8 tok/s).
  Full performance ladder: [qualification.md](qualification.md).

Receipts:
[quality-deep-20261007](../benchmarks/quality-deep-20261007/) (GSM8K/IFEval
per-quant results + paired analysis),
[tool-eval-repeats-20261007](../benchmarks/tool-eval-repeats-20261007/)
(15 runs with traces),
[quality-nvfp4-vs-exl3-20261007](../benchmarks/quality-nvfp4-vs-exl3-20261007/)
(core suite), [nvfp4-comparison-20261007](../benchmarks/nvfp4-comparison-20261007/)
(performance ladder).

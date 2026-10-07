# Deep quality: EXL3 vs NVIDIA NVFP4 (2026-10-07)

Single RTX PRO 6000 (96 GB, 4x8-pin, 600 W card default). Same day, same
image `8294c3c914c0` (vLLM v0.31.0 rebase), both legs served with YaRN 512K
(`LONGCTX=1`), MTP3, thinking on, temperature 0, `HF_HUB_OFFLINE=1`.

Checkpoints:

- EXL3: `wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-v1` @ `73a050c2`
- NVFP4: `nvidia/Qwen3.8-Flash-Next-NVFP4` @ `fc694b54` (ModelOpt
  MIXED_PRECISION: NVFP4 experts 48 layers, FP8 block-128 MTP experts,
  FP8 per-tensor PLE n-gram table in a dedicated shard; 132.7 GB).
  Content-identical to the pre-super-squash pin `2061e0b0`; the profile
  pin was re-pinned to `fc694b54` in `model-profiles.sh`.

The nvidia checkpoint loads through the stock `ModelOptMixedPrecisionConfig`
path — no engine patch or image rebuild was needed (unlike the RedHatAI
CompressedTensors checkpoint). The FP8 PLE resolves via
`quantized_layers` directly to `Qwen4ExpPLEFp8EmbeddingMethod`.

Harness: lm_eval 0.4.13, `local-chat-completions` + `--apply_chat_template`
+ `tokenized_requests=False` (server applies the Qwen3 chat template; the
reasoning parser strips thinking), `num_concurrent=16`,
`max_gen_toks=3072` (IFEval's task config caps generation at 1280), each
leg tokenized with its own checkpoint's tokenizer.

Timing (UTC): EXL3 leg 09:02-09:22 (GSM8K 492 s, IFEval 689 s); NVFP4 leg
15:18-15:37 (GSM8K 445 s, IFEval 673 s), 10-problem GSM8K smoke 10/10
before the battery. Engine swapped between legs; EXL3 restored after the
NVFP4 leg.

## GSM8K (1319 problems, 5-shot, exact match)

| Filter | EXL3 | NVFP4 | Delta |
|---|---|---|---|
| flexible-extract | 0.91964 +/- 0.00749 | 0.89538 +/- 0.00843 | -2.43 pts |
| strict-match | 0.91812 +/- 0.00755 | 0.89310 +/- 0.00851 | -2.50 pts |

## IFEval (541 prompts, 0-shot)

| Metric | EXL3 | NVFP4 | Delta |
|---|---|---|---|
| prompt strict | 0.79852 +/- 0.01726 | 0.79113 +/- 0.01749 | -0.74 pts |
| inst strict | 0.80576 | 0.79856 | -0.72 pts |
| prompt loose | 0.81885 +/- 0.01657 | 0.81331 +/- 0.01677 | -0.55 pts |
| inst loose | 0.81894 | 0.81295 | -0.60 pts |

## Findings

- EXL3 leads on both suites: GSM8K by ~2.4-2.5 pts (well outside the
  ~0.8 pt stderr), IFEval by ~0.6-0.7 pts (within stderr).
- Consistent with the core suite's NVFP4 regressions (orchid exact
  repetition 0/5, seven 19/21): the NVFP4 quantization trades a small
  amount of quality for the prefill speedup.
- NVFP4 eval time is ~10% faster on GSM8K and ~2% faster on IFEval, in
  line with the prefill-heavy workload mix of these suites.

Per-leg results JSON: `exl3/`, `nvfp4/` (plus the NVFP4 smoke gate).
Per-sample generations were logged to
`/tmp/opencode/quality-deep/{exl3,nvfp4}-{gsm8k,ifeval}/` (not committed).
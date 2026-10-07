# Deep quality: EXL3 vs NVIDIA NVFP4 vs RedHatAI NVFP4 (2026-10-07)

Single RTX PRO 6000 (96 GB, 4x8-pin, 600 W card default). Same day, same
image `8294c3c914c0` (vLLM v0.31.0 rebase), all legs served with YaRN 512K
(`LONGCTX=1`), MTP3, thinking on, temperature 0, `HF_HUB_OFFLINE=1`.

Checkpoints:

- EXL3: `wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-v1` @ `73a050c2`
- NVFP4 (nvidia): `nvidia/Qwen3.8-Flash-Next-NVFP4` @ `fc694b54` (ModelOpt
  MIXED_PRECISION: NVFP4 experts 48 layers, FP8 block-128 MTP experts,
  FP8 per-tensor PLE n-gram table in a dedicated shard; 132.7 GB).
  Content-identical to the pre-super-squash pin `2061e0b0`; the profile
  pin was re-pinned to `fc694b54` in `model-profiles.sh`.
- NVFP4 (RedHatAI): `RedHatAI/Qwen3.8-Flash-Next-NVFP4` @ `c8f2fb1b`
  (CompressedTensors: NVFP4 experts only; attention/dense/GDN/MTP BF16;
  102.5 GB BF16 PLE table host-offloaded; 174 GB). The `nvfp4` profile
  was temporarily pointed at this checkpoint for the leg (same convention
  as the core suite) and restored afterwards.

The nvidia checkpoint loads through the stock `ModelOptMixedPrecisionConfig`
path (FP8 PLE resolves via `quantized_layers` to
`Qwen4ExpPLEFp8EmbeddingMethod`); the RedHatAI checkpoint loads through the
`CompressedTensorsConfig` path fixed by `patches/port-nvfp4-ple-ct.py`
(unquantized PLE). No image rebuild was needed for either.

Harness: lm_eval 0.4.13, `local-chat-completions` + `--apply_chat_template`
+ `tokenized_requests=False` (server applies the Qwen3 chat template; the
reasoning parser strips thinking), `num_concurrent=16`,
`max_gen_toks=3072` (IFEval's task config caps generation at 1280), each
leg tokenized with its own checkpoint's tokenizer. 10-problem GSM8K smoke
10/10 on both NVFP4 legs before the battery.

Timing (UTC): EXL3 leg 09:02-09:22 (GSM8K 492 s, IFEval 689 s); nvidia leg
15:15-15:41 (GSM8K 445 s, IFEval 673 s); RedHatAI leg 16:11-16:43 (GSM8K
550 s, IFEval 862 s). Engine swapped between legs; EXL3 restored after
each.

## GSM8K (1319 problems, 5-shot, exact match)

| Filter | EXL3 | NVFP4 (nvidia) | NVFP4 (RedHatAI) |
|---|---|---|---|
| flexible-extract | 0.91964 +/- 0.00749 | 0.89538 +/- 0.00843 | 0.91964 +/- 0.00749 |
| strict-match | 0.91812 +/- 0.00755 | 0.89310 +/- 0.00851 | 0.91888 +/- 0.00752 |

Correct counts (flexible / strict): EXL3 1213 / 1211, nvidia 1181 / 1178,
RedHatAI 1213 / 1212.

Per-problem (flexible): EXL3 and RedHatAI agree on 1217/1319 with a
symmetric 51/51 swap — identical totals, different error sets (~102
problems, 7.7%). nvidia's gap is a net loss, not just different errors:
70 problems EXL3 gets right that nvidia misses, 38 the other way (net
-32). All three correct on 1110 (84.1%); none correct on 49 (3.7%).

## IFEval (541 prompts, 0-shot)

| Metric | EXL3 | NVFP4 (nvidia) | NVFP4 (RedHatAI) |
|---|---|---|---|
| prompt strict | 0.79852 +/- 0.01726 | 0.79113 +/- 0.01749 | 0.78743 +/- 0.01761 |
| inst strict | 0.80576 | 0.79856 | 0.79257 |
| prompt loose | 0.81885 +/- 0.01657 | 0.81331 +/- 0.01677 | 0.80407 +/- 0.01708 |
| inst loose | 0.81894 | 0.81295 | 0.80336 |

## Findings

- GSM8K: EXL3 and RedHatAI are statistically tied (identical flexible
  total; strict within 1 problem). nvidia NVFP4 is genuinely lower:
  -2.4-2.5 pts, a net loss of 32 problems outside the ~0.8 pt stderr.
- IFEval: EXL3 leads both NVFP4 builds by 0.6-1.5 pts (within stderr);
  nvidia edges RedHatAI by ~0.4-1.0 pts.
- The two NVFP4 builds trade differently: nvidia is the fastest
  (GSM8K 445 s / IFEval 673 s vs EXL3 492/689) but loses GSM8K quality;
  RedHatAI keeps EXL3-level GSM8K quality but is the slowest (550/862 s,
  +24%/+28% vs nvidia) — consistent with its 2x larger PLE table
  (102.5 GB BF16 vs 51.2 GB FP8 in pinned host).
- Consistent with the core suite's NVFP4 regressions (orchid exact
  repetition, seven) on both builds.

Per-leg results JSON: `exl3/`, `nvfp4/`, `rha/` (plus the NVFP4 smoke
gates). Per-sample generations were logged to
`/tmp/opencode/quality-deep/{exl3,nvfp4,rha}-{gsm8k,ifeval}/` (not
committed).
# True-KLD ladder and the engine-side keystone attempt (2026-10-17)

## Measured (tree-vs-tree, panel l0, 5120 rows, identical protocol)
| card | bits | true KLD | additive prediction | ratio |
|---|---|---|---|---|
| K3 uniform | 3.349 | 0.03592 | — | — |
| K4 uniform | 4.0 | **0.02086** | — | — |
| measured425 (l3 knapsack) | 4.2465 | 0.02149 | 0.01414 | 1.52 |
| measured459 (l3 knapsack) | 4.5868 | 0.01984 | 0.01377 | 1.44 |
| vendor425 (per-expert K4/K5) | 4.25 | **0.01433** | — | — |
| K5 uniform | 5.0 | **0.01412** | — | — |

UPDATE 18:33 (same day): the keystone became measurable after all. The tree
blocker was MultiLinear's uniform-BITRATE fusion assert (block_sparse_mlp.py
fuses all experts of a projection into one batch regardless of rate spread);
patches/port-exl3-dense-moe.py adds EXV3_FORCE_DENSE=1, which routes quantized
MoE through the fork's own documented dense per-expert fallback (identical
math, ~same wall time: 14 min). Vendor true KLD = 0.01433 at 4.25 bits —
31% BETTER than uniform K4 and 33% better than measured425 at the same budget.
Published per-expert allocation genuinely works; my panel-knapsack l3 solve
genuinely does not (0.0215 is worse than plain K4). The engine-side attempt
below stands as the record of why THAT route was dead; the number came home
through the converter tree.

UPDATE 18:47: K5 uniform true KLD = 0.01412 at 5.0 bits. Corrected reading:
vendor's per-expert allocation is a BITS-SAVING trick (matches uniform K5's
quality at 85% of the bits), not a quality trick - it does not beat what you
get by just buying bits. K6V could not be measured: the req6b card was built
without layers.*.ple.ple_embedding.layer_multipliers (tree loader requires it,
engine loader tolerates it; it passed previously as a measurement ALT because
alt tensors are keyed to the base's module tree). Not rebuilt: top of curve,
least informative point.

Headline (pre-keystone, superseded for vendor but still true for measured):
**plain uniform K4 (4.0 bits) beats measured425 (4.25 bits)**. The
additive knapsack model is 1.4–1.5x optimistic AND, at these budgets, the
projection-level allocation freedom it optimizes is worth less than simply
raising the uniform rate. The measured-card program is dead on quality grounds,
independent of the b12x tier-policy blocker (docs/engine-tier-capability.md).

## Why vendor425 cannot be measured locally
1. exv3 converter tree: `multilinear.py:26,76` assert uniform K per fused group ->
   per-expert mixed-K cards (vendor granularity) cannot load as measure base.
2. vLLM engine route (attempted, GPU1-only per owner directive): harvested
   `prompt_logprobs` (top-20) for the 64 panel rows on vendor@TP1 and K4@TP1,
   compared against BF16 tree logits (scripts/sc/bf16-logitdump.py,
   kl-engine-harvest.py, kl-estimate.py). The estimator calibrated against K4's
   known tree KLD produced 74x bias. Diagnosis chain:
   - alignment verified correct (argmax agreement 60% vs 11% shifted);
   - temperature=0 hypothesis rejected (not delta-dominated, top-1 mean 0.76);
   - units verified nats (top-20 exp-mass 0.948);
   - **true-token test is dispositive: tree-BF16 mean true-token logprob -1.97
     vs -2.91 (K4 engine) and -2.92 (vendor engine)**. Quantized cards cannot be
     1 nat/token WORSE than a no-PLE tree reference while passing every gate;
     engine-side prompt_logprobs carry a systematic (likely PLE-conditioning or
     logit-collection) corruption common to both cards. Absolute engine KL is
     therefore unusable, and "bias-corrected" vendor values from this pipeline
     are artifacts.
3. BF16 on the engine is physically impossible here: checkpoint 336 GiB > 2 x 95.6 GiB.

## Salvageable relative statement (common-mode cancels)
Engine-vs-engine, identical pipeline, 64 rows: KL(K4||vendor)=0.0571,
reverse 0.0606. With KL(BF16||K4)=0.0209, vendor lands in the same ~0.02 band
as uniform K4 — nowhere near the additive model's implied 0.014.
Consequence for T8: the serve-default question is not "vendor vs measured" —
every 4-4.6 bpw allocation is behaviorally interchangeable on our gates, and the
cheapest member of the band (uniform K4, 4.0 bits, KLD 0.0209) is also the
cheapest to serve. Vendor 4.25 remains default; no rebuild is justified.

## Artifacts (post-cleanup 2026-10-17)
Host-side scratch was deleted with the experiment cleanup; the durable record
lives in git:
- `benchmarks/kld-receipts-20261017/` - gzipped raw measure_model JSONs
  (panel l2/l3, vanilla l3, all basekld-* incl. vendor dense-path, analyses)
  and the consolidated build.log.
- `quant/recipe-*.yaml.gz`, `patches/`, `scripts/sc/` - everything needed to
  regenerate any number here (panel corpus retained on burst at
  `calibration/agentic-quant-panel-v1/`, sha256 `6cf142dc...f644b`).
- Deleted unrecoverably (by design, artifacts of the rejected engine route):
  `bf16-logits.pt`, `engine-lp-{k4,vendor425}.pt`, all built experiment cards
  (ladder, vanilla, measured, tuned, ref-rebuilds), BF16 checkpoints on both
  hosts. Vendor K4.25-v1 + PLE-FP8-v1 snapshots are untouched production assets.

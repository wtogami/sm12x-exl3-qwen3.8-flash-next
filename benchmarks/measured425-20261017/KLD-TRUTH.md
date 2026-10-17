# True-KLD ladder and the engine-side keystone attempt (2026-10-17)

## Measured (tree-vs-tree, panel l0, 5120 rows, identical protocol)
| card | bits | true KLD | additive prediction | ratio |
|---|---|---|---|---|
| K3 uniform | 3.349 | 0.03592 | — | — |
| K4 uniform | 4.0 | **0.02086** | — | — |
| measured425 (l3 knapsack) | 4.2465 | 0.02149 | 0.01414 | 1.52 |
| measured459 (l3 knapsack) | 4.5868 | 0.01984 | 0.01377 | 1.44 |
| vendor425 (per-expert K4/K5) | 4.25 | not locally measurable | — | — |

Headline: **plain uniform K4 (4.0 bits) beats measured425 (4.25 bits)**. The
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

## Artifacts (all byte-stable, regenerable)
- sc-measure/basekld-{vendor425(failed),measured425,measured459,k4}.json
- sc-measure/bf16-logits.pt (top-64+lse dump of the 64-row panel stream)
- sc-measure/engine-lp-{vendor425,k4}.pt (harvested top-20 prompt_logprobs)
- snapshots/k4 symlink (req4-build/out) in the K4.25-v1 repo dir for engine boots
- scripts/sc/{bf16-logitdump,kl-engine-harvest,kl-estimate}.py + gpu0-kltree.sh

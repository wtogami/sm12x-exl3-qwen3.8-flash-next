# Measured-Allocation Quant Cards: Final Report and Abort Decision (2026-10-17)

**Bottom line:** We tried to build EXL3 cards that beat (or match-at-fewer-bits)
the vendor `Qwen3.8-Flash-Next EXL3 K4.25-v1` card by *measuring* per-group
quantization quality on a calibration panel and solving a bits-for-KLD
knapsack. The program is closed without a shippable result: our allocations
underperform plain uniform quantization at the same bits, the vendor's quality
could not be reproduced below ~5.0 bits, and the true-KLD ladder proves the
quality band above ~4.25 bits is behaviorally flat. The vendor card remains the
serving default at TP1 and TP2. Full decision record:
[`benchmarks/decision-20261015.md`](../benchmarks/decision-20261015.md)
(evening entry, 2026-10-17).

## What was tried

### 1. Fixing the measurement pipeline (Oct 16-17)
Earlier measurement attempts produced all-zero quality deltas. Root cause was
three stacked defects, now fixed and committed
(`patches/port-exl3-measure-ovr.py`):

1. `BlockSparseMLP.forward` fused/graph/batched tiers bypassed the per-expert
   `EXL3Linear.forward` where measurement overrides delegate (the dsv4 module
   had a `converting` guard; block_sparse_mlp did not).
2. 48 shared-expert groups were FP16 in the base card — structurally
   unmeasurable zeros; filtered by trellis-tensor presence.
3. No tripwire — the measuring tool now self-checks that two candidate bitrates
   diverge and aborts on all-zero output.

### 2. A better calibration corpus
Measurements and conversions use the panel corpus (the vendor author's claimed
calibration, packed to
`calibration/agentic-quant-panel-v1/panel-qwen-packed-2048.safetensors`,
sha256 `6cf142dc…f644b`, regenerable from the public HF dataset via
`scripts/req6-pack-panel-calibration.py`).

### 3. Measure → solve → build
True KL-vs-BF16 was measured for every quantization group at two granularities
(l2 layer-uniform, 48 groups; l3 per-projection, 96 groups) across K4/K5/K6
alternatives, and a Lagrangian knapsack (verbatim copy of upstream
`optimize_model`) allocated bits at budgets 3.7-5.65. Two l3 cards were built
and byte-verified: **measured425** (achieved 4.2465 bits) and **measured459**
(4.5868 bits). Recipes, build scripts, and provenance notes are committed.

### 4. Both cards failed to boot — and why (T2 audit)
The engine rejects them at weight prep: `projection-mixed EXL3 requires exactly
two consecutive K2-K6 tiers, got (3, 5)`. Source audit
([docs/engine-tier-capability.md](engine-tier-capability.md)) shows this is
**kernel-library policy, not adapter code**: b12x `fused_moe/trellis.py`
enforces consecutive tiers and hardcodes the canonical `(3,4,5)` family on the
path our TP2+EP serving uses; the vendor card only boots because its mix lands
exactly on the canonical adjacent pair `{4,5}`. The mapped escape (rank-sliced
format path, ~1.5 days of patching) unlocks *only our own allocations*, which
the KLD ledger below had already shown to be losers — parked indefinitely.
The emitter now enforces the engine contract at recipe time
(`ENGINE_MODE`/`TIER_WHITELIST` in `scripts/sc/emit-recipe.py`), so this class
of failure can't recur.

### 5. True-KLD ground truth (the keystone measurements)
For the first time, absolute lm_head KL vs BF16 on panel text was measured for
every card class (identical protocol, 5120 rows; final ledger:
[`benchmarks/measured425-20261017/KLD-TRUTH.md`](../benchmarks/measured425-20261017/KLD-TRUTH.md)):

| card | bits | true KLD |
|---|---|---|
| K3 uniform | 3.349 | 0.03592 |
| K4 uniform | 4.0 | 0.02086 |
| measured425 (l3 knapsack) | 4.2465 | 0.02149 |
| **vendor 4.25** | 4.25 | **0.01433** |
| measured459 (l3 knapsack) | 4.5868 | 0.01984 |
| K5 uniform | 5.0 | 0.01412 |

To measure the vendor card itself we had to add a runtime escape hatch
(`patches/port-exl3-dense-moe.py`, `EXV3_FORCE_DENSE=1`): its per-expert
mixed-K layout trips the converter runtime's uniform-bitrate fusion assert,
and the fork's documented dense per-expert fallback handles it. An attempt to
measure via engine `prompt_logprobs` instead was killed by a ~1 nat/token
common-mode artifact in that path (diagnosis in KLD-TRUTH.md) — the tree route
is the reliable instrument.

## Why we are aborting

1. **The quality band is flat.** Uniform K5 (5.0 bits) is statistically
   indistinguishable from vendor (4.25 bits) on true KLD (0.01412 vs 0.01433),
   and gates + x20 defect probes put even 6-bit K6V in the same behavioral
   wash. Extra bits buy nothing observable.
2. **Reproduction failed.** Our measured allocations score 0.0215/0.0198 at
   4.25/4.59 bits — worse than plain uniform K4 (0.0209 at 4.0). The additive
   first-order model the knapsack relies on is 1.4-1.5x optimistic *and*
   directionally harmful at this scale; vendor's per-expert allocation could
   only be matched by paying uniform-K5 bits.
3. **Vendor is a geometric sweet spot, not just "good enough."** It is the
   only point in the quality band that fits TP1 with confirmed 512K context
   (73.8 GiB GPU-side). K5 needs ~80 GiB of weights and K6V ~93.8 GiB — no
   single-card 512K for either.
4. **Remaining unlocks have negative value.** The multitier/EP-elimination
   engine work would serve only allocations we know are worse than uniform.
   The one experiment still logically alive (per-expert-granularity
   measurement, validated against vendor's 0.01433 ground truth) targets
   *sub-4.25-bit cost savings*, which no current fleet constraint demands.

## Kept as assets

- True-KLD ledger + protocols (`basekld-*.json`, `scripts/sc/baseline-kl.sh`,
  `gpu0-kltree.sh`) — the definitive quality instrument for any future card.
- Engine tier-capability contract + capability-aware emitter (boot-safe by
  construction).
- Dense-path measurement patch (vendor-class cards measurable forever after).
- measured425/measured459 cards + recipes as offline artifacts
  (`MEASURED-*-KEPT.md`), plus `quant/recipe-measuredl2-425.yaml.gz` (a
  bootable l2 recipe, solved pre-verdict).
- Panel calibration corpus with hash-pinned provenance.

## Fallback posture

If the vendor snapshot ever becomes unavailable: **uniform K5 @ TP2**
(quality-equivalent at +18% expert VRAM, TP1 512K lost) with K6V as the
TP2-only quality reserve. The vendor snapshot itself is hash-pinned in the
repo dir; re-verify against `PROVENANCE` before any redeploy.

# Long-context quality measurements (2026-10-21/22 overnight campaign)

Five instruments run against the qualified v0.32.0 image (`exl3`, TP2,
2x300 W, FP8 KV, MTP3, greedy C1) under both serving configurations:
**native** (`LONGCTX=0`, 262,144) and **YaRN** (`LONGCTX=1`, 524,288,
factor 2.0 untuned). Same seeds across arms so overlapping depths pair.
Receipts: `benchmarks/{multineedle-native-20261021,babilong-20261021,nolima-20261021,table-position-20261022}/`.

## Results by capability

**1. Exact retrieval (literal key -> hex value), N=25, K needles:**
perfect (1.000) at every depth to 524,288 with K up to 8, under both
configs. A single worst-case cell (524K x K=8, N=25) closed the ladder.

**2. Multi-fact reasoning (BABILong, N=20, strict upstream scorer).**
Accuracy by length; `y` = YaRN arm, unmarked = native (only the native arm
runs below 393K since 512K is outside its window). Bases at 0k match
between arms.

| task (base) | 8k | 64k | 128k | 256k | 512k |
|---|---|---|---|---|---|
| qa1 latest-position (1.00) | 0.90 / y0.90 | 0.90 / y0.90 | 0.75 / y0.70 | 0.75 / y0.90 | y0.65 |
| qa2 two-fact (0.90) | 0.65 / y0.60 | 0.55 / y0.50 | 0.50 / y0.55 | 0.25 / y0.20 | y0.40 |
| qa3 three-fact (0.75) | 0.55 / y0.55 | 0.30 / y0.35 | 0.40 / y0.45 | 0.55 / y0.50 | y0.20 |
| qa4 relations (0.95) | 0.70 / y0.70 | 0.80 / y0.65 | 0.70 / y0.80 | 0.60 / y0.70 | y0.70 |
| qa5 chains (0.95) | flat 0.95 both arms, all lengths | | | | |
| qa7 counting (0.45) | 0.40 / y0.40 | 0.20 / y0.20 | 0.10 / y0.15 | 0.15 / y0.10 | y0.05 |

Reasoning-class tasks cross the 85%-of-base threshold between 0k and 64k;
native and YaRN pairs are statistically indistinguishable everywhere.
qa5 (few-fact chains) is length-invariant; qa7 (counting) is the sharpest
decay after ordinal arithmetic.

**3. Associative retrieval, zero lexical overlap (NoLiMa released set,
native arm, N=290/length):** base(1K) 0.807 -> 8K 0.528 -> 16K 0.421 ->
32K 0.338 -> 64K 0.307 -> 128K 0.128. Effective length (85%-of-base
convention) ~1-2K, mid-pack versus the published table (most models 1-8K;
GPT-4.1 16K). Depth slice: decay is a LENGTH effect -- per-depth accuracy
is flat to within noise (no lost-in-the-middle U at these scales).

**4. Position binding vs position counting (NIAT-style table probe,
N=20):** querying cells by (row, column) where rows carry id labels:
1.000 presence AND order-aligned at every length in both configs,
including 524,032 tokens / 9,124 rows. Removing row ids so "row N" means
the Nth row (counting required): 0.58/0.40 at 75 rows (4K), 0.15 at 310,
~0 beyond 1,200 rows, identical between arms (parity at every length).
Local position binding is flawless; sequential counting collapses within
thousands of elements -- the same weakness BABILong qa7 measures.

**5. Phase 2 -- N=50 resamples, fine-depth ladder, tuned-YaRN comparison
(2026-10-22, receipts `benchmarks/quality-phase2-20261022/`).** Two serving
lanes in parallel: burst TP2 @2x400 W and gg TP1 @350 W (quality is
power/topology-invariant per section 1-4 parity checks; paired arm-vs-arm
cells share one host where possible). Generator: BABILong create_tasks
pipeline over the PG19 test books, sizes converted to our token space with
the measured ratio 0.975; per-file achieved lengths in `calibration.json`.

Quotable cells at N=50 confirm phase-1 shapes: native qa1 0.94/0.92/0.86
(8k/64k/128k), qa2 0.50/0.44/0.40, qa3 0.40/0.36/0.28 -- the qa1-vs-multi-fact
gap is real, not screening noise. qa7 counting stays weak at BOTH lengths
(0.42/0.38), matching the counting-collapse finding. YaRN arm at the official
512k bin: qa1 0.72, qa2 0.24. The earlier qa2 inversion (0.40 at N=20)
regressed to the trend line at N=50 as expected from the CI note. Table probe
@524,032 reproduced exactly: labeled presence/aligned 1.00/1.00 (n=50),
ordinal presence 1.00 / aligned 0.00. NoLiMa bracket: 2K 0.74-0.83 across
depths, 4K 0.62-0.72 -- flat-vs-depth again, level-shift between lengths:
the length effect, not a position effect.

Fine ladder (generated depths in our tokens, qa1/qa4, N=10, A = shipping
YaRN f2.0):

| depth | A qa1 | A qa4 | B qa1 | B qa4 | C qa1 | C qa4 |
|---|---|---|---|---|---|---|
| 294k | 0.90 | 0.70 | 0.30 | 0.30 | 0.80 | 0.70 |
| 327k | 0.80 | 0.90 | 0.10 | 0.50 | 0.40 | 0.60 |
| 360k | 0.80 | 0.80 | 0.10 | 0.20 | 0.50 | 0.50 |
| 393k | 0.70 | 0.80 | 0.10 | 0.10 | 0.00* | 0.10* |
| 458k | 0.40 | 0.60 | 0.00 | 0.20 | -- | -- |
| 491k | 0.70 | 0.70 | 0.10 | 0.10 | -- | -- |

\*C cells at its own window edge: many samples exceed 393,216 tokens and are
counted skips (answered=3/10). Between the phase-1 bins the curve is gently
declining, no additional cliff between 294k and the official 512k bin; the
512k N=50 anchors (qa1 0.72) sit on the trend, not below it.

Arm comparison (same files, same stride subsets, paired cells): shipping
untuned A scores >= tuned B in **all twelve** pairs (median gap ~0.4), plus
both 512k anchors. The mscale=1.72 attention_factor is worse than the YaRN
formula default on this model; keep the shipping config. A-vs-C is close on
overlapping depths; window-choice (393K vs 524K) stays an ops/concurrency
decision rather than a quality one. TTFT differs by lane as expected from TPS
topology (burst ~30-53s @524K vs gg ~47-81s); scores do not.

## Findings

- **The headline context number is capability-dependent.** "512K context"
  is literally true (perfect needle + table-cell retrieval across it) and
  practically misleading (multi-fact reasoning crosses its threshold by
  ~8-64K regardless of config; associative retrieval by ~2K).
- **No YaRN tax was detectable in-window on any of four mechanisms**
  (retrieval, reasoning, association-independent config, position). The
  tuned-vs-untuned follow-up now closes it: the shipping f2.0 config beats
  the hand-tuned attention_factor variants on every paired deep cell, so
  the default is the tuned-for-this-model choice, not a placeholder.
- **Counting/ordinal arithmetic is the model's sharpest weakness**
  (two independent instruments, floor by ~300 sequential elements). Worth
  stating in user docs; avoid workflows that require enumerating items.

## Default-window implication

Native-default (`LONGCTX=0`) remains recommended, now on evidence rather
than priors -- via OPERATIONAL grounds, not quality: no in-window quality
difference exists to win, while native buys 3.73x vs 1.86x full-context
concurrency at util 0.94, halves worst-case TTFT, and removes the
>262,144-only crash surface (#62383/#62409). `LONGCTX=1` stays as
documented opt-in whose proven value is literal retrieval over 262K-512K
documents.

## Caveats and follow-ups

- Screening cells with +-15pp CIs were resampled at N=50 for the quotable
  set (phase 2 above); remaining open: qa2 at 256k/512k with larger N (the
  shallow-vs-deep non-monotonicity in qa1 @458k/@491k is within N=10 noise
  and only matters if a per-bin effective-length number gets quoted), and
  qa7 past 64k. Intermediate attention_factor values (~1.2-1.5) untested;
  the 12/12 paired win of the default over 1.72 suggests the formula value
  is already near-optimal for this checkpoint.
- NoLiMa base (0.81) is itself mid-pack, so its effective length mixes
  task-fluency and length-decay (their base-score convention is applied).
- BABILong/NoLiMa token lengths come from the released files; our
  tokenizer's counts differ slightly (recorded per sample in receipts).
- Fine-grained depth: BABILong ships a generator; regenerating official
  bins + bisecting inside the 8-128K zone is the next refinement.
- The table probe's ordinal arm floors below YaRN-relevant lengths; it
  cannot test scaling taxes past ~16K (pairing at 4K/16K is parity).

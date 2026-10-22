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

## Findings

- **The headline context number is capability-dependent.** "512K context"
  is literally true (perfect needle + table-cell retrieval across it) and
  practically misleading (multi-fact reasoning crosses its threshold by
  ~8-64K regardless of config; associative retrieval by ~2K).
- **No YaRN tax was detectable in-window on any of four mechanisms**
  (retrieval, reasoning, association-independent config, position). This
  does NOT generalize to tuned-vs-untuned YaRN arms or to mscale ablations
  (still parked), but the shipping YaRN config is not taxing native-window
  quality on these instruments.
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

- N=20/25 screening: +-15pp CIs; isolated inversions (qa2 256k vs 512k)
  are inside noise. Quote-worthy cells need an N=50 pass: BABILong
  qa1/qa2/qa3 at 8k/64k/128k both arms; qa7 across lengths; NoLiMa 2K/4K
  to bracket the effective-length crossing; table-labeled 524K.
- NoLiMa base (0.81) is itself mid-pack, so its effective length mixes
  task-fluency and length-decay (their base-score convention is applied).
- BABILong/NoLiMa token lengths come from the released files; our
  tokenizer's counts differ slightly (recorded per sample in receipts).
- Fine-grained depth: BABILong ships a generator; regenerating official
  bins + bisecting inside the 8-128K zone is the next refinement.
- The table probe's ordinal arm floors below YaRN-relevant lengths; it
  cannot test scaling taxes past ~16K (pairing at 4K/16K is parity).

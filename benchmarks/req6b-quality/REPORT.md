# Experiment B — uniform-K6 mcg with agentic-quant-panel-v1 calibration

Single variable vs experiment A: calibration corpus (panel packed file instead of
vendor-default mix). Same recipe (uniform K6 experts, mcg, bf16 dense), same repairs,
same YaRN-524K profile, MTP3, CUDA graphs. Boot facts in boot.json
(KV 4,775,271 tok / 9.11x; acceptance 55.6%).

Gate scores (measurement rows only):

| suite     | base K4.25 | A (vendor cal) | B (panel cal) |
|-----------|-----------:|---------------:|--------------:|
| biomed    |      13/13 |          13/13 |         12/13 |
| api-tools |      16/16 |          16/16 |         16/16 |
| retrieval |        6/6 |            6/6 |           6/6 |
| seven     |      20/21 |          19/21 |         19/21 |
| orchid    |        5/5 |            2/5 |           4/5 |

Orchid word-count occurrences per run — base [100,100,100,100,100],
A [100,99,99,99,100], B [100,99,100,100,100].

Verdict: ORCHID_PARTIAL (2/5 -> 4/5 toward base; the off-by-one word-count bias
shrinks but does not vanish under panel calibration — remaining gap belongs to rate,
not corpus). New in B: biomed A3-summarize fails reproducibly (runs in both files):
completion = 3072 reasoning-only tokens, truncated, empty visible answer, while A
summarizes in ~1285 tokens with visible text. Longer chain-of-thought under panel
rounding costs one biomed task at fixed max_tokens.

Serving decision: revert to A (biomed is the primary domain gate; A: biomed 13/13,
orchid 2/5 = 56/60; B: biomed 12/13, orchid 4/5 = 57/60 overall, but biomed-heavy
workloads favor A). Owner may override.

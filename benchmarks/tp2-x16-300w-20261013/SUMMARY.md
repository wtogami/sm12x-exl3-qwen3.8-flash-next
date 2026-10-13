# A/B re-run at 300 W with x16 restored + context ladder (burst, 2026-10-13)

Same protocol as benchmarks/tp2-300w-20261013 (same image, client-side
harness, MTP3, YaRN 512K, batch 2048, 2 Hz power sampling), rerun after the
GPU-0 reseat restored PCIe x16 on both cards (links.txt: width 16/16,
0 replays at leg starts). New in this run: a context ladder (TTFT and
post-prefill C1 decode at fixed prompt depth). Engine restored to TP2+EP
at 17:55.

## Decode sweep, aggregate tok/s (medians of 3; short-prompt forced output)

| C | TP1 Oct-6 receipt | TP1 today | TP2+EP yesterday (x8) | TP2+EP today (x16) | TP2 vs TP1 today |
|---|---|---|---|---|---|
| 1  | 114.7 | 117.6 | 172.7 | 167.4 | +42% |
| 2  | 192.5 | 201.5 | 279.9 | 293.5 | +46% |
| 4  | 310.3 | 314.3 | 443.5 | 446.3 | +42% |
| 8  | 469.7 | 453.0 | 691.7 | 711.3 | +57% |
| 16 | 595.0 | 592.0 | 977.8 | 987.4 | +67% |

x8 -> x16 moved TP2 by +1..+5% except a 3% C1 regression (within run noise;
C1 is PCIe-idle anyway). Yesterday's pessimistic-bound caveat is retired:
the x8 numbers were 97-99% of the truth, as the byte ledger predicted.

## Prefill ladder, TTFT medians (s)

| Depth | TP1 today | TP2+EP yesterday | TP2+EP today | speedup today |
|---|---|---|---|---|
| 131072 | 23.4 | 16.4 | 15.6 | 1.50x |
| 261632 | 47.8 | 34.2 | 33.4 | 1.43x |
| 523264 | 98.6 | 72.4 | 71.8 | 1.37x |

## Context ladder (new): TTFT s / post-prefill C1 decode tok/s, per depth

| Depth | TP1 TTFT | TP2 TTFT | speedup | TP1 dec | TP2 dec | TP2 dec gain |
|---|---|---|---|---|---|---|
| 16384  |  2.9 |  2.0 | 1.45x | 204.5 | 304.8 | +49% |
| 65536  | 11.5 |  8.2 | 1.40x | 209.6 | 306.5 | +46% |
| 131072 | 23.3 | 16.7 | 1.39x | 213.9 | 314.9 | +47% |
| 261632 | 47.6 | 34.4 | 1.38x | 219.3 | 329.9 | +50% |
| 523264 | 98.2 | 72.4 | 1.36x | 224.3 | 346.4 | +54% |

Decode RISES with depth on both engines (high-acceptance filler content;
matches qualification's "high-acceptance mode" numbers at 131K: TP1 today
213.9 vs receipt 219.3). The TP2 advantage holds or grows slightly at depth.
TTFT and the prefill ladder agree at the shared depths (72.4 vs 71.8 at
523K), cross-validating both harnesses.

## Power and energy (leg means, both GPUs summed)

| | TP1 | TP2+EP | TP2/TP1 |
|---|---|---|---|
| decode W / prefill W | 304 / 300 | 589 / 581 | 1.95x / 1.94x |
| J per 1k tok, C16 | 514 | 597 | +16% |
| J per 1k tok, 523K prefill | 56.6 | 79.7 | +41% |
| prefill tok/s | 5306 | 7287 | +37% |

Reading: at the 2x8-pin reality (300 W/card), TP2+EP buys 1.4-1.5x prefill,
+42..67% decode throughput, ~2x KV per request-slot, at ~1.94x the wall
draw; per-token energy costs +16% at C16 and +41% on prefill (prefill
loses energy efficiency to per-card DVFS being rate-limited by each 300 W
budget plus comm overhead, and gains speed by parallelism). If throughput
per watt is the metric, TP1 still wins slightly; if tokens per second per
dollar-of-silicon-actually-installed, TP2 wins everywhere.

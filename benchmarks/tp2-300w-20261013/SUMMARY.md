# EXL3 TP=2+EP vs TP=1 at matched 300 W limits (burst, 2026-10-13)

Same host, same image (tp2-experimental d8d54dc), both GPUs enforced
300.00 W; client-side harness identical to power-300w-20261006 (MTP3,
YaRN 512K, batch 2048, 256-token decode outputs, 3 runs/level, 2 Hz
power sampler). CAVEAT: GPU0 (bus 51) ran the whole session at PCIe
x8 (max x16, 936 replays) - a healthy pair may shift numbers; re-run
after reseat before publishing.

## Decode, aggregate tok/s (medians)
| C | TP=1 receipt 10-06 | TP=1 today | TP=2+EP today | TP2 vs TP1 today |
|---|---|---|---|---|
| 1  | 114.7 | 118.9 | 172.7 | +45% |
| 2  | 192.5 | 199.0 | 279.9 | +41% |
| 4  | 310.3 | 308.3 | 443.5 | +44% |
| 8  | 469.7 | 478.8 | 691.7 | +44% |
| 16 | 595.0 | 610.3 | 977.8 | +60% |

Same-day TP=1 control tracks the 10-06 receipts within ~3%: image drift
is negligible; cross-day comparison is safe on this host.

## Prefill, TTFT seconds (medians)
| Depth | TP=1 today | TP=2+EP today | speedup |
|---|---|---|---|
| 131072 | 22.5 | 16.4 | 1.37x |
| 261632 | 46.1 | 34.2 | 1.35x |
| 523264 | 95.7 | 72.4 | 1.32x |

## Power / energy (leg means, both GPUs summed)
| | TP=1 | TP=2+EP |
|---|---|---|
| decode W | 321 (gpu0 297) | 589 (294+295) |
| prefill W | 316 | 580 |
| decode MHz | 1642 | 2587 |
| prefill MHz | 1237 | 2235 |
| J/1k tok C16 | 526 | 602 |
| J/1k prefill 523K | 57.8 | 80.2 |

Reading: at the 300 W cable reality, TP2+EP is strictly faster than the
TP1 you can actually run (there is no 600 W single-card option right
now), at ~1.15-1.39x the energy per token; each card sits at a higher
DVFS point (2235-2587 MHz vs 1237-1642) because per-card work halves
while the cap does not. Context per card unchanged (weights+KV halve;
engine reported KV 6,294,289 tokens at 0.94).

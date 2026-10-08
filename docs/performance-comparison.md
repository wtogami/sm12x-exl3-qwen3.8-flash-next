# Performance Comparison: EXL3 vs NVIDIA NVFP4 vs RedHatAI NVFP4

Three quantizations of **Qwen3.8-Flash-Next** ([base model](https://huggingface.co/Qwen/Qwen3.8-Flash-Next))
measured head-to-head on one RTX PRO 6000 Blackwell (96 GB GDDR7, 4x8-pin,
600 W default limit) on 2026-10-07, all under one serving stack: engine
image `8294c3c914c0` (vLLM v0.31.0 fork), MTP3 speculative decoding, YaRN
512K context, 2048-token batch budget, plus a fourth checkpoint
(`exl3-ple8`) as a PLE-format ablation. Power measurements use a 2 Hz
on-card sampler with exact run-window attribution. The companion quality
verdict is [quality-comparison.md](quality-comparison.md).

**Headline: both NVFP4 builds prefill 20-30% faster than EXL3 at every
power level; single-stream decode is a three-way tie at 600 W. At C16 the
fresh four-way sweep gives NVIDIA the lead (992 tok/s vs EXL3 856), with
RedHatAI the only build that fails to scale (595). NVIDIA wins size and
energy (prefill energy/token runs 14-31% under EXL3's best operating
point). SM clock-lock sweep: locks Pareto-dominate power caps for the
NVFP4 builds and roughly tie for EXL3; decode energy drops ~40% at a
1300-1600 MHz lock.**

## Checkpoints

| Quant | HF card (measured revision) | Size | Weight footprint |
|---|---|---|---|
| EXL3 K4.25 v1 | [wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-v1](https://huggingface.co/wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-v1) [`73a050c`](https://huggingface.co/wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-v1/tree/73a050c27b8c488c65acd6d1c74e45ff02be5fab) | 179.4 GB | mixed K4/K5 EXL3 experts + BF16 non-experts on GPU; ~102 GB (95 GiB) BF16 PLE table host-mmap — same table as RHA |
| NVIDIA NVFP4 | [nvidia/Qwen3.8-Flash-Next-NVFP4](https://huggingface.co/nvidia/Qwen3.8-Flash-Next-NVFP4) [`fc694b5`](https://huggingface.co/nvidia/Qwen3.8-Flash-Next-NVFP4/tree/fc694b54fb0174e0913e6adf86691ef85a4ead47) | 132.7 GB | NVFP4 main experts on GPU; FP8 PLE 51.2 GB pinned host |
| RedHatAI NVFP4 | [RedHatAI/Qwen3.8-Flash-Next-NVFP4](https://huggingface.co/RedHatAI/Qwen3.8-Flash-Next-NVFP4) [`c8f2fb1`](https://huggingface.co/RedHatAI/Qwen3.8-Flash-Next-NVFP4/tree/c8f2fb1b9869f686b214782036123b10ff96d14a) | 174 GB | NVFP4 experts + BF16 non-experts ~81 GB on GPU; 102.5 GB BF16 PLE host-offloaded |
| exl3-ple8 (ablation) | [wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-PLE-FP8-v1](https://huggingface.co/wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-PLE-FP8-v1) [`888306b`](https://huggingface.co/wrldsuksgo2mars/Qwen3.8-Flash-Next-EXL3-K4.25-PLE-FP8-v1/tree/888306bd3996d6317758c07df50622829259ad17) | 128.3 GB | EXL3 K4.25 experts (same as row 1); **FP8 PLE** ~48 GiB host-mmap — the PLE-format control for EXL3 |

## Power ladder (YaRN 512K, MTP3, C1; same day 2026-10-07)

TTFT seconds at 131K / 523K prompt depth, and post-prefill C1 decode
tok/s, at four power limits:

| Power | EXL3 131K | EXL3 523K | EXL3 dec | nvidia 131K | nvidia 523K | nvidia dec | RHA 131K | RHA 523K | RHA dec |
|---|---|---|---|---|---|---|---|---|---|
| 600 W | 13.1 s | 57.1 s | 261 | **9.5 s** | **42.9 s** | 261 | 9.7 s | **43.3 s** | 262 |
| 450 W | 15.5 s | 69.1 s | 260 | 11.3 s | 49.7 s | 263 | 11.0 s | 49.6 s | 264 |
| 330 W | 21.1 s | 89.5 s | 244 | 14.4 s | 62.9 s | 259 | 13.9 s | 62.8 s | 251 |
| 300 W | 22.3 s | 97.8 s | 226 | 15.6 s | 68.4 s | 248 | 15.4 s | 68.8 s | 242 |

- **Prefill: both NVFP4 builds beat EXL3 at every level** (native FP4
  expert GEMMs on Blackwell); 131K TTFT 9.5 s vs 13.1 s, 523K 42.9 s vs
  57.1 s at 600 W. The two NVFP4 builds are within 0.5 s of each other
  everywhere.
- **Decode: a wash at 600 W** (~261 tok/s all three); under caps decode
  degrades least for nvidia (−5.0% 600→300 W), then RHA (−7.6%), then
  EXL3 (−13.4%).
- **nvidia at 300 W prefill-matches EXL3 at 450 W** (68.4 vs 69.1 s at
  523K) — same speed, 150 W less. The 330 W cap is the classic knee:
  ~7% faster than 300 W for ~10% more power.

## SM clock-lock sweep (both engines, 600 W limit inert, 2 Hz power sampling)

`nvidia-smi -lgc` as the primary control instead of the power cap. The
lock is a **ceiling, not a pin**: above the power-cooled ceiling
(~2330 MHz EXL3, ~2590 MHz nvidia) locks throttle back to free-running
behavior, so only the band below the ceiling is an independent knob.
Key operating points (sustained short-prompt C1 decode; prefill energy
J/1k = measured mean-window W x TTFT):

| Engine | Point | eff MHz | 523K TTFT | prefill W | **J/1k prefill** | decode tok/s | decode W |
|---|---|---|---|---|---|---|---|
| EXL3 | free-running | 2365 | 56.0 s | 580 | 62.1 | 132.1 | 515 |
| EXL3 | lock 2200 | 2107 | 60.2 s | 512 | 58.9 | 123.2 | 322 |
| EXL3 | lock 1600 | 1590 | 75.8 s | 375 | 54.4 | 106.4 | 256 |
| EXL3 | lock 1300 | 1297 | 89.4 s | 317 | **54.1** | 95.7 | 228 |
| nvidia | free-running | 2616 | 42.1 s | 575 | 46.3 | 136.0 | 493 |
| nvidia | lock 2200 | 2170 | 46.5 s | 462 | 41.1 | 129.8 | 316 |
| nvidia | lock 1600 | 1590 | 57.7 s | 339 | 37.4 | 117.2 | 256 |
| nvidia | lock 1300 | 1297 | 67.2 s | 291 | **37.3** | 105.9 | 231 |

- Prefill energy has a **flat basin at 1300-1600 MHz**: −12.8% (EXL3) /
  −19.3% (nvidia) J/token vs free-running; locking below the basin costs
  TTFT almost linearly with no further energy win.
- **Decode energy is the big lock win**: ~40% less J/token at 1300-1600
  MHz for 26-28% less speed; the 2200 lock keeps ~94% of decode speed at
  ~2/3 of the decode power.
- **Locks vs caps**: for the NVFP4 builds a lock dominates the
  same-wattage cap point on both speed and energy (e.g. nvidia lock1600
  57.7 s @ 37.4 J vs pl330 62.9 s @ ≤39.7 J); for EXL3 locks and caps
  converge within ~1 J/1k — trellis-heavy prefill tracks average power
  regardless of limit shape, while FP4 prefill exposes measurable DVFS
  oscillation waste that a clean lock recovers.
- Cross-quant: **nvidia locked at 1900 MHz out-prefills EXL3 at full
  power** (51.0 s @ 402 W vs 56.0 s @ 580 W).
- Full eight-point tables + methodology:
  [qualification.md § SM clock-lock sweep](qualification.md#sm-clock-lock-sweep-2026-10-07).

## External evidence vs measured

| Source | Claim | Verdict on this host |
|---|---|---|
| arXiv 2605.11999 (H200) | power caps inert during decode; locks Pareto-dominate; decode insensitive to clock above ~1590 MHz; locks >= base clock clamp | Direction confirmed (locks win, decode energy −40%); "flat decode" **not** reproduced — this card free-runs decode at 2365-2616 MHz and decode scales ~clock^0.4-0.55 (MoE GEMMs + MTP are compute-leaning); no firmware clamp seen, the power brake throttles instead |
| GreenLLM (arXiv 2508.16449) | U-shaped prefill energy, mid-band optimum | Confirmed with a caveat: basin (1300-1600) + linear rise, no sharp U |
| r/LocalLLaMA PRO 6000 threads | ~310 W cap sweet spot; "raise the limit, limit the clocks" | Both land in the 1300-1600 MHz basin (291-375 W draw); recipe confirmed |
| Treeru blog (this card) | 450 W costs −19% prefill, −1.3% decode | Matches our cap ladder (69.1 vs 57.1 s; 260 vs 261 tok/s) |
| Undervolt community (LACT/VF-offset) | V/F-curve offset beats clock lock | Out of scope here: needs pynvml/root beyond this host's nvidia-smi-only grant; clock lock alone already recovers most of it on this workload |

## Concurrency scaling (protocol-clean core-suite legs, all four quants)

Aggregate decode tok/s (C1/C2/C4/C8/C16), 256-token outputs, separate
HTTP requests, model name stamped in each receipt:

| Concurrency | C1 | C2 | C4 | C8 | C16 |
|---|---|---|---|---|---|
| EXL3 tok/s | 127.5 | 219.8 | 359.5 | 592.2 | **855.7** |
| NVIDIA NVFP4 tok/s | 133.7 | 239.8 | 415.9 | 637.1 | **992.3** |
| RedHatAI NVFP4 tok/s | 123.6 | 214.7 | 383.9 | 574.7 | **594.5** |
| exl3-ple8 tok/s | 120.8 | 210.0 | 356.9 | 595.2 | **861.0** |

Single-stream is a four-way tie. At C16 **NVIDIA NVFP4 leads (992)**,
EXL3 and its FP8-PLE ablation are close behind (856/861), and **RedHatAI
is the one build that does not scale past C8 (594.5, +3% C8→C16 vs +44%
for EXL3, +56% for NVIDIA)**. The earlier "EXL3 +45% at C16" note
compared EXL3 only against RedHatAI and flagged nvidia as not-yet
re-measured; with nvidia measured on the same stack that ordering is
wrong — NVIDIA is the best scaler, not EXL3 (EXL3 is still +44% over
RedHatAI). The C16 stall tracks the RedHatAI build specifically, not
either of its headline formats: not the NVFP4 experts (NVIDIA NVFP4 is
the best scaler) and not the BF16 PLE table (EXL3 carries the same
format and scales to 856). The two NVFP4 builds differ jointly in
quantization pipeline (LLM Compressor vs ModelOpt) and PLE format
(BF16 vs FP8); neither axis is isolated at C16 here. Receipts:
[core-suite-20261007](../benchmarks/core-suite-20261007/),
[ple8-quality-20261007](../benchmarks/ple8-quality-20261007/core/).

## End-to-end eval legs (wall-clock, 16-way concurrent requests)

| Leg | EXL3 | nvidia | RHA | exl3-ple8 |
|---|---|---|---|---|
| GSM8K (1319 x 5-shot) | 492 s | **445 s** | 550 s | 500 s |
| IFEval (541) | 689 s | **673 s** | 862 s | 699 s |

The ple8 ablation refines the earlier PLE-format reading: swapping only
the PLE table BF16→FP8 on the EXL3 path changed nothing (500/699 vs
492/689 s), so PLE format alone does not set eval-leg wall time. The
RHA-vs-nvidia gap (+24%/+28%) therefore reflects the combination of PLE
format with each build's expert/serving path (compressed-tensors vs the
FP4-GEMM/trellis paths), not the table size on its own.

## Bottom line

- **Speed-first interactive (prefill-dominated):** nvidia NVFP4 at 600 W
  free-running — fastest TTFT at every depth, smallest checkpoint
  (132.7 GB), lowest energy/token of any build x config combination.
- **Throughput-first multi-user:** nvidia NVFP4 — best C16 aggregate
  decode (992.3 tok/s) on top of its prefill lead; EXL3 is second
  (855.7) and remains the pick when quality must also be top-tier
  (joint-best GSM8K, the only 5/5 orchid).
- **Energy-constrained serving:** lock 2200 MHz for interactive use
  (~94% decode speed, ~2/3 decode power) or 1300-1600 MHz for
  batch/offline work (−13/−19% prefill J, −39/−40% decode J per token).
  Locks beat caps on every NVFP4 axis and tie them on EXL3 — locking is
  at worst free.
- **RHA NVFP4** matches EXL3 quality and nvidia prefill but pays nvidia's
  2x PLE host memory (the same ~102 GB BF16 table EXL3 carries — only
  nvidia ships FP8 at 51.2 GB), has the worst eval-leg wall time, and is
  the only build that stalls at C16; keep it as the CT-format reference,
  not the performance pick.
- No cross-release comparisons: every number above is same-day
  (2026-10-07) on image `8294c3c914c0`; `nvidia-smi -pl`/`-lgc` do
  not persist across reboots.

Receipts:
[clock-sweep-20261007](../benchmarks/clock-sweep-20261007/) (lock sweep
+ 2 Hz power samples + cap-frontier join),
[nvfp4-comparison-20261007](../benchmarks/nvfp4-comparison-20261007/)
(ladders), [power-tuning-20261007](../benchmarks/power-tuning-20261007/),
[power-300w-20261006](../benchmarks/power-300w-20261006/),
[quality-nvfp4-vs-exl3-20261007](../benchmarks/quality-nvfp4-vs-exl3-20261007/)
(Oct-7 morning core leg), [core-suite-20261007](../benchmarks/core-suite-20261007/)
(four-way C1-C16 + stamped core suites), [quality-deep-20261007](../benchmarks/quality-deep-20261007/)
(eval leg times), [ple8-quality-20261007](../benchmarks/ple8-quality-20261007/)
(PLE-format ablation). Full record: [qualification.md](qualification.md).
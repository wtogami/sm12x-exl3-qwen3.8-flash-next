# Option B: requantize Qwen/Qwen3.8-Flash-Next ourselves (plan)

Goal: a K≈6 EXL3 card of our own that runs the b12x fast path
(graphs + MTP + EP) at the existing topology's quality ceiling, sized for
2× RTX PRO 6000 Blackwell at 300 W, serving 512K (YaRN-2) as the default
profile with native-262K as the exception profile.

Source: `Qwen/Qwen3.8-Flash-Next` bf16 @ `de4b8e4d43b917e7706784d8bb445c9af86a3540`
(profile `req6`; download started 2026-10-14 ~02:00, ~250 GB, resumes).

## Locked design decisions (with provenance)

| Choice | Value | Why |
|---|---|---|
| Codebook | **mcg** (sqg = optional future) | b12x executes mcg; mul1 is the Swift wall (docs/tp2-exl3-605bpw.md). Assert marker in gate 1. |
| MoE rate | uniform 6 → tuned 5.5–6.0 avg | b12x validator K≤6; Pareto prior art (GLM-4.7: KL .137→.082 4→6bpw; Qwen3.5-397B: tuned 4.61 beat untuned 5.0). |
| Dense (attn/GDN/indexer) | **bf16** | our K4.25 card's proven topology; KV/attention sensitivity per mratsim heuristics + gpt-oss/R1 FP4 precedent; dodges b12x dense-exl3's had_r_128 dependency entirely. |
| Shared experts (width-640) | **bf16** | unsliceable at TP≥2 under the Hadamard-128 guard; bf16 is the base-card topology; costs ~3 GiB/rank, buys exactness. |
| lm_head | **bf16** | keep the untested quantized-head path out of the hot rod; 1.27 GB. |
| PLE n-gram table | BF16 host-pinned, extracted from source | matches existing receipts (gate comparability); FP8-ple re-pack optional later (−48 GiB pinned RAM). |
| Context | max_model_len 524,288, YaRN factor 2.0 default; native-262,144 second profile | owner decision: quality+perf fall-off past 512K (Qwen tech-report RULER/MRCR tables); harnesses auto-detect via /v1/models max_model_len. |
| Concurrency target | ≥8 × 512K sessions | frontier table: pool @6bpw ≈ 4.45M tokens ⇒ ~8.5×512K; MTP3 acceptance re-tuned at gate 4 (fallback MTP2). |
| Kernels | b12x fused MoE (EP), CUDA graphs ON | the whole point; eager is never acceptable (2 tok/s lesson). |

## Ladder (each gate blocks the next)

0. **Source integrity**: download completes → verify against repo file
   checksums; record total size + preprocessor/template hashes.
1. **K6 acceptance on our own artifact** (first converted expert tensor):
   `scripts/probe-exl3-k6-acceptance.py` — cross-stack numerics vs vendor
   reconstruct + b12x fused-MoE prepare/run at K6 mcg. (Methodology
   already validated on K4/K5: bf16-resolution agreement, 5581ca0.)
2. **Reference quants**: uniform `-b 4` and `-b 6`, mcg, dense/shared/head
   unquantized (qmap overrides), work_dir checkpointed for resume.
3. **Sensitivity measurement**: `eval/model_diff.py -l2/-l3` between the
   two references (+ reference choice per decision D3 below) → per-tensor
   KL contributions.
4. **Allocation**: `util/optimize.py` at target 5.8–6.0 avg, then apply
   mratsim hand heuristics (he measured manual > optimizer): higher bits
   on early blocks; nothing above K6 (b12x cap); shared/dense already bf16.
5. **Final conversion** at tuned strategy (~hours, Viterbi-bound; 3rd GPU
   at x8 optional here and at gate 3 — AutoSplit, so x8 is ~free).
6. **Probe engine**: TP2+EP, graphs ON, `MTP_TOKENS=0` first prefill →
   then MTP3 acceptance measurement (acceptance length / tok-per-verify).
7. **Quality gates** vs committed baselines (benchmarks/tp2-ep-base-quality):
   quality suite 20/22-class, orchid 5/5, retrieval 6/6 incl. 240K,
   biomed 13/13, context ladder 8K→523K.
8. **Perf receipts** @300 W: decode C1/C16 + prefill TTFT vs K4.25 cards;
   expected −15..25% decode (bytes under power cap); KV/concurrency
   anchor vs the frontier table. Bonus queued item: TP1 re-run on GPU=1
   (cooler card; owner note 0f177b2).
9. **Ship**: commit profile + sidecar snapshots + docs to prod branch
   lineage; K4.25 card retained as fallback.

## Calibration set (the one consequential open choice)

exllamav3 default is 250×2048 generic rows; Swift's sidecar shows exactly
that. For 512-expert top-10 routing that is ~10k token-activations per
expert — statistically fine, distribution-wise biased toward whatever the
corpus covers. Proposal (~512 rows × 2048, mixed, fixed seed, all in
tree under `calibration/`):

- 40% generic prose (wiki/books-equivalent) — coverage floor
- 25% code + tool-use JSON/agentic transcripts — the coding workload
- 25% biomedical text (PubMed abstracts + OA full-text samples; the
  biomed gate corpus seeds this) — the paper-analysis workload
- 10% long-range synthetic (repetition/needle-style) — stabilizer

Gate on this step: after a dry H-capture, dump per-expert activation
counts and flag starved experts (<500 activations) before committing to
quantization (mratsim's MoE-calibration caveat).

## Decisions needed from owner

- **D1 calibration mix** above vs alternative ratios / more biomedical weight.
- **D2 third GPU (x8)**: use for gates 3+5 (faster), or keep 2-GPU + CPU-offload?
- **D3 KL reference**: bf16 original (ground truth, slow: CPU-offload or
  3-GPU) vs an 8 bpw-H6 intermediate (mratsim practice; rankings hold,
  absolute KL shifted).
- **D4 final average**: uniform 6.0 (simple, reproducible) vs tuned
  ~5.7–5.8 (better KL/byte, more moving parts).
- **D5 cleanup**: delete Swift re-downloadable artifacts (~280 GB: 6-bit
  snapshot + converted bf16ple) once this plan is committed; keep tooling.

## Risks on record

- K6 fused-MoE JIT specialization unproven until gate 1 on real tensors
  (contract says {2..6}; K5 in production).
- MTP3 acceptance may shift with the new target/draft pair → measure at
  gate 6, retune or drop to MTP2 (perf-column honesty).
- exllamav3 1.5.1 default codebook is now mul1 — **every conversion
  invocation must pin `-cb mcg`** and gate 1 asserts the marker tensor.
- Calibration-set overfitting to biomed → keep the generic 40% floor.

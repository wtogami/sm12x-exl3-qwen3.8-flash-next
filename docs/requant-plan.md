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
| MoE rate | **uniform 6.0** (owner 2026-10-14); tuned mix = deferred study track | b12x validator K≤6; Pareto prior art (GLM-4.7: KL .137→.082 4→6bpw; Qwen3.5-397B: tuned 4.61 beat untuned 5.0). |
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
2. **Uniform 6.0 conversion** (`-b 6 -cb mcg`, dense/shared/head/PLE
   unquantized, work_dir checkpointed for resume). LAUNCHED 2026-10-14
   (container req6-convert): `python -m exllamav3.conversion.convert_model
   -i <src de4b8e4d> -o /hf/req6-build/out -w /hf/req6-build/work -rcp
   quant/recipe-uniform6.yaml(-gz) -b 6 -hb 16 -mb 6 -vb 16 -cb mcg -cr
   250 -cc 2048 -d 0,1 -v`. The recipe (74,040 budgeted tensors: 73,728
   routed experts→6, 312 shared/GDN/attn/indexer→16) exactly mirrors the
   K4.25 base-card ledger topology. Two post-conversion REPAIRS required
   (converter forces these, vanilla CLI can't opt out):
   (a) n-gram table: converter emits K6 trellis `ngram_embedding.
   safetensors`; fork serves checkpoint_mapped BF16 in-shard weights (the
   fork has never served trellis n-gram), so we re-inject the 130 BF16
   source tensors as model-ngram-*.safetensors and register them in the
   index (scripts/req6-ngram-reinject.py; DONE 2026-10-14, verified
   bitwise-identical to source, 102.4 GB). The K6 trellis file is STASHED
   at req6-build/keep/ngram_embedding.K6-mcg.safetensors (37 GB, owner
   request) for the future "quantized n-gram on host: 38 vs 104 GB pinned
   RAM / PLE-quant sensitivity" experiment;
   (b) MTP dense linears: `-mb` is one scalar over the whole draft block
   (mtp_bits covers attn+shared+experts), so mtp arrives fully K6-quantized;
   dequant its non-expert linears to BF16 via the exl3-605-shared-dequant
   pattern so the draft matches the base topology (experts quantized,
   dense bf16) and never touches b12x's ext-dependent dense-exl3 path.
   REPAIRS (a)+(b)+(c) DONE 2026-10-14 (scripts/req6-ngram-reinject.py,
   scripts/req6-mtp-repair.py): 10 dense mtp tensors -> BF16 (q_proj
   round-trip cos64 0.99979 vs source, rel-RMSE 2.0% = expected K6 err);
   ledger now 75,934 entries incl. 1,536 mtp experts; exl3-quantized total
   75,264 = exactly the base-card invariant. Fork schema compatibility
   (e) GHOST KEYS (found by the MTP3 boot failure, 13:50): repair (b) fixed
   the index but left the replaced trellis payloads physically in shard 13;
   vLLM's default loader walks FILES, not the index, so any module that
   exists at load time (draft under MTP) strict-rejects the orphans.
   scripts/req6-strip-ghost-keys.py removed 40; verified zero file-vs-index
   ghosts remain across all shards.
   verified against exl3.py's parser (nested stored_tensors + quant_format
   + int bits_per_weight + mcg_multiplier 0xCBAC1FED all expected). Calibration: vendor
   default 250×2048 - with no allocation to steer, the corpus is no
   longer a lever; it still shapes in-tensor rounding (H-weighted
   Viterbi + suh/svh fit), so pin it for reproducibility, not quality.
   (Gate 0 DONE 2026-10-14: 144 files / 360.0 GB content-verified vs
   trees/de4b8e4d metadata -> benchmarks/req6-source/gate0-verify.log.)
3. **Quality vs BF16 (D3)**: tensor-level RMSE/cos of reconstructed
   experts against source BF16 (vendor reconstruct, minutes, 2 GPUs) +
   `eval/model_diff.py` KL with the BF16 original as ref on CPU-offload
   (360 GB fits 995 GiB RAM; 10×1024 rows is a few hours - fine).
   This KL becomes the absolute number of record for future tuning.
4. *(DEFERRED — tuning study track)*: regenerate 4/5 bpw refs, run
   `model_diff -l2/-l3` sensitivity, `util/optimize.py` + mratsim
   heuristics (early blocks up, K6 ceiling). Nothing here is lost by
   deferral; it costs a later 2-reference re-run.
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
8d. *(REPAIR, new)* ledger filter: 1.6.1 records BF16 passthrough
   modules in tensor_storage; the fork's standard-fused-MoE scanner needs
   an experts-only ledger (base-card shape) or it silently declines the
   fast path and the enforce-eager guard fires at the vision tower.
   scripts/req6-ledger-filter.py (75,934 -> 75,264).
   GATE 1 PASSED 2026-10-14: real-K6 cross-stack numerics (maxabs <=2e-3
   = bf16 res), vs-source rel-RMSE 1.79-2.00% across layers/projections/
   MTP, probe-engine boot TP2+EP GRAPHS ON (capture 4 s), first prefill
   + "42" smoke clean. The fused-MoE K6 question is answered: YES.

9. **Ship**: commit profile + sidecar snapshots + docs to prod branch
   lineage; K4.25 card retained as fallback.

## Calibration set (D1 - narrowed by prior-art survey, 2026-10-14)

**Prior art, verified from card READMEs/sidecars:** the entire comparable
field uses exllamav3's bundled default (250x2048): Swift 6.05 ("standard
ExLlamaV3 mix"), Infatoshi GLM-5.3 ("default calibration 250x2048"),
mratsim GLM-4.6/4.7 (default; he warns calibration==ppl-dataset causes
ppl overfitting, hence KL as the metric and manual heuristics over the
optimizer). One documented domain outlier: brandonmusic GLM-5.2 built on
a 12,228-sample legal+general corpus (~1M tokens, SHA-256 journalled).
Our own K4.25 card's sidecar records calibration:null and its qflashrt
doc link now 404s, but its ds4rt-lineage recipe is 250x2048 standard.

**Vendor default anatomy** (exllamav3/conversion/calibration_data.py +
bundled standard_cal_data, ~5.5 MB fixed corpus): wiki 50, c4 20,
code 20, multilingual 10, technical 10, tiny 5, RANDOM-vocab rows 20
(weight/135). Caveat found by inspection: the bundled code.utf8 is
m68k assembly and technical.utf8 is license text - near-zero coverage of
modern code or agentic/tool-use distributions.

**Architecture fact (shrinks the problem):** qwen4_exp (our model_type)
sets `calibration_all_experts = True` in qwen4_exp.py:306 - the vendor
converter forces every expert to fire on every calibration token when
capturing Hessians, so mratsim's "expert quantized away as unimportant"
failure mode cannot occur at the H pass for our model; his residual
concern (value-range/clipping coverage) remains.

### Prior-art panel: what the base-card author actually used (found 2026-10-14)

wrldsuksgo2mars published his calibration: HF dataset
`wrldsuksgo2mars/agentic-quant-panel-v1` (canonical corpus
`flash-corpus-next-v1`, originally built for DeepSeek-V4-Flash-0731).
Verified locally, SHA256SUMS-checked, at
`.cache/huggingface/calibration/agentic-quant-panel-v1/`:
calibration 1,426 prompts / 1,081,027 recorded tokens (~768-token records,
seed 20260806, SHA-256 source-group bucketing, disjoint heldout);
five-axis token blend code_agentic 33.3% / general wiki 24.0% / math 20.4%
/ reasoning_termination 12.1% / structured xLAM 10.2%. His allocation claim
("4.25bpw, error-selected 5bpw at 3:5:8 gate:up:down") is forensically
confirmed from our base-card ledger: K5 counts 3528:5880:9408 = exactly
3:5:8 (base 1176), exactly 25.00% upgraded -> exactly 4.2500 bpw.
Caveat: the published release is the original corpus; his per-model Qwen
selection re-tokenized the same recipe (the card is not claimed
tokenizer-identical to it).

Staged for the deferred study: scripts/req6-pack-panel-calibration.py
rendered all 1,426 records through the Qwen chat template into the exact
`-cd` contract (int32 input_ids (1426, 2048) + per-row lengths;
1,180,869 tokens; row range 566-1269; sha256
6cf142dc...644b) at `.cache/huggingface/calibration/agentic-quant-panel-v1/panel-qwen-packed-2048.safetensors`.
A/B recipe (single variable - corpus): identical uniform-6.0 mcg recipe,
`-cd <panel file> -cr 1426 -cc 2048`, compare orchid (our only regression:
deterministic 99-word length bias vs base 5/5) + KL-vs-BF16 + tensor RMSE.
~2x H-capture time vs vendor 250x2048 (~+1-1.5h). Rationale for expecting
signal: the panel is saturated with exact-format agentic outputs (xLAM
JSON, structured completions) and was the calibration behind the card that
beats ours on strict length compliance.

Baseline decision: quantize the reference pair with the PURE vendor
default - identical to every comparable card and to our existing
baseline's recipe, which keeps sensitivity measurement apples-to-apples.
Optional A/B only if time allows: augment with ~64-128 rows modern
code/tool-use + biomedical text (the vendor mix contains neither) and
compare KL on a held-out generic set + biomed gate deltas. If the A/B
doesn't clearly win, ship the plain default.

## Decisions needed from owner

- **D1 calibration**: confirm plain vendor-default baseline (recommended);
  opt in/out of the modern-code+biomed A/B.
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
- VERSION-PAIRING (bit us 2026-10-14 02:14): the prebuilt
  exv3-runtime/exllamav3_ext.so carries the **1.6.1** binding surface
  (6-arg ngram_dequant), but the conversion first ran under the 1.5.1
  python tree (5-arg call) -> TypeError mid-calibration. Python tree and
  ext .so MUST be the same vendor version; relaunched under exv3-env-161
  + `-r` resume (work-dir checkpoints made the 40-min ngram pass
  survive). Verified: every ext symbol 1.6.1 calls exists in the .so.
- Calibration overfitting: vendor mix doubles as EXL3's ppl dataset (mratsim
  caveat) → judge gates on KL + our biomed/quality suites, never on ppl.

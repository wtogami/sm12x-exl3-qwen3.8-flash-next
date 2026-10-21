# Tuning and kernel-choice history (provenance)

Extracted from the README on 2026-10-20 so the README can stay
new-user-facing. Everything here is era-tagged measurement record, not
current documentation. Unless noted, columns are the v0.1.0–v0.3.1-era
images: RTX = one 400 W card, 262144 context; Spark = one GB10. The
full generated matrices from the raw receipt directories live in
[../benchmarks/RESULTS.md](../benchmarks/RESULTS.md); the dated
qualification ledger is [qualification.md](qualification.md).

Tool-eval figures in RESULTS.md use C8 (eight concurrent cases); their
points are not a normalized score and are not comparable to the C1
numbers below. The original EXL3/NVIDIA columns differ in quantization,
PLE precision and runtime: they are not a controlled mmap-on/off
comparison.

## MTP draft-policy sweeps (RTX, resident mode)

Development comparisons on the same seven C1 workloads (three measured
responses each). C8/C16 are shorter probes: 128 forced prose tokens, one
measured batch after warmup. They are distinct from the final 256-token,
three-run client matrix in RESULTS.md. Small differences between runs are
not a statistical proof of superiority; the defaults use the best observed
mixed C1 result.

| Quant | Draft policy | C1 weighted blend, tokens/s | C8 aggregate | C16 aggregate |
|---|---|---:|---:|---:|
| EXL3 | Off | 92.76 | 465.99 | 762.88 |
| EXL3 | 1 | 131.54 | 561.14 | 861.92 |
| EXL3 | 2 | 149.16 | 532.52 | 718.39 |
| EXL3 | **3, default** | **152.97** | — | — |
| EXL3 | 4 | 150.63 | 408.90 | 486.12 |
| EXL3 | Adaptive 3 → 1 | 150.71 | 516.51 | 792.07 |
| NVFP4 | Off | 96.38 | 500.96 | 855.50 |
| NVFP4 | 1 | 130.35 | 597.43 | 915.16 |
| NVFP4 | **2, default** | **152.40** | 552.95 | 881.49 |
| NVFP4 | 3 | 149.46 | 549.68 | 544.11 |
| NVFP4 | Adaptive 2 → 1 | 146.04 | 585.89 | 858.97 |

Adaptive policies use the longer draft for 1–4 scheduled requests and one
draft token for 5–16. Neither improved the observed C1 blend. MTP1 is a
measured option for higher C16 throughput at a substantial C1 cost. The
MTP4 EXL3 and MTP3 NVIDIA C16 probes reached only 14 and 13 overlapping
streams, respectively.

Reproducer for the EXL3 adaptive comparison (suppress the automatic static
configuration, supply the measured scheduler policy explicitly):

```bash
QUANT=exl3 PLE_MMAP=0 MTP_TOKENS=0 bash start.sh --speculative-config \
  '{"method":"mtp","num_speculative_tokens":3,"num_speculative_tokens_per_batch_size":[[1,4,3],[5,16,1]]}'
```

Code-heavy C1 note: EXL3 MTP4 reached 217.18 tokens/s on the greedy
`merge_intervals` workload versus 202.99 in the qualified MTP3 run; NVIDIA
MTP3 reached 202.67 versus 182.82 with MTP2. Workload-specific options, not
defaults; distinct from the sampled async coding task in RESULTS.md.

## Spark BF16-mmap tuning (GB10)

BF16 mmap comparisons favor **MTP2** for the mixed C1 workload. The initial
readahead-off sweep measured 26.29, 27.32, 25.72 and 24.14 tokens/s for
MTP1 through MTP4. Targeted readahead raised the MTP2 blend to 29.21;
adding B12x vocabulary on the same host raised it to 30.75; the combined
MTP2 / readahead2048 / B12x vocabulary profile measures **31.11 tokens/s**
in the completed qualification. All tuning receipts, C16 probes and
same-host comparisons: [../benchmarks/spark-review/TUNING.md](../benchmarks/spark-review/TUNING.md).
The 128-range and 2048-range short C16 probes measured 89.09 and 97.14
tokens/s; these single probes are distinct from the final client matrix.

## Kernel-choice measurement record behind the defaults

- **NVFP4 MoE:** native FlashInfer CUTLASS ships. Precise B12x won isolated
  component timings but lost the end-to-end C1 blend, 136.74 versus 152.40
  tokens/s. `B12X_NVFP4=1` retains the tested optional bridge, off by
  default.
- **Vocabulary projection:** native on RTX; B12x on Spark. B12x did not
  improve the RTX mixed C1 blend, but improved the same-host Spark
  comparison from 29.21 to 30.75 tokens/s.
- **HC and GDN:** native on both platforms. Spark's native HC was faster at
  decode sizes; the optional B12x GDN path failed its B16/Q3 numerical
  check and showed no timing advantage in earlier cases. RTX comparisons
  also retained native paths.
- **QSA:** the former B12x QSA bridge was retired when v0.30.0 rejected FP8
  main caches and upstream PR 55557 (in v0.31.0) superseded it; its
  pre-rebase measurements are historical.

## Selected profile summaries (pre-rebase, v0.3.1-era images)

| Measurement | EXL3 resident, MTP3 (v0.1.0) | NVFP4 resident, MTP2 (v0.1.0) | EXL3 PLE8 mmap, MTP3 | EXL3 mmap Spark (BF16 PLE), MTP2 |
|---|---:|---:|---:|---:|
| C1 seven-workload weighted decode, tokens/s | **152.82** | **151.11** | **147.78** | **31.11** |
| C1 greedy `merge_intervals` median, tokens/s | 202.99 | 185.55 | 197.74 | 38.12 |
| C1 sampled async coding task median, tokens/s | 185.55 | 166.92 | 183.45 | 36.21 |
| C16 sampled-prose aggregate median, tokens/s | 696.96 | 930.71 | 659.75 | 101.00 |
| Full-context boundary: 261888 input + 256 output | Pass | Pass | Pass | Pass |
| API tool constraints / retrieval probes | 16/16; 6/6 | 16/16; 6/6 | 16/16; 6/6 | 16/16; 6/6 |

These are different workloads, not interchangeable rates. Startup KV pools
on those runtimes: 796612 tokens (resident EXL3), 686817 (resident NVIDIA),
880600 (EXL3 PLE8 mmap) — 2.6–3.4× the configured context; pools are
profiled at boot and vary by host, so consult each receipt's `runtime.json`.

## Release-era notes

v0.1.0 runtime receipts retain the original image IDs. v0.2.0 added mmap
support, reviewed validation fixes, and full `exl3-ple8` mmap
qualification on RTX. v0.3.0/v0.3.1 added native Spark support and the
resident-mode loading repair (220-test RTX runs, resident regressions on
both architectures; the shipping-default structured-output receipts are at
[../benchmarks/rtx-structured-final/qualification.json](../benchmarks/rtx-structured-final/qualification.json)).
The current branch completes three engine rebases (v0.30.0, v0.31.0, then
released v0.32.0). Publishing runtime images was tried and retired: the
registry packages were the old maintainer's, fell two rebases behind, and
were removed as a distribution path on 2026-10-20 — building from source is
the design, not a fallback.

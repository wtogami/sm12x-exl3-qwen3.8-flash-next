# Spark (DGX Spark GB10) qualification handoff — v0.32.0 base

You are qualifying this recipe on native arm64 hardware. Nothing here
requires registry access; build and test entirely locally.

## What changed for this test
- Base: `vllm/vllm-openai:v0.32.0` (official arm64 image), replacing v0.31.0.
- The image now carries `patches/port-mrope-served-window.py` — the real
  fix for the v0.32.0 M-RoPE boundary crash (upstream issue
  vllm-project/vllm#62383, fix filed as PR vllm-project/vllm#62409).
  It floors the cos/sin cache at `max_model_len` instead of restoring the
  old blanket 4x. Spark serves the native 262144 window by default, where
  the floor is provably inert (bit-identical cache sizing to stock,
  verified on RTX). If you see ANY behavior delta vs v0.31 at native
  lengths, that is itself a finding about this build.
- Extended context: `LONGCTX=1` (YaRN 512K) is NOT needed to pass this
  qualification — the native window is the gate — but it is an optional
  probe we would genuinely like results from (see below). The image
  carries the #62383 fix and it is proven on RTX at 480K; no Spark has
  ever been observed serving past its native window, so a pass OR a
  documented failure is valuable data.
- Spark defaults are unchanged: `PLE_MMAP=1` (checkpoint-mapped table),
  MTP2, 0.7 utilization cap, native 262144.

## Prerequisites
- DGX Spark (GB10), driver that loads, podman (or docker) + the CUDA CDI
  hook; `bash build.sh` provisions the CDI spec automatically.
- Checkpoints in the HuggingFace cache: `QUANT=exl3 bash download.sh`
  (~168 GiB BF16-PLE EXL3 K4.25). The mmap backend reads that checkpoint
  in place; no extra conversion.

## Run (from the repo root, on this branch)
```bash
git rev-parse HEAD                     # record this in your report
bash build.sh                          # native arm64 build -> spark-exl3-qwen3.8-flash-next:local
python3 scripts/test-quant-profiles.py
RESULT_DIR=benchmarks/spark-v032 \
  IMAGE=localhost/spark-exl3-qwen3.8-flash-next:local \
  QUANTS=exl3 \
  BOUNDARY_TOKENS= \
  bash scripts/qualification-legs.sh   # functional matrix + runtime.json/memory.json
                                      # captures per profile, owns start/stop
bash scripts/spark-report.sh benchmarks/spark-v032
# prints the path of spark-report-<host>-<utc>.tar.gz
```
Notes:
- `BOUNDARY_TOKENS=` (empty) is correct for the native-window gate: the
  280K/480K legs are past-native requests, impossible by definition here.
- The legs script is resume-safe (a profile with an existing first
  receipt is skipped) and never aborts the matrix mid-profile; its
  per-step `exit N` lines land in `RESULT_DIR/driver.log`, which the
  report packages automatically.
- Add `QUANTS="exl3 exl3-ple8"` only if you also downloaded the FP8-PLE
  variant; `exl3` is the gate.

## Pass criteria
- `build.sh` exits 0 (all in-image CPU canaries run during the build).
- `test-quant-profiles.py` exit 0.
- `qualification-legs.sh` exit 0 — equivalently: every `exit` line in
  `driver.log` says `exit 0` (api, vision, retrieval, structured,
  seven, orchid, tools for each profile booted).
- Engine log during the run contains no `Xid`, no retraction storms, and
  `checkpoint_mapped` is confirmed active at startup.
- seven/orchid are informational — report counts, don't chase a miss
  class marked as known jitter in docs/qualification.md.

## Optional (wanted!): extended-context probe — `LONGCTX=1`

First-ever Spark observation past the native window. Skip it if you only
have time for the gate. Unified-memory allocation is the open question
(the KV pool at 0.7 util with a 524288 window), so an OOM or auto-shrink
is an acceptable, reportable outcome — capture what happened, don't
fight it:

```bash
LONGCTX=1 RESULT_DIR=benchmarks/spark-v032-longctx \
  IMAGE=localhost/spark-exl3-qwen3.8-flash-next:local \
  QUANTS=exl3 BOUNDARY_TOKENS="280000 480000" \
  bash scripts/qualification-legs.sh
bash scripts/spark-report.sh benchmarks/spark-v032-longctx
```

If the engine boots, the boundary legs (280K/480K) are the interesting
part: they are exactly the requests that crash unpatched v0.32.0 and
pass on RTX with this image. Send both report tarballs back; a crash
report from this section does not invalidate the gate above.

## Send back
The single `spark-report-*.tar.gz` the script prints. It bundles all
receipts (including `driver.log`), host/driver/image/git identity, and
the exit-code summary. No model weights, no personal files.

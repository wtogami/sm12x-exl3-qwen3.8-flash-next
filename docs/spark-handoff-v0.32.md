# Spark (DGX Spark GB10) qualification handoff — v0.32.0 base

You are qualifying this recipe on native arm64 hardware. Nothing here
requires registry access; build and test entirely locally.

## What changed for this test
- Base: `vllm/vllm-openai:v0.32.0` (official arm64 image), replacing v0.31.0.
- New in the image: the `port-mrope-positions-factor.py` regression port
  (upstream issue vllm-project/vllm#62383). Spark serves the native
  262144 window by default and never crosses the bug boundary, so the
  port must be a no-op on this platform — if you see ANY behavior change
  vs v0.31 at native lengths, that is itself a finding.
- Do NOT set `LONGCTX=1` on v0.32 for anything beyond a guarded test:
  the YaRN path past 262144 is the open upstream bug (the vendored ×4
  port fixes it on RTX; Spark is untested for it).
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
git rev-parse HEAD                    # record this in your report
bash build.sh                         # native arm64 build -> spark-exl3-qwen3.8-flash-next:local
bash start.sh                         # exl3, mmap, MTP2, native window
python3 scripts/test-quant-profiles.py
RESULT_DIR=benchmarks/spark-v032 bash scripts/quality-suite.sh
python3 scripts/capture-runtime.py --output benchmarks/spark-v032/runtime.json
python3 scripts/capture-mmap-memory.py --output benchmarks/spark-v032/memory.json
bash stop.sh
```
Then package everything:
```bash
bash scripts/spark-report.sh benchmarks/spark-v032
# prints the path of spark-report-<host>-<utc>.tar.gz
```

## Pass criteria
- `build.sh` exits 0 (all in-image CPU canaries run during the build).
- `quality-suite.sh`: every step exit 0; contracts pass at their
  documented rates (vision pass; retrieval 6/6 at 8K/240K; api 16/16;
  seven/orchid are informational - report counts, don't chase a miss
  class marked as known jitter in docs/qualification.md).
- Engine log during the run contains no `Xid`, no retraction storms, and
  `checkpoint_mapped` is confirmed active at startup.

## Send back
The single `spark-report-*.tar.gz` the script prints. It bundles all
receipts, host/driver/image/git identity, and an exit-code summary. No
model weights, no personal files.

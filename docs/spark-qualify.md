# DGX Spark (GB10) qualification — v0.32 branch

**Repo:** https://github.com/wtogami/sm12x-exl3-qwen3.8-flash-next
**Branch:** `spark-v0.32-qual`
**Effort:** ~1–2 h, almost all unattended (plus a ~168 GiB model download on a fresh host)

This rebases the recipe onto **vLLM v0.32.0** and qualifies it natively on
your Spark. Everything builds and runs locally: no registry access, nothing
to push. The authoritative details are in
[`docs/spark-handoff-v0.32.md`](spark-handoff-v0.32.md) — this is the short
version.

## Steps

```bash
git clone https://github.com/wtogami/sm12x-exl3-qwen3.8-flash-next
cd sm12x-exl3-qwen3.8-flash-next
git checkout spark-v0.32-qual

git rev-parse HEAD                      # record this in your reply

bash build.sh                           # native arm64 build; provisions the CUDA CDI spec itself

QUANT=exl3 bash download.sh             # skip if the EXL3 K4.25 checkpoint is already cached

python3 scripts/test-quant-profiles.py

RESULT_DIR=benchmarks/spark-v032 \
  IMAGE=localhost/spark-exl3-qwen3.8-flash-next:local \
  QUANTS=exl3 BOUNDARY_TOKENS= \
  bash scripts/qualification-legs.sh

bash scripts/spark-report.sh benchmarks/spark-v032
```

Notes:

- The legs script owns engine start/stop, captures runtime/memory evidence
  itself, and is **resume-safe** — if anything fails, fix and re-run the
  same command; completed profiles are skipped.
- `BOUNDARY_TOKENS=` (empty) is correct for the default run: those 280K/480K
  probes are past the native 262,144 window by definition (see the optional
  probe below for why we also *want* those on Spark).
- The last command prints the path of a single `spark-report-<host>-<utc>.tar.gz`.

## Pass criteria (the gate)

- `build.sh`, `test-quant-profiles.py`, and `qualification-legs.sh` all exit 0
  (equivalently: every `exit N` line in `benchmarks/spark-v032/driver.log` reads `exit 0`)
- Engine log shows no `Xid` lines and `checkpoint_mapped` active at startup
- `seven`/`orchid` quality counts are informational — report the numbers, don't chase misses

## Optional, but genuinely wanted: extended-context probe (`LONGCTX=1`)

v0.32 had an upstream crash past the native window (vllm#62383); this image
carries the fix, proven on RTX at 480K — but **no Spark has ever been
observed serving past native**, and the unified-memory allocation behavior
at a 524288 window is exactly the data we don't have. Skip if short on time;
an OOM with a captured log tail is a perfectly good result to send back.

```bash
LONGCTX=1 RESULT_DIR=benchmarks/spark-v032-longctx \
  IMAGE=localhost/spark-exl3-qwen3.8-flash-next:local \
  QUANTS=exl3 BOUNDARY_TOKENS="280000 480000" \
  bash scripts/qualification-legs.sh
bash scripts/spark-report.sh benchmarks/spark-v032-longctx
```

A failure here does **not** invalidate the gate above.

## Send back

The one or two `spark-report-*.tar.gz` files the last commands printed, plus
your `git rev-parse HEAD`. They bundle all receipts, driver logs, and
host/driver/GPU/image identity — deliberately no model weights, no personal
files.

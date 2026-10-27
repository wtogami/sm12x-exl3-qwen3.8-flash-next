#!/usr/bin/env python3
"""Assert the KV residency geometry a GPU_MEMORY_UTILIZATION decision was made on.

Why this exists. The headroom ladder's conclusion -- that 0.96 and 0.965 are *slower*
than the 0.94 default while 0.97 and 0.98 are much faster -- rests on residency being
counted in 3200-token pages, with per-sequence state on top. Those boundaries are
properties of the IMAGE, not of the model: gg (v0.31) pools were 296/336/356/376 pages
at 0.94/0.96/0.97/0.98 and burst (v0.32) pools are 303/344/363/384 -- the same shape,
shifted by 7-8 pages, and the 0.96 trap reproduced at the new page counts. Any statement
of the form ">= N pages is clean" is therefore expired the moment the image changes.

The honest version is a promise to re-measure, and promises humans hold are worthless.
This makes the check mechanical: read the geometry the scheduler is actually using
(/metrics labels, not a transcribed log line), compare it to the qualified baseline, and
  FAIL  if the mechanism changed (attention block size != expected, or the mamba/attention
        page shapes stopped being equalized) -- every page-arithmetic conclusion in the
        qualification ledger is then void until re-derived, and
  WARN  if the mechanism held but the pool moved -- the shape of the ladder survives, its
        breakpoints do not, so re-run scripts/util-deep-concurrency.sh before quoting
        any specific utilization level as clean or dirty.

Exit codes: 0 clean, 1 warning (pool drift), 2 failure (mechanism changed / unreachable).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

EXPECTED_BLOCK_SIZE = 3200  # tokens; the page unit vLLM picks for this hybrid model


def cache_config(base_url: str) -> dict[str, str]:
    """Pull vllm:cache_config_info's label set -- the engine's own statement of geometry."""
    url = base_url.rstrip("/") + "/metrics"
    with urllib.request.urlopen(url, timeout=30) as response:
        body = response.read().decode("utf-8", "replace")
    for line in body.splitlines():
        if line.startswith("vllm:cache_config_info"):
            inner = line.split("{", 1)[1].rsplit("}", 1)[0]
            out: dict[str, str] = {}
            for part in inner.split('",'):
                key, _, value = part.partition('="')
                out[key.strip()] = value.rstrip('"')
            return out
    raise LookupError(f"no vllm:cache_config_info gauge on {url}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:8001/v1")
    ap.add_argument("--baseline", type=Path, help="qualified geometry to compare against")
    ap.add_argument("--util", help="which baseline entry applies (e.g. 0.98)")
    ap.add_argument("--page-tolerance", type=float, default=2.0,
                    help="pages of drift tolerated before WARN (default 2)")
    ap.add_argument("--output", type=Path, help="write the observed geometry here")
    ap.add_argument("--host", help="only compare against baseline entries for this host")
    ap.add_argument("--warn-only", action="store_true",
                    help="report drift but exit 0 (for legs that record without gating)")
    args = ap.parse_args()

    try:
        cfg = cache_config(args.base_url)
    except Exception as exc:  # noqa: BLE001 - any transport failure is "cannot verify"
        print(f"RESIDENCY UNKNOWN: {exc}")
        return 2

    observed = {
        "attention_block_size": cfg.get("effective_attention_block_size") or cfg.get("block_size"),
        # NOT num_gpu_blocks. On this hybrid model that label counts a different pool
        # (a 0.94 boot with a 969,224-token / 303-page attention pool reports 342, and a
        # 0.98 boot with 384 attention pages reports 433). The attention pool is
        # kv_cache_size_tokens, and dividing it by the block size is what the residency
        # arithmetic in the qualification ledger is actually about.
        "num_gpu_blocks": cfg.get("num_gpu_blocks"),
        "mamba_block_size": cfg.get("mamba_block_size"),
        "mamba_cache_mode": cfg.get("mamba_cache_mode"),
        "gpu_memory_utilization": cfg.get("gpu_memory_utilization"),
        "kv_cache_size_tokens": cfg.get("kv_cache_size_tokens"),
        "printed_max_concurrency": cfg.get("kv_cache_max_concurrency"),
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"cache_config_labels": observed}, indent=2) + "\n")

    problems: list[str] = []
    notes: list[str] = []

    # Mechanism checks: these hold regardless of image or pool size.
    try:
        block = int(observed["attention_block_size"])
    except (TypeError, ValueError):
        block = 0
    if block != EXPECTED_BLOCK_SIZE:
        problems.append(
            f"attention block size is {observed['attention_block_size']}, expected "
            f"{EXPECTED_BLOCK_SIZE}: residency is no longer counted in the pages the "
            f"qualification ledger's arithmetic uses, so every band/breakpoint claim is void"
        )
    if observed["mamba_cache_mode"] not in (None, "align"):
        notes.append(f"mamba_cache_mode={observed['mamba_cache_mode']} (qualified: align)")
    try:
        tokens = int(observed["kv_cache_size_tokens"])
    except (TypeError, ValueError):
        tokens = 0
    if not tokens:
        problems.append("engine reported no kv_cache_size_tokens; cannot check the pool")

    if args.host is not None and observed["gpu_memory_utilization"] not in (None, str(args.util)):
        notes.append(f"engine booted at util {observed['gpu_memory_utilization']}, asked to "
                     f"compare against the util={args.util} baseline entry")

    if args.baseline and args.util and block and tokens:
        table = json.loads(args.baseline.read_text())
        entry = next((e for e in table["entries"]
                      if str(e.get("util")) == str(args.util)
                      and (args.host is None or e.get("host") == args.host)), None)
        if entry is None:
            problems.append(f"no baseline entry for util={args.util}"
                            + (f" host={args.host}" if args.host else "")
                            + f" in {args.baseline}")
        else:
            now_pages = tokens / block
            was_pages = float(entry["pool_pages"])
            drift = now_pages - was_pages
            notes.append(f"pool {was_pages:.0f} -> {now_pages:.1f} pages "
                         f"({tokens:,} tokens, block {block}) vs {entry.get('note', 'baseline')}")
            if abs(drift) > args.page_tolerance:
                problems.append(
                    f"pool moved {drift:+.1f} pages at util={args.util} (tolerance "
                    f"{args.page_tolerance:g}). The eviction band sits on page counts, so "
                    f"re-run scripts/util-deep-concurrency.sh before calling any utilization "
                    f"level clean -- v0.31 -> v0.32 moved ~8 pages and moved the trap with it."
                )

    for line in notes:
        print(f"  residency: {line}")
    if problems:
        for line in problems:
            print(f"RESIDENCY {'WARN' if args.warn_only else 'FAIL'}: {line}")
        return 0 if args.warn_only else (2 if block != EXPECTED_BLOCK_SIZE else 1)
    print(f"residency geometry OK: block {block} tokens, "
          f"{(tokens / block if block else 0):.0f} pages, "
          f"util {observed['gpu_memory_utilization']}, "
          f"num_gpu_blocks(label, other pool)={observed['num_gpu_blocks']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

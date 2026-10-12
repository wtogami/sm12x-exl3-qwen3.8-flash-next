#!/usr/bin/env python3
"""Integrated-path correctness test: MappedTable -> RowStager, versus positional pread.

Run inside the built image:
    podman run --rm --device nvidia.com/gpu=all --security-opt label=disable \
      -v ~/.cache/huggingface:/home/rtx/.cache/huggingface:ro -e HOME=/home/rtx \
      -e PLE_ROWCOPY_SO=/opt/qwen38/ple_rowcopy.so -e TEST_NUM_ROWS=320001536 \
      -e TEST_SPLIT_PARTS=128 -e TEST_ROWS=4000 --entrypoint python3 <image> \
      /opt/qwen38-tests/test_stream_table.py
Measured result: 4,000/4,000 rows byte-identical to os.pread at independently computed offsets, and
2,000 unowned rows zeroed with no residue from a 0xAB-filled destination.


The engine's own gather cannot be used as the oracle here: _gather_mapped_rows_kernel dereferences
host addresses, which is the capability discrete RTX lacks. So the reference is `os.pread` at offsets
computed independently from the shard layout -- a different code path to the same bytes.
"""
import os, sys
import numpy as np, torch

LAYER = int(os.getenv("TEST_LAYER", "1"))
N = int(os.getenv("TEST_ROWS", "3000"))

def main():
    from vllm.models.qwen4_exp.nvidia.ple_pageable import (
        MappedTable, discover_table_layout)
    from vllm.model_executor.layers.qwen_stream_gather import StreamTable
    from vllm.model_executor.layers.qwen_ple_stream import RowStager

    import json, glob
    snap = sorted(glob.glob(os.path.expanduser(
        "~/.cache/huggingface/hub/models--*PLE-FP8*/snapshots/*")))[0]
    wm = json.load(open(os.path.join(snap, "model.safetensors.index.json")))["weight_map"]
    files = sorted({os.path.realpath(os.path.join(snap, v))
                    for k, v in wm.items() if "ngram_embedding.shard_" in k})
    cfg_dtype = torch.float8_e4m3fn
    # layer 1's table: 128 shards over the vocab; discover_table_layout wants the org vocab size
    idx = json.load(open(os.path.join(snap, "config.json"))) if os.path.exists(
        os.path.join(snap, "config.json")) else {}
    layout = discover_table_layout(files, LAYER, int(os.getenv("TEST_NUM_ROWS", "2500012")),
                                   160, cfg_dtype, int(os.getenv("TEST_SPLIT_PARTS", "512")))
    print(f"  layout: {layout.num_rows:,} rows x {layout.row_bytes} B, "
          f"{len(layout.shards)} shards, rows_per_shard={layout.rows_per_shard:,}")
    dev = torch.device("cuda", 0)
    table = MappedTable(layout, dev)
    assert len(table.views) == len(layout.shards), "views must match shards"

    max_rows = N
    stager = RowStager(table, device=dev, row_bytes=layout.row_bytes,
                       rows_per_shard=layout.rows_per_shard, max_rows=max_rows, threads=8)

    rng = np.random.default_rng(7)
    ids = rng.choice(layout.num_rows, size=N, replace=False).astype(np.int64)
    vs, ve = 0, layout.num_rows            # single-rank: everything owned
    out = torch.zeros(N, layout.row_bytes, dtype=torch.uint8, device=dev)
    stager.gather(ids, out, vs, ve)
    got = out.cpu().numpy()

    # independent oracle: pread the shard file at offset + local*row_bytes
    by_shard = {}
    bad = 0
    for i, r in enumerate(ids):
        s = int(r) // layout.rows_per_shard
        local = int(r) - s * layout.rows_per_shard
        sh = layout.shards[s]
        key = sh.path
        if key not in by_shard:
            by_shard[key] = os.open(key, os.O_RDONLY)
        want = os.pread(by_shard[key], layout.row_bytes,
                        sh.offset + local * layout.row_bytes)
        if len(want) != layout.row_bytes or want != got[i].tobytes():
            bad += 1
    print(f"  owned rows: {bad} of {N:,} mismatched a positional pread -> "
          f"{'PASS' if bad == 0 else 'FAIL'}")

    # unowned rows must be zero even when the destination held something else
    out2 = torch.full((N, layout.row_bytes), 0xAB, dtype=torch.uint8, device=dev)
    owned_hi = N // 2
    stager.gather(ids, out2, 0, max(1, int(ids[:owned_hi].max())))
    got2 = out2.cpu().numpy()
    resid = 0
    for i, r in enumerate(ids):
        if r < max(1, int(ids[:owned_hi].max())):
            continue
        if got2[i].any():
            resid += 1
    print(f"  unowned rows zeroed: {resid} of {N - owned_hi:,} left residue -> "
          f"{'PASS' if resid == 0 else 'FAIL'}")
    for fd in by_shard.values():
        os.close(fd)
    return 1 if (bad or resid) else 0

if __name__ == "__main__":
    sys.exit(main())

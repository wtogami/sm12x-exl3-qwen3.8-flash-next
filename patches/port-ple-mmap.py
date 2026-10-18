#!/usr/bin/env python3
"""Apply the reviewed checkpoint-mapped PLE backport to the pinned vLLM base.

Upstream: PR #58439 + stacked PR #58835 (head 047fc1c, main merged in at
4e4eab7) re-anchored onto v0.32.0: the PR's vllm/ diff against its main
parent applies byte-clean (all four touched files are identical between
4e4eab7 and the v0.32.0 tag, verified against the installed image tree).
The common/nvidia ngram_embedding split still holds, so the shared-base
hunks land in qwen4_exp/common/ngram_embedding.py and the CUDA-specific
pageable embedding and loader glue in the nvidia module, restoring the
upstream PR's original file shape.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1]).parent
assets = Path(__file__).parent
for name, expected in json.loads((assets / 'ple-mmap-base-hashes.json').read_text()).items():
    p = root / name
    actual = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
    if actual != expected:
        raise RuntimeError(f'Mmap backport base mismatch: {name}: {actual} != {expected}')
subprocess.run(['patch', '--batch', '--forward', '--fuzz=0', '-p1', '-d', str(root),
                '-i', str((assets / 'ple-mmap-pr58439-58835-v0.32.patch').resolve())], check=True)
print('Applied reviewed mmap PLE backport from PR 58439 + 58835 @ 047fc1c (v0.32.0 rebase)')

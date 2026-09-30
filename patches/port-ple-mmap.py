#!/usr/bin/env python3
"""Apply the reviewed checkpoint-mapped PLE backport to the pinned vLLM base.

Upstream: PR #58439 + stacked PR #58835 (head 47b9933db82d) rebased onto
v0.30.0.  v0.30.0 predates the common/nvidia ngram_embedding split, so the
vendored patch merges the shared-base hunks into
vllm/models/qwen4_exp/nvidia/ngram_embedding.py and adapts the
vllm/config/engram.py validation hunks to that release's EngramConfig shape.
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
                '-i', str((assets / 'ple-mmap-pr58439-58835-v0.30.patch').resolve())], check=True)
print('Applied reviewed mmap PLE backport from PR 58439 + 58835 @ 47b9933db82d (v0.30.0 rebase)')

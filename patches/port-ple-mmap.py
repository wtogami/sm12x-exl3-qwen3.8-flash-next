#!/usr/bin/env python3
"""Apply the reviewed checkpoint-mapped PLE backport to the pinned vLLM base.

Upstream: PR #58439 + stacked PR #58835 (head 47b9933db82d) rebased onto
v0.31.0.  v0.31.0 carries the common/nvidia ngram_embedding split, so the
shared-base hunks land in qwen4_exp/common/ngram_embedding.py and the
CUDA-specific pageable embedding and loader glue in the nvidia module,
restoring the upstream PR's original file shape.  The QSA FP8 KV backport
(PR 55557) shipped upstream in this release and is no longer vendored.
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
                '-i', str((assets / 'ple-mmap-pr58439-58835-v0.31.patch').resolve())], check=True)
print('Applied reviewed mmap PLE backport from PR 58439 + 58835 @ 47b9933db82d (v0.31.0 rebase)')

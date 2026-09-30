#!/usr/bin/env python3
"""Apply upstream PR #55557: fp8_e4m3 main KV cache on the Qwen4Exp QSA path.

v0.30.0 rejects an FP8 main KV cache on QSA; the recipe ships
``--kv-cache-dtype fp8``.  The merged upstream change (main only, commit
dff1bde84dd6, first released in v0.31.0) lands in v0.30.0 with fuzz 0, so it
replaces this recipe's earlier B12x QSA bridge.  QSA keeps BF16 Q/K/V math and
dequantizes the e4m3 cache with the layer's host-side scales inside its own
Triton kernel; the side caches and GDN state remain unchanged.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1]).parent
assets = Path(__file__).parent
for name, expected in json.loads((assets / 'qsa-fp8-base-hashes.json').read_text()).items():
    p = root / name
    actual = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
    if actual != expected:
        raise RuntimeError(f'QSA FP8 backport base mismatch: {name}: {actual} != {expected}')
subprocess.run(['patch', '--batch', '--forward', '--fuzz=0', '-p1', '-d', str(root),
                '-i', str((assets / 'qsa-fp8-pr55557-v0.30.patch').resolve())], check=True)
print('Applied upstream QSA fp8_e4m3 main KV cache backport from PR 55557 @ dff1bde84dd6')

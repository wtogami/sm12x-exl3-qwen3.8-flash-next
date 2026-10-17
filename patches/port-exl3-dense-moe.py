#!/usr/bin/env python3
# Route quantized block-sparse MLPs through the DENSE per-expert path on demand
# (EXV3_FORCE_DENSE=1). Motivation: MultiLinear fusion asserts uniform bitrate
# (K/mcg/mul1) across all experts of a projection, which makes per-expert
# mixed-K cards (e.g. the vendor K4.25 release) unloadable by this runtime even
# though the format is valid. The fork's own comment (block_sparse_mlp.py,
# support_quant_paths block) documents the dense fallback: "configurations with
# any of those run every batch size through the dense per-expert path, which
# handles all of them". The fused/batched tiers are all gated on the same two
# flags (lines 629/852/1115/1318), so clearing them is sufficient and the
# semantics are identical (exact dequant matmul per expert) - just slower.
# Idempotent. Target: runtime tree exv3-env-161 (same tree as port-exl3-measure-ovr.py).
import os, sys

TREE = os.environ.get("EXV3_TREE", os.path.expanduser("~/.cache/huggingface/exv3-env-161"))
P = os.path.join(TREE, "exllamav3/modules/block_sparse_mlp.py")
MARK = "EXV3_FORCE_DENSE"
s = open(P).read()
if MARK in s:
    print("already patched"); sys.exit(0)
anchor = "        # Make fused modules (only used by the quantized fast paths)."
assert s.count(anchor) == 1, "anchor moved; inspect load_local manually"
ins = (
    "        import os as _os  # port-exl3-dense-moe: vendor-style per-expert mixed-K\n"
    "        if _os.environ.get('EXV3_FORCE_DENSE') == '1' and self.is_quantized:\n"
    "            self.support_quant_paths = False\n"
    "            self.support_bc_bszn = False\n"
)
s = s.replace(anchor, ins + anchor)
open(P, "w").write(s)
import ast; ast.parse(s)
print(f"patched {P}")

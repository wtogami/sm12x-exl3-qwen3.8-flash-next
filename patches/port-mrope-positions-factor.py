#!/usr/bin/env python3
"""Keep the M-RoPE cos/sin cache at the v0.31 x4 slack.

Upstream PR 59853 ("M-RoPE cache 4x smaller", shipped in v0.32.0) made
mrope_positions_factor() return 1 whenever multimodal pruning is off,
sizing the MRotaryEmbedding cache at exactly max_position_embeddings.
The Qwen4Exp path indexes deeper than the token position into that same
cache, so serving beyond the checkpoint's native window (YaRN 512K: any
prompt over 262144 tokens) faults: the first chunk past 262144 kills the
worker with an illegal memory access (observed under both GDN prefill
backends; the reporting kernel varies because CUDA errors are async).
Measured on RTX PRO 6000, image v0.32-test: 260067-token probe passes,
280069 fails; with this port restored to 4, 280069 and 480071 pass.
This restores the exact v0.31.0 sizing (max_position_embeddings * 4);
the memory cost is the same table the recipe ran for its whole life.
"""
from pathlib import Path
import sys

root = Path(sys.argv[1])
path = root / "model_executor/models/qwen3_vl.py"
text = path.read_text()

old = "    return 4 if pruned else 1\n"
assert text.count(old) == 1, "mrope_positions_factor return anchor changed"
text = text.replace(
    old,
    "    return 4  # recipe port: Qwen4Exp reads past x1 cache; keep v0.31 x4 slack\n",
)
compile(text, str(path), "exec")
path.write_text(text)
print("Applied mrope positions_factor x4 slack port (upstream 59853 regression)")

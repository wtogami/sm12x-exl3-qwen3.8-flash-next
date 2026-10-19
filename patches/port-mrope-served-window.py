#!/usr/bin/env python3
"""Floor the M-RoPE cos/sin cache on the served window (upstream PR 62409).

Root cause (filed as vllm-project/vllm#62383, fixed by us in #62409):
for scaling rope types the get_rope() factory discards the caller's
max_position and passes original_max_position_embeddings as the
MRotaryEmbedding base, i.e. the PRE-scaling window, while served
positions reach the full YaRN-extended window (max_model_len =
original * factor). The cache was therefore always undersized by the
scaling factor; the blanket 4x headroom (Qwen2.5-VL era, bf3b79efb) had
been masking that for any factor <= 4. PR 59853 made the headroom
conditional (positions_factor=1 for text serving, shipped v0.32.0), so
every YaRN deployment past the native window started reading past the
cos/sin cache. The first consumer to fault is the MTP draft path under
CUDA graph replay (captured with dummy positions, replayed with real
ones): async Xid 31 FAULT_PDE ACCESS_TYPE_VIRT_READ, boundary exactly at
native max_position_embeddings (measured 260067 pass / 280069 fault).
Note the defect is NOT model-side: sizing the cache at the qsa.py
get_rope() call site is a provable no-op because the factory overrides
the caller's base; and the drafter instance needs the same
target_model_config resolution mrope_positions_factor() already uses
(a floor off vllm_config.model_config alone leaves the drafter
undersized and still crashes).

This applies the exact #62409 patch: cache rows =
max(base * positions_factor, target max_model_len).  Byte-identical
sizing to stock v0.32.0 for native-window serving; no reliance on the
4x slack anymore.  This port supersedes port-mrope-positions-factor.py
(the blunt "return 4" workaround), which is removed from the image.
"""
from pathlib import Path
import sys

root = Path(sys.argv[1])
path = root / "model_executor/layers/rotary_embedding/mrope.py"
text = path.read_text()

old = (
    "        self.cache_max_position_num = max_position_embeddings * positions_factor\n"
)
assert text.count(old) == 1, "mrope cache-sizing anchor changed"
new = """        # port (upstream PR 62409): the factory base is the PRE-scaling
        # window for scaling rope types, so floor the cache on the engine's
        # own position bound. Resolution mirrors mrope_positions_factor():
        # a drafter gets the target's positions.
        served_window = 0
        try:
            from vllm.config import get_current_vllm_config

            vllm_config = get_current_vllm_config()
            spec = vllm_config.speculative_config
            model_config = (
                (spec and spec.target_model_config) or vllm_config.model_config
            )
            served_window = int(model_config.max_model_len or 0)
        except Exception:
            # No engine context (unit tests constructing rope directly).
            pass
        self.cache_max_position_num = max(
            max_position_embeddings * positions_factor, served_window
        )
"""
text = text.replace(old, new)
compile(text, str(path), "exec")
path.write_text(text)
print("Applied mrope served-window cache floor port (upstream 62409)")

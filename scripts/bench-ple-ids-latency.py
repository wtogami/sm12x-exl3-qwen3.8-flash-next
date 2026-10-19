#!/usr/bin/env python3
"""CPU-only: how does the engine's host-side compute_ngram_ids cost scale with tokens per step?

The in-engine phase split put `ids` at ~727 us per staged step, which after the gather fix is the larger half
of the remaining exposed host work (~1.1 ms of a ~28 ms decode step). Two explanations fit that number, and
they call for different responses:

  * fixed per-call cost -- a chain of small torch CPU ops whose dispatch overhead dominates the arithmetic,
    the same shape the gather turned out to have. Then the win comes from issuing fewer ops.
  * real arithmetic over the batch -- then it scales with tokens, and only a faster kernel helps.

This measures the curve directly, on the engine's own function, at CPU-only cost so no engine boot is needed.
Geometry comes from the checkpoint config, so the stub `self` carries multipliers and a vocab split of the
same shape the model does; only the tensors are synthetic.
"""
import glob
import json
import os
import sys
import time
from types import SimpleNamespace

import numpy as np
import torch

sys.path.insert(0, "/usr/local/lib/python3.12/dist-packages")
from vllm.models.qwen4_exp.nvidia import ngram_embedding as ne  # noqa: E402

E = ne.Qwen4ExpNGramEmbedding

hub = os.path.expanduser("~/.cache/huggingface/hub")
snap = sorted(glob.glob(os.path.join(hub, "models--*PLE-FP8*", "snapshots", "*")))[0]
cfg = json.load(open(os.path.join(snap, "config.json")))


def find_keys(node, want, out=None):
    out = {} if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            if any(w in k.lower() for w in want) and not isinstance(v, (dict, list)):
                out.setdefault(k, v)
            find_keys(v, want, out)
    elif isinstance(node, list):
        for v in node:
            find_keys(v, want, out)
    return out


keys = find_keys(cfg, ("ngram", "ple", "engram", "head", "vocab_size", "eos"))
print("  config keys that look relevant:")
for k, v in sorted(keys.items()):
    print(f"    {k} = {v}")

heads = int(keys.get("heads_per_ngram", keys.get("ple_heads_per_ngram", 8)))
ngram_size = int(keys.get("ngram_size", keys.get("ple_ngram_size", 3)))
eos = int(keys.get("eos_token_id", 151645))
vocab = int(keys.get("vocab_size", keys.get("text_config.vocab_size", 151936)))
total_heads = heads * max(1, ngram_size - 1)

rng = np.random.default_rng(11)
layer_multipliers = torch.from_numpy(
    (rng.integers(1, 1 << 31, size=total_heads * ngram_size) | 1).astype(np.int64)
)
sizes = torch.from_numpy(np.full((total_heads,), max(1, vocab // ngram_size), dtype=np.int64))
offsets = torch.from_numpy(
    np.cumsum(np.concatenate(([0], sizes.numpy()[:-1]))).astype(np.int64)
)

# The same shape copy_module_for_cpu_ids() builds inside the engine: CPU hash buffers plus the shift helpers.
stub = SimpleNamespace(
    layer_multipliers=layer_multipliers,
    ngram_heads_vocab_sizes=sizes,
    ngram_heads_offsets=offsets,
    eos_token_id=eos,
    ngram_size=ngram_size,
    heads_per_ngram=heads,
    _shift_precompute=E._shift_precompute,
    _shift_apply=E._shift_apply,
)

fn = E.compute_ngram_ids
print(f"  stub geometry: ngram_size={ngram_size} heads_per_ngram={heads}"
      f" => {total_heads} heads, vocab={vocab}, eos={eos}")
print(f"  {'tokens':>7} {'ms/call':>9} {'us/token':>9} {'rows':>7}")
for n_tokens in (16, 54, 128, 512, 2048, 8192):
    ids = torch.from_numpy(rng.integers(0, vocab, size=n_tokens, dtype=np.int64))
    qsl = torch.tensor([0, n_tokens], dtype=torch.int64)
    ctx = torch.from_numpy(rng.integers(0, vocab, size=(1, 4), dtype=np.int64))
    out = fn(stub, ids, qsl, ctx)
    reps = max(5, min(300, 300_000 // max(n_tokens, 1)))
    t0 = time.perf_counter()
    for _ in range(reps):
        fn(stub, ids, qsl, ctx)
    dt = (time.perf_counter() - t0) / reps
    print(f"  {n_tokens:7d} {dt * 1e3:9.3f} {dt * 1e6 / n_tokens:9.2f}"
          f" {int(out.numel()):7d}")

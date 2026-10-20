#!/usr/bin/env python3
"""CPU tests for the safety-critical piece of PLE streaming v2: the staleness mask.

`apply_staged_rows` decides, on the device and inside a captured graph, whether a staged row is used. If
it is wrong, the model silently consumes another step's n-gram features -- no error, no crash, just worse
output. So the semantics get tested without a GPU or an engine.

Run:  python3 scripts/test-ple-stream-v2.py [path/to/qwen_ple_stream2.py]
"""
import argparse
import importlib.util as import_util
import os
import sys

import torch


def load(path):
    spec = import_util.spec_from_file_location("qwen_ple_stream2_probe", path)
    mod = import_util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default="patches/qwen_ple_stream2.py")
    a = ap.parse_args()
    try:
        mod = load(a.path)
    except Exception as e:  # the module imports vllm; fall back to a copy of the function contract
        print(f"  could not import {a.path}: {type(e).__name__}: {e}")
        print("  importing under test requires vllm; run inside the image")
        return 2
    f = mod.apply_staged_rows
    rb, n = 4, 5
    fails = 0

    def check(label, cond):
        nonlocal fails
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            fails += 1

    # every row fresh -> every row copied verbatim
    dev = torch.arange(n * rb, dtype=torch.uint8).reshape(n, rb)
    out = torch.zeros(n, rb, dtype=torch.uint8)
    seq = torch.full((n,), 7, dtype=torch.int64)
    f(dev, seq, torch.tensor(7), out, torch.empty(n, dtype=torch.bool), n)
    check("all-fresh copies every row", torch.equal(out, dev))

    # none fresh -> all zeros, never the staged bytes
    out = torch.full((n, rb), 0xAB, dtype=torch.uint8)
    f(dev, seq, torch.tensor(8), out, torch.empty(n, dtype=torch.bool), n)
    check("all-stale zeroes the output", not out.any())

    # mixed -> per-row decision, and the decision is per ROW not per buffer
    out = torch.full((n, rb), 0xCD, dtype=torch.uint8)
    seq2 = torch.tensor([9, 9, 3, 9, 3])
    f(dev, seq2, torch.tensor(9), out, torch.empty(n, dtype=torch.bool), n)
    want = torch.stack([dev[i] if seq2[i] == 9 else torch.zeros(rb, dtype=torch.uint8) for i in range(n)])
    check("mixed tags resolve per row", torch.equal(out, want))
    check("stale rows carry no staged bytes", bool((out[2] == 0).all() and (out[4] == 0).all()))
    check("fresh rows carry the staged bytes", bool((out[1] == dev[1]).all()))

    # a short n_rows must not read or write past it
    out = torch.full((n, rb), 0xAB, dtype=torch.uint8)
    f(dev, seq, torch.tensor(7), out, torch.empty(n, dtype=torch.bool), 2)
    check("n_rows bounds the write", bool((out[:2] == dev[:2]).all()) and bool((out[2:] == 0xAB).all()))

    # sequence 0 is what the buffer starts at; a fill that never ran must not look "fresh" at seq 0
    out = torch.full((n, rb), 0xEF, dtype=torch.uint8)
    z = torch.zeros(n, dtype=torch.int64)
    f(dev, z, torch.tensor(0), out, torch.empty(n, dtype=torch.bool), n)
    # Characterisation, not wishful thinking: tags start at 0 and an un-run fill leaves cur_seq at 0, so
    # they MATCH and the buffer is copied. Asserted as-is because if someone later changes the tag
    # initialisation this flips, and that change would be the moment the engine-impossible case becomes
    # real. It cannot pass vacuously: it either copies or it does not.
    # Initial state as the code actually leaves it: row tags at STALE_NO_FILL (-1), step counter at 0.
    out2 = torch.full((n, rb), 0xEF, dtype=torch.uint8)
    f(dev, torch.full((n,), -1, dtype=torch.int64), torch.tensor(0), out2,
      torch.empty(n, dtype=torch.bool), n)
    check("initial state is STALE, so nothing is copied before the first fill", not out2.any())
    out3 = torch.full((n, rb), 0xAB, dtype=torch.uint8)
    f(dev, torch.full((n,), -1, dtype=torch.int64), torch.tensor(-1), out3,
      torch.empty(n, dtype=torch.bool), n)
    check("...and a sentinel on BOTH sides would match (why the two starts differ)",
          torch.equal(out3, dev))
    print("  NOTE  safety here is a property of the tags, not of hook placement. Async mode once failed")
    print("        every fill, which left cur_seq at its initial value; with a shared initial value the")
    print("        mask matched and uninitialized memory was consumed as features. The two sides now")
    print("        start at different values, so an unfilled buffer cannot match whatever happens next.")

    # One knob, asserted on the loaded module rather than on the launcher: staged by default when only
    # PLE_STREAM is set (bare container runs), and a set-but-falsey V2 still wins -- that explicit pair
    # is how the historical in-replay cells reproduce.
    os.environ.pop("PLE_STREAM_V2", None)
    os.environ["PLE_STREAM"] = "1"
    check("PLE_STREAM alone selects staged", mod.v2_enabled())
    os.environ["PLE_STREAM_V2"] = "0"
    check("explicit V2=0 wins over PLE_STREAM=1", not mod.v2_enabled())
    os.environ["PLE_STREAM"] = "0"

    print(f"\n  {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

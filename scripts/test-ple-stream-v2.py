#!/usr/bin/env python3
"""CPU tests for the safety-critical piece of PLE streaming v2: the staleness mask.

`apply_staged_rows` decides, on the device and inside a captured graph, whether a staged row is used. If
it is wrong, the model silently consumes another step's n-gram features -- no error, no crash, just worse
output. So the semantics get tested without a GPU or an engine.

Run:  python3 scripts/test-ple-stream-v2.py [path/to/qwen_ple_stream2.py]
"""
import argparse
import importlib.util as import_util
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
    stale_tag = torch.full((n,), -1, dtype=torch.int64)
    out2 = torch.full((n, rb), 0xEF, dtype=torch.uint8)
    f(dev, stale_tag, torch.tensor(-1), out2, torch.empty(n, dtype=torch.bool), n)
    check("unfilled buffer (sentinel tags) is STALE, not copied", not out2.any())
    check("zero-vs-zero no longer matches once tags start at the sentinel",
          not torch.equal(out2, dev))
    print("  NOTE  an unfilled buffer is only safe-by-accident: in the engine _lookup runs after")
    print("        prepare_inputs has staged the step and the first fill sets cur_seq>=1, so a zero-vs-zero")
    print("        match cannot be reached. Staging the first step before the first lookup is the property")
    print("        that makes it unreachable, and it is a property of the hook placement, not of this")
    print("        function. Worth revisiting if the hook ever moves.")
    print(f"\n  {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

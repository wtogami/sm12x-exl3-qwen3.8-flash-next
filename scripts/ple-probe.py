#!/usr/bin/env python3
"""Determinism + throughput probe for PLE backend comparisons. Run with: python3 -I <this> 

Correctness first, and wider than the 392-byte greedy check it replaces. A row gather that reads the
wrong n-gram rows can still produce a plausible short answer, so hashing two short replies proves less
than it looks like it does. This hashes six greedy completions of 64 tokens each across deliberately
different regimes (long context, code, arithmetic, verbatim repetition, multilingual, multi-step), where
a corrupted PLE contribution has much more room to show.

Prints machine-parseable lines the matrix driver diffs:
  aggregate <sha256-16>       over the concatenation of all six completions
  correct <yes|no>            against --expect, when given
  prefill <tok/s>  decode <tok/s>
  tokens <n>                  total completion tokens hashed (so an empty run cannot look "identical")

Nothing is randomised: temperature 0, fixed seed, fixed prompt list, fixed order. A difference between
configurations is then the configuration, not the workload.
"""
import argparse
import hashlib
import json
import statistics
import time
import urllib.request

PROMPTS = [
    ("long-context", "The quick brown fox jumps over the lazy dog. " * 220 +
     "\n\nIn one paragraph, what changed if I replaced 'fox' with 'owl' throughout, and why?"),
    ("code", "Write a Python function `shard_of(row_id, rows_per_shard)` that returns the shard index "
             "and local offset, then explain the integer arithmetic line by line."),
    ("arithmetic", "A table is 320,001,536 rows of 160 bytes. How many GiB is that? Show the division "
                   "steps, then say how many 4 KiB pages it needs if every row is fetched alone."),
    ("verbatim", "Repeat exactly, with no commentary: page cache hits 0.744 at 8 GiB, 0.797 at 16 GiB."),
    ("multilingual", "Erkläre in zwei Sätzen, warum CUDA Graphs keine Host-Copy enthalten dürfen. "
                     "Antworte auf Deutsch."),
    ("multi-step", "List three reasons a compressed btrfs extent costs more device traffic than an "
                   "uncompressed one for random 160-byte reads, then say which reason dominates and why."),
]
LONG = ("The quick brown fox jumps over the lazy dog. " * 700)[:14000]
SHORT = "Define page-cache reuse distance in one sentence."


def post(url, model, prompt, max_tokens):
    body = json.dumps({"model": model, "prompt": prompt, "max_tokens": max_tokens,
                       "temperature": 0, "seed": 11}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.load(r)
    txt = d["choices"][0]["text"]
    return txt, d["usage"]["prompt_tokens"], d["usage"]["completion_tokens"], time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8001/v1/completions")
    ap.add_argument("--model", default="qwen38-exl3-ple8")
    ap.add_argument("--expect", default="", help="aggregate sha to match (16-hex prefix ok)")
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--reps", type=int, default=4, help="repeats for the throughput shapes")
    a = ap.parse_args()

    parts, total_tokens = [], 0
    for name, prompt in PROMPTS:
        try:
            txt, ptok, ctok, el = post(a.url, a.model, prompt, a.max_tokens)
        except Exception as e:  # a dead engine must not look like a matching hash
            print(f"  request failed on {name}: {type(e).__name__}: {e}")
            print("aggregate ERROR")
            print("correct no")
            return 2
        parts.append(f"[{name}]{txt}")
        total_tokens += ctok
        print(f"  {name:13s} prompt={ptok:6d} tok  completion={ctok:3d} tok  {el:.2f}s")

    blob = "\n".join(parts).encode()
    agg = hashlib.sha256(blob).hexdigest()[:16]
    print(f"aggregate {agg}")
    print(f"tokens {total_tokens}")
    if a.expect:
        ok = agg.startswith(a.expect)
        print(f"correct {'yes' if ok else 'no'}")
        if not ok:
            print("--- first divergence ---")
            print(blob.decode(errors="replace")[:400])
    # throughput: median of reps, two shapes
    for label, prompt, mt in (("prefill", LONG, 1), ("decode", SHORT, 256)):
        rates = []
        for _ in range(a.reps):
            _, ptok, ctok, el = post(a.url, a.model, prompt, mt)
            rates.append((ptok if mt == 1 else ctok) / el)
        print(f"{label} {statistics.median(rates):.1f} tok/s")
    return 0 if not a.expect or agg.startswith(a.expect) else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Determinism + throughput probe for PLE backend comparisons. Run: python3 -I <this>

Correctness first, and wider than the 392-byte greedy check it replaces. A row gather that reads the
wrong n-gram rows can still produce a plausible short answer, so hashing two short replies proves less
than it looks like it does. This hashes greedy 64-token completions of six prompts across regimes where
a corrupted PLE contribution has room to show (long context, code, arithmetic, verbatim repetition,
multilingual, multi-step).

ONLY SINGLE-CHUNK PROMPTS ARE GATED. Measured on the deployed pinned engine: repeated identical requests
at temperature 0 reproduce while prompt_tokens <= max_num_batched_tokens (2048 here) and do not above it,
where prefill is chunked -- five back-to-back calls of one 2110-token prompt produced three different
completions. Prefix caching is exonerated (103,379 queries, 0 hits). A longer 4010-token prompt looked
stable over three runs, so no tail-size rule is claimed: the boundary the data supports is "needs chunked
prefill". The long prompt is therefore hashed into `all` and reported, never gated -- gating on an
irreproducible prompt manufactures false failures and can equally manufacture a false pass.

Prints machine-parseable lines the matrix driver diffs:
  strict <sha256-16>     over completions whose prompt_tokens <= --chunk-limit  (the gate)
  all    <sha256-16>     over every completion, chunked ones included
  tokens <n>             completion tokens hashed, so an empty run cannot read as a match
  correct <yes|no>       strict vs --expect, when given
  prefill <tok/s>  decode <tok/s>

A hash is an equality test, not a distance metric. When two regimes disagree it cannot distinguish one
early token flipping on a near-tie and then cascading through 64 greedy tokens from a genuinely wrong
PLE contribution -- both read as "the hash differs". --dump records the per-prompt text so --diff can
report how far the two runs agree before they part.
"""
import argparse
import hashlib
import json
import os
import statistics
import sys
import time
import urllib.request

LONG_UNIT = "The quick brown fox jumps over the lazy dog. "
PROMPTS = [
    ("long-context", LONG_UNIT * 220 + "\n\nIn one paragraph, what changed if I replaced 'fox' with "
     "'owl' throughout, and why?"),
    ("code", "Write a Python function `shard_of(row_id, rows_per_shard)` that returns the shard index "
             "and local offset, then explain the integer arithmetic line by line."),
    ("arithmetic", "A table is 320,001,536 rows of 160 bytes. How many GiB is that? Show the division "
                   "steps, then say how many 4 KiB pages it needs if every row is fetched alone."),
    ("verbatim", "Repeat exactly, with no commentary: page cache hits 0.744 at 8 GiB, 0.797 at 16 GiB."),
    ("multilingual", "Erkläre in zwei Sätzen, warum CUDA Graphs keine Host-Copy enthalten dürfen. "
                     "Antworte auf Deutsch."),
    ("multi-step", "List three reasons a compressed btrfs extent costs more device traffic than an "
                   "uncompressed one for random 160-byte reads, then say which reason dominates and "
                   "why."),
]
SHORT = "Define page-cache reuse distance in one sentence."


def diff_dumps(path, la, lb):
    """Report how far apart two labelled dumps of the same prompts are.

    The interesting quantity is the shared prefix. Greedy decoding cannot recover from one flipped token,
    so a long common prefix that then parts is the signature of a near-tie rounding the other way, while
    parting at character zero of every prompt means the two runs are computing something different.
    Returns the number of prompts that differ so the caller can exit non-zero.
    """
    by = {la: {}, lb: {}}
    for line in open(path):
        if line.strip():
            r = json.loads(line)
            if r["label"] in by:
                by[r["label"]][r["name"]] = r["text"]
    if set(by[la]) ^ set(by[lb]):
        print(f"  the two labels cover different prompts: {sorted(set(by[la]) ^ set(by[lb]))}")
        return -1
    if not by[la]:
        print(f"  no rows for {la!r}/{lb!r} in {path}")
        return -1
    print(f"  comparing {la} vs {lb} over {len(by[la])} prompts")
    differing = 0
    for name in [n for n, _ in PROMPTS] + sorted(set(by[la]) - {n for n, _ in PROMPTS}):
        if name not in by[la]:
            continue
        a, b = by[la][name], by[lb][name]
        if a == b:
            print(f"  {name:13s} identical ({len(a)} chars)")
            continue
        differing += 1
        i = 0
        while i < min(len(a), len(b)) and a[i] == b[i]:
            i += 1
        print(f"  {name:13s} DIFFERS after {i} chars (~{len(a[:i].split())} words) of {len(a)}/{len(b)}")
        print(f"      shared tail: ...{a[max(0, i - 40):i]!r}")
        print(f"      {la:<11s}: {a[i:i + 60]!r}")
        print(f"      {lb:<11s}: {b[i:i + 60]!r}")
    print(f"diff {'IDENTICAL' if differing == 0 else f'{differing} of {len(by[la])} prompts differ'}")
    return differing


def resolve_model(completions_url, opener=None):
    """Ask the engine which model it serves, instead of assuming.

    The two profiles on this branch serve different names -- `qwen38-exl3-ple8` for the FP8 PLE table,
    `qwen38-exl3` for BF16 -- so any hard-coded default is wrong for one of them. The failure is easy to
    misread: a name mismatch makes every request 404, which looks exactly like a broken engine, and the
    drivers switch profiles between hosts routinely. Deriving the id also catches a container that came up
    with an unexpected model, because the resolved name is printed.
    """
    base = completions_url.rsplit("/v1/", 1)[0]
    open_fn = opener or urllib.request.urlopen
    with open_fn(urllib.request.Request(base + "/v1/models"), timeout=60) as r:
        data = json.load(r)
    ids = [m["id"] for m in data.get("data", []) if m.get("id")]
    if not ids:
        raise RuntimeError(f"{base}/v1/models returned no model ids")
    return ids[0]


def post(url, model, prompt, max_tokens):
    body = json.dumps({"model": model, "prompt": prompt, "max_tokens": max_tokens,
                       "temperature": 0, "seed": 11}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.load(r)
    return (d["choices"][0]["text"], d["usage"]["prompt_tokens"],
            d["usage"]["completion_tokens"], time.time() - t0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8001/v1/completions")
    ap.add_argument("--model", default="",
                    help="served model name; empty resolves it from the engine's /v1/models")
    ap.add_argument("--expect", default="", help="strict hash to match (16-hex prefix ok)")
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--chunk-limit", type=int, default=2048,
                    help="prompt_tokens at or below this are single-chunk, hence reproducible")
    ap.add_argument("--reps", type=int, default=4, help="timed repeats for the throughput shapes")
    ap.add_argument("--warmup", type=int, default=2,
                    help="discarded requests per shape before timing; a cold cell measures "
                         "roughly 30 percent low on prefill")
    ap.add_argument("--dump", default="", help="append strict-set completion text to this JSONL")
    ap.add_argument("--label", default="unlabelled", help="label for --dump rows (the matrix cell name)")
    ap.add_argument("--diff", nargs=3, metavar=("FILE", "LABEL_A", "LABEL_B"),
                    help="offline: how far apart two dumps part; needs no server")
    a = ap.parse_args()

    if a.diff:
        return 0 if diff_dumps(*a.diff) == 0 else 1

    try:
        model = a.model or resolve_model(a.url)
    except Exception as e:                      # a missing engine must not look like an empty model name
        print(f"  could not resolve the served model: {type(e).__name__}: {e}")
        print("strict ERROR")
        print("correct no")
        return 2
    print(f"model {model}")

    parts, total = [], 0
    for name, prompt in PROMPTS:
        try:
            txt, ptok, ctok, el = post(a.url, model, prompt, a.max_tokens)
        except Exception as e:  # a dead engine must not look like a matching hash
            print(f"  request failed on {name}: {type(e).__name__}: {e}")
            print("strict ERROR")
            print("correct no")
            return 2
        parts.append((name, ptok, txt))
        total += ctok
        flag = "" if ptok <= a.chunk_limit else "   (chunked: reported, not gated)"
        print(f"  {name:13s} prompt={ptok:6d} tok  completion={ctok:3d} tok  {el:.2f}s{flag}")

    def sha(items):
        return hashlib.sha256("\n".join(f"[{n}]{x}" for n, _, x in items).encode()).hexdigest()[:16]

    strict_items = [x for x in parts if x[1] <= a.chunk_limit]
    if not strict_items:
        print("  every prompt exceeded the chunk limit; nothing can be gated")
        print("strict NONE")
        return 2
    strict = sha(strict_items)
    print(f"strict {strict}")
    if a.dump:
        with open(a.dump, "a") as f:
            for n, ptok, txt in strict_items:
                f.write(json.dumps({"label": a.label, "name": n, "prompt_tokens": ptok,
                                    "strict": strict, "text": txt}) + "\n")
        print(f"dumped {len(strict_items)} gated completions to {a.dump} as {a.label!r}")
    print(f"all {sha(parts)}")
    print(f"tokens {total}")
    print(f"gated {len(strict_items)}/{len(parts)} prompts")

    for label, prompt, mt in (("prefill", LONG_UNIT * 700, 1), ("decode", SHORT, 256)):
        # Warm up before timing. A freshly booted cell measured 8,205 tok/s prefill against 11,000-11,900
        # for the SAME configuration on a long-running engine, while decode agreed to 0.5% -- the gap is
        # cold kernel selection / autotuning and page-cache state, not the thing under test. Without this
        # every cell would be a measurement of how recently it booted.
        for _ in range(a.warmup):
            post(a.url, model, prompt, mt)
        rates, pt0 = [], 0
        for _ in range(a.reps):
            _, ptok, ctok, el = post(a.url, model, prompt, mt)
            pt0 = ptok
            rates.append((ptok if mt == 1 else ctok) / el)
        # Print the shape, not just the rate. A prefill number measured without the truncation on the
        # prompt is not comparable to one measured with it, and dropping that truncation in a rewrite is
        # exactly how I nearly reported a 2.2x instrument failure that was a change of definition.
        print(f"{label} {statistics.median(rates):.1f} tok/s "
              f"(prompt={pt0} tok, completion={'1' if mt == 1 else mt}, "
              f"reps={a.reps}, warmup={a.warmup}, prompt_chars={len(prompt)})")

    if a.expect:
        ok = strict.startswith(a.expect)
        print(f"correct {'yes' if ok else 'no'}")
        if not ok:
            print("--- strict-set text (first 160 chars each) ---")
            for n, _, x in strict_items:
                print(f"  [{n}] {x[:160]}")
    return 0 if not a.expect or strict.startswith(a.expect) else 1


if __name__ == "__main__":
    raise SystemExit(main())

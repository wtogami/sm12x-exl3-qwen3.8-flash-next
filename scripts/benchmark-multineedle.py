#!/usr/bin/env python3
"""Multi-needle retrieval curve over the native context window.

Screens the depth at which exact recall decays as needle count grows.  Each
cell places K verification keys at stratified-jittered positions inside a
fixed-length filler archive, asks for all K values in one greedy turn, and
scores per needle (substring presence of each exact value) plus a stricter
sample-level all-or-nothing count.  The K=1 arm reproduces the single-needle
archive format from test-context-retrieval.py so the older credential stays
comparable.  Depths are prompt-token budgets; the final prompt always fits
the measured window because the filler is shortened by the key/instruction
reserve.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import uuid


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


prefill = module("prefill", "benchmark-prefill.py")
workloads = module("workloads", "benchmark-workloads.py")

# Dense below the transition zone the literature suggests, then steps of
# 32K up to the exact native ceiling; the ceiling cell also doubles as a
# position-boundary sanity probe for the un-scaled window.
DEFAULT_DEPTHS = [8192, 16384, 32768, 49152, 65536, 98304, 131072, 163840,
                  196608, 229376, 262144]


def needle_pair(seed, depth, needles, sample, index):
    stem = f"{seed}|{depth}|{needles}|{sample}|{index}"
    name = "ARCHIVE_VERIFICATION_KEY" if needles == 1 else f"ARCHIVE_VERIFICATION_KEY_{index + 1}"
    value = hashlib.sha256(("value:" + stem).encode()).hexdigest()[:16]
    return name, value


def placement(seed, depth, needles, sample, index):
    """Stratified position: stratum centre plus deterministic +/-30% jitter."""
    stem = f"{seed}|{depth}|{needles}|{sample}|{index}"
    jitter = int(hashlib.sha256(("pos:" + stem).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return (index + 0.5 + 0.3 * (jitter - 0.5)) / needles


def build_prompt(filler, pairs, positions):
    order = sorted(range(len(pairs)), key=lambda i: positions[i])
    parts, cursor = [], 0
    for i in order:
        offset = min(round(len(filler) * positions[i]), len(filler))
        parts.append(filler[cursor:offset])
        parts.append(f"{pairs[i][0]} = {pairs[i][1]}\n")
        cursor = offset
    parts.append(filler[cursor:])
    names = [name for name, _ in pairs]
    if len(names) == 1:
        head = f"Read this archive and find its {names[0]}.\n"
        tail = f"\nEnd of archive. Return only the exact {names[0]}, without explanation."
    else:
        head = "Read this archive and find its " + ", ".join(names) + ".\n"
        tail = ("\nEnd of archive. Return only the exact value of each key, "
                "one value per line, without explanation.")
    return head + "".join(parts) + tail


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--model", required=True)
    parser.add_argument("--depths", type=int, nargs="+", default=DEFAULT_DEPTHS)
    parser.add_argument("--needles", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--samples", type=int, default=25)
    parser.add_argument("--seed", type=int, default=787)
    parser.add_argument("--reserve", type=int, default=192, help="token budget for key lines and instruction")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.base_url = args.base_url.rstrip("/").removesuffix("/v1")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        output.write(json.dumps({"record": "meta", "schema": "qwen38-multineedle-v1",
                                 "args": vars(args) | {"output": str(args.output)},
                                 "method": "K keys at stratified-jittered character positions in "
                                           "fixed-token filler; per-needle exact-substring scoring; "
                                           "K=1 mirrors the single-needle archive format"}) + "\n")

        def emit(row):
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()

        for depth in sorted(args.depths):
            filler = prefill.exact_prompt(args.base_url, args.model,
                                         max(depth - args.reserve, 256), uuid.uuid4().hex)
            for needles in sorted(args.needles):
                ok_needles = ok_samples = 0
                ttfts = []
                for sample in range(args.samples):
                    pairs = [needle_pair(args.seed, depth, needles, sample, i) for i in range(needles)]
                    positions = [placement(args.seed, depth, needles, sample, i) for i in range(needles)]
                    prompt = build_prompt(filler, pairs, positions)
                    result = workloads.request(args.base_url, args.model, prompt,
                                               max(32, 24 * needles + 32))
                    content = result.pop("content", "")
                    usage = result.get("usage") or {}
                    flags = [value in content for _, value in pairs]
                    ok_needles += sum(flags)
                    ok_samples += all(flags)
                    ttfts.append(result["ttft_seconds"])
                    emit({"record": "measurement", "depth": depth, "needles": needles,
                          "sample": sample, "expected": [value for _, value in pairs],
                          "needle_hits": flags,
                          "response": content.strip()[:400], **result})
                cell = {"record": "cell", "depth": depth, "needles": needles,
                        "samples": args.samples,
                        "needle_accuracy": round(ok_needles / (args.samples * needles), 4),
                        "sample_accuracy": round(ok_samples / args.samples, 4),
                        "median_ttft_seconds": round(sorted(ttfts)[len(ttfts) // 2], 3)}
                emit(cell)
                print(f"depth={depth:>6} K={needles} needles={cell['needle_accuracy']:.3f} "
                      f"all-or-nothing={cell['sample_accuracy']:.3f} "
                      f"ttft={cell['median_ttft_seconds']:.2f}s", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run the BABILong reasoning-in-a-haystack matrix against our OpenAI endpoint.

Conventions replicate the upstream evaluation (babilong_eval_openai_api_models
notebook + babilong/metrics.py from the booydar/babilong clone): instruction +
two-shot examples + post_prompt, <context> wrapper, system "You are a
intelligent assistant.", temperature 0, max_tokens 20, strict label-based
scoring.  Samples are a deterministic stride subset of the released 100 per
cell so receipts are reproducible.  Lengths run in the order given so a
worst-case-first schedule reports the deepest cells first; prompts over the
served window are recorded as over-window skips rather than failures.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


def load_sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


workloads = load_sibling("benchmark-workloads.py")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--model", required=True)
    parser.add_argument("--repo", type=Path, required=True, help="booydar/babilong checkout")
    parser.add_argument("--data-dir", type=Path, required=True, help="downloaded <task>_<len>.json files")
    parser.add_argument("--tasks", nargs="+", default=["qa1", "qa2", "qa3", "qa4", "qa5", "qa7"])
    parser.add_argument("--lengths", nargs="+", default=["512k", "256k", "128k", "64k", "8k", "0k"])
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.base_url = args.base_url.rstrip("/").removesuffix("/v1")

    sys.path.insert(0, str(args.repo))
    from babilong.prompts import DEFAULT_PROMPTS, DEFAULT_TEMPLATE, get_formatted_input
    from babilong.metrics import TASK_LABELS, compare_answers

    args.output.parent.mkdir(parents=True, exist_ok=True)
    system_prompt = "You are a intelligent assistant."
    with args.output.open("x") as output:
        def emit(row):
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()

        emit({"record": "meta", "schema": "qwen38-babilong-v1",
              "args": {k: str(v) for k, v in vars(args).items()},
              "method": "upstream prompts + strict compare_answers; deterministic stride "
                        "subset of released samples; over-window prompts recorded as skips"})
        for length in args.lengths:
            for task in args.tasks:
                data = json.loads((args.data_dir / f"{task}_{length}.json").read_text())
                stride = max(len(data) // args.samples, 1)
                picked = list(range(0, len(data), stride))[:args.samples]
                cfg = DEFAULT_PROMPTS[task]
                correct = answered = skipped = 0
                ttfts = []
                for idx in picked:
                    sample = data[idx]
                    text = get_formatted_input(sample["input"], sample["question"],
                                               cfg["examples"], cfg["instruction"],
                                               cfg["post_prompt"], template=DEFAULT_TEMPLATE)
                    try:
                        result = workloads.request(args.base_url, args.model, text, 20)
                    except Exception as exc:  # noqa: BLE001 - count 400/window rejects
                        if "400" in str(exc) or "maximum context length" in str(exc):
                            skipped += 1
                            emit({"record": "measurement", "task": task, "length": length,
                                  "sample": idx, "skipped": "over-window", "error": str(exc)[:200]})
                            continue
                        raise
                    ok = compare_answers(sample["target"], result["content"],
                                         sample["question"], TASK_LABELS[task])
                    correct += ok
                    answered += 1
                    ttfts.append(result["ttft_seconds"])
                    emit({"record": "measurement", "task": task, "length": length, "sample": idx,
                          "target": sample["target"], "response": result["content"].strip()[:160],
                          "correct": ok, "question": sample["question"].strip(),
                          "usage": result["usage"], "ttft_seconds": round(result["ttft_seconds"], 3)})
                cell = {"record": "cell", "task": task, "length": length,
                        "samples": args.samples, "answered": answered, "over_window_skipped": skipped,
                        "accuracy": round(correct / answered, 4) if answered else None,
                        "median_ttft_seconds": round(sorted(ttfts)[len(ttfts) // 2], 2) if ttfts else None}
                emit(cell)
                print(f"{task} {length}: acc={cell['accuracy']} answered={answered} "
                      f"skipped={skipped} ttft={cell['median_ttft_seconds']}", flush=True)


if __name__ == "__main__":
    main()

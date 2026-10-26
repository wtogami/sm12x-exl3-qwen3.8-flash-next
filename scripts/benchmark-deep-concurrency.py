#!/usr/bin/env python3
"""Worst-case concurrency: N simultaneous full-prefill requests at a set fraction of the
served context window.

The util ladder answers how big the KV pool is and how much margin a boot leaves; it
answers the concurrency question only arithmetically (pool tokens divided by request
length). This measures it: N distinct, prefix-cache-defeating prompts, each prefilled
from scratch, fired together, while the engine's own counters are sampled. Preemptions
and waiting depth are the evidence that matters -- a pool that "fits" 2 x 512K by
division can still preempt a third arrival, and the user-visible cost of that is TTFT,
not an error.

Depths are given as fractions of the window the server reports, not token counts, so
the same cell means the same thing on a native 262144 host and a YaRN 524288 one.
"""

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
from pathlib import Path
import threading
import time
import urllib.request
import uuid

HERE = Path(__file__).with_name


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE(filename))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


prefill = module("prefill", "benchmark-prefill.py")
context = module("context", "benchmark-context.py")


def metrics(base):
    """Fetch /metrics and reduce to the three counters this probe cares about."""
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/metrics", timeout=30) as response:
            body = response.read().decode()
    except Exception:
        return None
    wanted = ("vllm:num_preemptions_total", "vllm:num_requests_waiting",
              "vllm:num_requests_running", "vllm:gpu_cache_usage_perc")
    out = {}
    for line in body.splitlines():
        name = line.split("{", 1)[0].split(" ", 1)[0]
        if name in wanted and name not in out:
            try:
                out[name] = float(line.rsplit(" ", 1)[1])
            except (IndexError, ValueError):
                pass
    return out


class Sampler(threading.Thread):
    """Poll engine counters while the cell runs. The engine counts preemptions
    cumulatively and never reports a peak, so the peak has to be observed."""

    def __init__(self, base, interval=1.0):
        super().__init__(daemon=True)
        self.base, self.interval = base, interval
        self.stop_at, self.samples = threading.Event(), []

    def run(self):
        while not self.stop_at.is_set():
            sample = metrics(self.base)
            if sample:
                self.samples.append(sample)
            self.stop_at.wait(self.interval)

    def peak(self, key):
        values = [s[key] for s in self.samples if key in s]
        return max(values) if values else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--model", required=True)
    parser.add_argument("--fractions", type=float, nargs="+", default=[0.5, 1.0],
                        help="prompt depth as a fraction of the server's max_model_len")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[2, 3, 4])
    parser.add_argument("--output-tokens", type=int, default=128)
    parser.add_argument("--request-timeout", type=float, default=900.0)
    parser.add_argument("--reserve-tokens", type=int, default=512,
                        help="headroom kept below the window for the chat template wrapper")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = args.base_url.rstrip("/").removesuffix("/v1")
    if not 0 < min(args.fractions) or max(args.fractions) > 1:
        parser.error("--fractions must be in (0, 1]")
    if min(args.concurrency) < 1 or args.output_tokens < 2:
        parser.error("concurrency >= 1 and output_tokens >= 2")

    with urllib.request.urlopen(base + "/v1/models", timeout=60) as response:
        served = json.load(response)["data"][0]
    window = int(served["max_model_len"])
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with args.output.open("x") as output:
        output.write(json.dumps({"record": "meta", "schema": "qwen38-deep-concurrency-v1",
                                 "args": {**vars(args), "output": str(args.output)},
                                 "served_model": served["id"], "max_model_len": window,
                                 "method": "N distinct exact-length prompts with unique nonces "
                                           "(so no prefix cache reuse), streamed from "
                                           "/v1/completions with min_tokens forced; engine "
                                           "counters sampled at 1 Hz during each cell"}) + "\n")
        for fraction in args.fractions:
            depth = int(fraction * window) - args.output_tokens - args.reserve_tokens
            if depth < 1024:
                continue
            for n in args.concurrency:
                nonces = [uuid.uuid4().hex for _ in range(n)]
                # Build prompts before the clock starts: tokenizing 500K tokens is not
                # something the engine should be charged for.
                prompts = [prefill.exact_prompt(base, args.model, depth, nonce)
                           for nonce in nonces]
                before = metrics(base)
                sampler = Sampler(base)
                sampler.start()
                started = time.perf_counter()

                def fire(prompt):
                    try:
                        return {"ok": True, **context.measure(
                            base, args.model, prompt, args.output_tokens,
                            seed=20260824)}
                    except Exception as error:  # a refused/timeouted cell is a result
                        return {"ok": False, "error": f"{type(error).__name__}: {error}"[:300]}

                with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
                    results = list(pool.map(fire, prompts))
                wall = time.perf_counter() - started
                time.sleep(3)  # let the last counter scrape land
                sampler.stop_at.set()
                sampler.join(timeout=10)
                after = metrics(base)
                ok = [r for r in results if r.get("ok")]
                row = {"record": "measurement", "fraction": fraction, "depth_tokens": depth,
                       "concurrency": n, "wall_seconds": round(wall, 2),
                       "requests_ok": len(ok), "requests_failed": len(results) - len(ok),
                       "ttft_seconds": [round(r["ttft_seconds"], 2) for r in ok],
                       "decode_tps": [round(r["decode_tps"], 1) for r in ok],
                       "prompt_tokens": [r["usage"]["prompt_tokens"] for r in ok],
                       "preemptions": (after["vllm:num_preemptions_total"]
                                       - before["vllm:num_preemptions_total"])
                                      if before and after and
                                      "vllm:num_preemptions_total" in before and
                                      "vllm:num_preemptions_total" in after else None,
                       "peak_waiting": sampler.peak("vllm:num_requests_waiting"),
                       "peak_running": sampler.peak("vllm:num_requests_running"),
                       "peak_gpu_cache_usage": sampler.peak("vllm:gpu_cache_usage_perc"),
                       "errors": [r["error"] for r in results if not r.get("ok")],
                       "prompts_sha256": [hashlib.sha256(p.encode()).hexdigest()[:16]
                                          for p in prompts]}
                output.write(json.dumps(row) + "\n")
                output.flush()
                print(json.dumps({k: row[k] for k in
                                  ("fraction", "depth_tokens", "concurrency", "requests_ok",
                                   "preemptions", "peak_waiting", "wall_seconds")}))


if __name__ == "__main__":
    main()

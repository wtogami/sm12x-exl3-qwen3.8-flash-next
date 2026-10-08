#!/usr/bin/env python3
"""SM clock-lock sweep: TTFT + decode + power at each locked clock.

The power limit is pinned to the card maximum (600 W) so the clock lock is
the only intended limiter; a background thread samples power.draw /
clocks.sm / temperature at 2 Hz throughout. At each point, after a settle
delay: one 131K warmup (also establishes the effective locked clock under
load), 2x 523K prefill, 2x 131K prefill, and a C1 decode block
(benchmark-decode.py, 2 warmups + 3 runs).

Prefill power windows are attributed exactly: the exact prompt is built
before t0, so [t0, t0 + ttft] is the prefill and [t0 + ttft, t1] the decode
tail. Decode block windows are the whole invocation (short prompts,
decode-dominated).

Requires sudo nvidia-smi (for -lgc/-rgc/-pl). The EXIT path resets clocks
and the power limit.

Usage:
  QUANT=exl3 MODEL=qwen38-exl3 OUT=benchmarks/clock-sweep-20261007/exl3 \
    python3 scripts/clock-sweep.py

Env:
  QUANT          profile label written into the receipts (exl3|nvfp4)
  MODEL          served model name on the engine
  OUT            output directory (sweep.jsonl + power.csv + per-run files)
  CLOCKS         space-separated points: "unlocked" and/or MHz values
                 (default: unlocked 1300 1600 1900 2200 2450 2700 2900)
  BASE_URL       engine base URL (default http://127.0.0.1:8001)
  SETTLE         seconds to wait after each lock change (default 90)
  DECODE_TOKENS  output tokens per decode run (default 4096)
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPT_DIR = Path(__file__).resolve().parent
prefill = load_module("prefill", SCRIPT_DIR / "benchmark-prefill.py")
ctx = load_module("ctx", SCRIPT_DIR / "benchmark-context.py")

QUANT = os.environ["QUANT"]
MODEL = os.environ["MODEL"]
OUT = Path(os.environ["OUT"])
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8001").rstrip("/")
SETTLE = int(os.environ.get("SETTLE", "90"))
DECODE_TOKENS = int(os.environ.get("DECODE_TOKENS", "4096"))
CLOCKS = os.environ.get(
    "CLOCKS", "unlocked 1300 1600 1900 2200 2450 2700 2900"
).split()
POWER_LIMIT_W = os.environ.get("POWER_LIMIT_W", "600")

OUT.mkdir(parents=True, exist_ok=True)
sweep_file = (OUT / "sweep.jsonl").open("w")
pwr_file = (OUT / "power.csv").open("w")
pwr_file.write("epoch, power_w, sm_mhz, temp_c\n")


def sudo_nvidia_smi(*args: str) -> None:
    subprocess.run(["sudo", "nvidia-smi", *args], check=True, capture_output=True)


class Sampler(threading.Thread):
    """Sample power.draw / clocks.sm / temperature at 2 Hz."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=power.draw,clocks.sm,temperature.gpu",
                 "--format=csv,noheader"],
                capture_output=True, text=True,
            ).stdout.strip()
            power_w, sm_mhz, temp_c = (part.strip() for part in out.split(","))
            pwr_file.write(f"{time.time():.3f}, {power_w}, {sm_mhz}, {temp_c}\n")
            pwr_file.flush()
            self._stop.wait(0.5)

    def stop(self) -> None:
        self._stop.set()
        self.join()


def emit(row: dict) -> None:
    sweep_file.write(json.dumps(row) + "\n")
    sweep_file.flush()
    print(json.dumps({k: row[k] for k in row if k in
                      ("record", "mhz", "depth", "role", "ttft_seconds",
                       "decode_tps", "decode_tps_median")}), flush=True)


def set_lock(point: str) -> None:
    if point == "unlocked":
        sudo_nvidia_smi("-rgc")
    else:
        sudo_nvidia_smi("-lgc", f"{point},{point}")


def run_prefill(depth: int, role: str) -> None:
    nonce = uuid.uuid4().hex
    prompt = prefill.exact_prompt(BASE, MODEL, depth, nonce)
    t0 = time.time()
    result = ctx.measure(BASE, MODEL, prompt, 256)
    t1 = time.time()
    if result["usage"]["prompt_tokens"] != depth:
        raise RuntimeError(
            f"server counted {result['usage']['prompt_tokens']} prompt "
            f"tokens, expected {depth}"
        )
    emit({"record": "prefill", "quant": QUANT, "depth": depth, "role": role,
          "nonce": nonce, "t_start": t0, "t_end": t1,
          "ttft_seconds": result["ttft_seconds"],
          "decode_seconds": result["decode_seconds"],
          "decode_tokens": result["decode_tokens"],
          "decode_tps": result["decode_tps"]})


def run_decode() -> None:
    out = OUT / "decode.json"
    t0 = time.time()
    subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "benchmark-decode.py"),
         "--base-url", BASE + "/v1", "--model", MODEL,
         "--profile", QUANT, "--mtp-tokens", "3",
         "--concurrency", "1", "--output-tokens", str(DECODE_TOKENS),
         "--runs", "3", "--warmup-runs", "2", "--output", str(out)],
        check=True,
    )
    t1 = time.time()
    report = json.loads(out.read_text())
    point = report["points"][0]
    emit({"record": "decode", "quant": QUANT, "t_start": t0, "t_end": t1,
          "decode_tps_median":
              point["aggregate_decode_tokens_per_second"]["median"],
          "runs": [run["decode_tokens_per_second"] for run in point["runs"]]})


def main() -> None:
    locks = sorted(int(point) for point in CLOCKS if point != "unlocked")
    order: list[str] = []
    if "unlocked" in CLOCKS:
        order.append("unlocked")
    # Interleave from the middle outward to descorrelate thermal drift.
    mid = len(locks) // 2
    order.append(str(locks[mid]))
    for k in range(1, len(locks) // 2 + 1):
        if mid - k >= 0:
            order.append(str(locks[mid - k]))
        if mid + k < len(locks):
            order.append(str(locks[mid + k]))

    sudo_nvidia_smi("-pl", POWER_LIMIT_W)
    sampler = Sampler()
    sampler.start()
    try:
        for point in order:
            emit({"record": "point", "quant": QUANT, "mhz": point,
                  "t_lock": time.time()})
            set_lock(point)
            time.sleep(SETTLE)
            run_prefill(131072, "warmup")
            run_prefill(523264, "run0")
            run_prefill(523264, "run1")
            run_prefill(131072, "run0")
            run_prefill(131072, "run1")
            run_decode()
            time.sleep(30)
    finally:
        sampler.stop()
        sudo_nvidia_smi("-rgc")
        sudo_nvidia_smi("-pl", POWER_LIMIT_W)
    sweep_file.close()
    pwr_file.close()


if __name__ == "__main__":
    main()
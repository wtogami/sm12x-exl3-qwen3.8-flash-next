#!/usr/bin/env python3
"""Analyze clock-sweep receipts: energy per prefill token, Pareto frontier,
decode clock sensitivity.

Joins sweep.jsonl run windows against the 2 Hz power.csv samples to get
mean/peak power and effective SM clock per run, then:

  1. per-point table: effective clock, TTFT (131K/523K), prefill tok/s,
     mean W during 523K prefill, J/1k prefill tokens, decode tok/s + W
  2. frontier joining the sweep points with the power-cap ladder receipts
     (cap points carry the cap as an upper bound on draw)
  3. decode clock-sensitivity check (the H200 paper claims decode is
     insensitive to SM clock above ~1590 MHz)

Usage:
  python3 scripts/analyze-clock-sweep.py [--out-root benchmarks/clock-sweep-20261007]
                                          [--cap-points <spec.json>]
                                          [--output analysis.txt]

Cap-point spec: JSON list of
  {"quant": "exl3", "label": "pl330", "watts": 330,
   "file": "benchmarks/power-tuning-20261007/ttft-matrix.jsonl",
   "config": "pl330-unlocked"}
where "config" filters rows of ttft-matrix-style files and "watts" is
taken from the row when the file has a watts field.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def load_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def window_stats(samples: list[tuple[float, float, float, float]],
                 t0: float, t1: float) -> dict:
    """samples: (epoch, W, mhz, temp). Returns mean/peak over [t0, t1]."""
    sel = [(w, m, t) for (e, w, m, t) in samples if t0 <= e <= t1]
    if not sel:
        return {"n": 0}
    return {
        "n": len(sel),
        "mean_w": statistics.mean(w for w, _, _ in sel),
        "peak_w": max(w for w, _, _ in sel),
        "mean_mhz": statistics.mean(m for _, m, _ in sel),
        "mean_temp_c": statistics.mean(t for _, _, t in sel),
    }


def cap_point_rows(spec: dict, repo: Path) -> list[dict]:
    """Extract (depth, ttft, decode_tps) rows for one cap-point spec entry."""
    path = Path(spec["file"])
    if not path.is_absolute():
        path = repo / path
    rows = load_rows(path)
    out = []
    for row in rows:
        if "ttft_seconds" not in row:
            continue
        if spec.get("config") and row.get("config") != spec["config"]:
            continue
        if spec.get("watts") is not None and "watts" in row:
            if int(row["watts"]) != int(spec["watts"]):
                continue
        out.append(row)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-root", default="benchmarks/clock-sweep-20261007")
    parser.add_argument("--cap-points", default=None,
                        help="JSON spec file (see module docstring)")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    repo = Path(__file__).resolve().parent.parent
    out_root = Path(args.out_root)
    cap_spec = (json.loads(Path(args.cap_points).read_text())
                if args.cap_points else [])

    text: list[str] = []
    machine: dict = {}

    for quant in ("exl3", "nvfp4"):
        qdir = out_root / quant
        if not (qdir / "sweep.jsonl").exists():
            continue
        samples = []
        for line in (qdir / "power.csv").read_text().splitlines()[1:]:
            if not line.strip():
                continue
            e, w, m, t = line.split(",")
            # Sampler keeps nvidia-smi's unit suffixes ("24.81 W", "180 MHz").
            samples.append((float(e), float(w.split()[0]),
                            float(m.split()[0]), float(t.split()[0])))
        sweep = load_rows(qdir / "sweep.jsonl")

        points: dict[str, dict] = {}
        current = None
        for row in sweep:
            if row["record"] == "point":
                current = row["mhz"]
                points[current] = {"requested_mhz": row["mhz"],
                                   "prefill": [], "decode": None}
            elif row["record"] == "prefill":
                pre = window_stats(samples, row["t_start"],
                                   row["t_start"] + row["ttft_seconds"])
                dec = window_stats(samples, row["t_start"] + row["ttft_seconds"],
                                   row["t_end"])
                points[current]["prefill"].append(
                    {"depth": row["depth"], "role": row["role"],
                     "ttft_seconds": row["ttft_seconds"],
                     "decode_tps": row["decode_tps"],
                     "prefill_power": pre, "decode_power": dec})
            elif row["record"] == "decode":
                stats = window_stats(samples, row["t_start"], row["t_end"])
                points[current]["decode"] = {
                    "decode_tps_median": row["decode_tps_median"],
                    "runs": row["runs"], "power": stats}

        text.append(f"=== {quant} ===")
        header = (f"{'point':>10} {'eff MHz':>8} {'131K s':>8} {'523K s':>8} "
                  f"{'pf tok/s':>9} {'mean W':>8} {'J/1k pf':>8} "
                  f"{'dec tps':>8} {'dec W':>7}")
        text.append(header)
        qmachine = {}
        for mhz, point in points.items():
            big = [p for p in point["prefill"] if p["depth"] == 523264
                   and p["role"] != "warmup"]
            small = [p for p in point["prefill"] if p["depth"] == 131072
                     and p["role"] != "warmup"]
            if not big:
                continue
            ttft_big = statistics.median(p["ttft_seconds"] for p in big)
            ttft_small = (statistics.median(p["ttft_seconds"] for p in small)
                          if small else None)
            mean_w = statistics.mean(p["prefill_power"]["mean_w"] for p in big
                                     if p["prefill_power"]["n"])
            eff = statistics.mean(p["prefill_power"]["mean_mhz"] for p in big
                                  if p["prefill_power"]["n"])
            j_per_1k = mean_w * ttft_big / 523.264
            dec = point["decode"]
            dec_tps = dec["decode_tps_median"] if dec else None
            dec_w = dec["power"]["mean_w"] if dec and dec["power"]["n"] else None
            text.append(
                f"{mhz:>10} {eff:8.0f} "
                f"{ttft_small:8.1f} {ttft_big:8.1f} "
                f"{523264 / ttft_big:9.0f} {mean_w:8.1f} {j_per_1k:8.2f} "
                f"{dec_tps:8.1f} {dec_w:7.1f}")
            qmachine[mhz] = {
                "requested_mhz": mhz, "effective_mhz": round(eff, 1),
                "ttft_131k_s": round(ttft_small, 2) if ttft_small else None,
                "ttft_523k_s": round(ttft_big, 2),
                "prefill_tok_s": round(523264 / ttft_big, 1),
                "mean_w_prefill": round(mean_w, 1),
                "j_per_1k_prefill_tok": round(j_per_1k, 3),
                "decode_tps": round(dec_tps, 1) if dec_tps else None,
                "mean_w_decode": round(dec_w, 1) if dec_w else None,
            }
        machine[quant] = {"points": qmachine, "cap_points": {}}

        # Frontier: cap points with the cap as an upper bound on draw.
        caps = [c for c in cap_spec if c["quant"] == quant]
        if caps:
            text.append("")
            text.append("frontier (cap points: cap W is an upper bound on "
                        "draw; J/1k marked with <=)")
            text.append(f"{'config':>18} {'523K s':>8} {'131K s':>8} "
                        f"{'dec tps':>8} {'W':>6} {'J/1k pf':>9}")
            for spec in caps:
                rows = cap_point_rows(spec, repo)
                big = [r for r in rows if r["depth"] == 523264]
                small = [r for r in rows if r["depth"] == 131072]
                if not big:
                    continue
                ttft_big = statistics.median(r["ttft_seconds"] for r in big)
                ttft_small = (statistics.median(r["ttft_seconds"] for r in small)
                              if small else None)
                dec_tps = statistics.median(r["decode_tps"] for r in big)
                watts = float(spec["watts"])
                j = watts * ttft_big / 523.264
                text.append(
                    f"{spec['label']:>18} {ttft_big:8.1f} "
                    f"{(ttft_small if ttft_small else float('nan')):8.1f} "
                    f"{dec_tps:8.1f} {watts:6.0f} {'<=' + format(j, '.2f'):>9}")
                machine[quant]["cap_points"][spec["label"]] = {
                    "watts": watts,
                    "ttft_523k_s": round(ttft_big, 2),
                    "ttft_131k_s": round(ttft_small, 2) if ttft_small else None,
                    "decode_tps": round(dec_tps, 1),
                    "j_per_1k_prefill_tok_upper": round(j, 3),
                }

        # Decode clock sensitivity.
        decs = []
        for m, p in points.items():
            if not (p["decode"] and p["decode"]["decode_tps_median"]):
                continue
            eff = statistics.mean(x["prefill_power"]["mean_mhz"]
                                  for x in p["prefill"]
                                  if x["prefill_power"]["n"])
            decs.append((eff, m, p["decode"]["decode_tps_median"]))
        if len(decs) >= 2:
            text.append("")
            text.append("decode vs effective clock:")
            for eff, m, tps in sorted(decs):
                text.append(f"  {m:>10} -> {tps:7.1f} tok/s (eff {eff:.0f} MHz)")

    output = "\n".join(text) + "\n"
    if args.output:
        Path(args.output).write_text(output)
        (Path(args.output).with_suffix(".json")).write_text(
            json.dumps(machine, indent=2) + "\n")
        print(f"wrote {args.output} and {Path(args.output).with_suffix('.json')}")
    else:
        print(output)
        print(json.dumps(machine, indent=2))


if __name__ == "__main__":
    main()
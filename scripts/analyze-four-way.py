#!/usr/bin/env python3
"""Protocol-clean core suites + 10-run tool-eval statistics for four quants.

Core receipts: benchmarks/core-suite-20261007/{exl3,rha,nvfp4} (ple8 from
benchmarks/ple8-quality-20261007/core, same protocol). Tool receipts:
benchmarks/tool-eval-repeats-20261007/{exl3,rha,nvfp4,ple8}/run-01..10.
Welch t + Holm over all six pairs. Run with the lm-eval venv python."""
import json
import os

import numpy as np
from scipy import stats

B = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "benchmarks")
QUANTS = ["exl3", "rha", "nvfp4", "ple8"]
LABELS = {"exl3": "EXL3", "rha": "RedHatAI", "nvfp4": "nvidia", "ple8": "exl3-ple8"}
# Hard Mode scenario ids (category P) of the pinned tool-eval-bench
# checkout cf54b4bfe705f12f71e8866f10730572497c8105.
HARD_IDS = {f"TC-{i}" for i in range(70, 89)}


def _meas(path):
    for line in open(path):
        r = json.loads(line)
        if r.get("record") == "measurement":
            yield r


def core(q):
    d = f"{B}/ple8-quality-20261007/core" if q == "ple8" else f"{B}/core-suite-20261007/{q}"
    seven = [(r["case"], r["contract"]["quality_contract_passed"]) for r in _meas(f"{d}/seven.jsonl")]
    orch = [r["contract"]["occurrences"] for r in _meas(f"{d}/orchid.jsonl")]
    api = [r["passed"] for r in _meas(f"{d}/api-tools.jsonl")]
    v = json.load(open(f"{d}/vision.json"))
    vis = all(r["passed"] for r in v["supported_image_counts"].values())
    ret = [r["passed"] for r in _meas(f"{d}/retrieval.jsonl")]
    cd = json.load(open(f"{d}/clients.json"))
    assert cd["model"] == {"exl3": "qwen38-exl3", "rha": "qwen38-nvfp4-redhatai",
                           "nvfp4": "qwen38-nvfp4", "ple8": "qwen38-exl3-ple8"}[q]
    dec = [round(p["aggregate_decode_tokens_per_second"]["median"], 1) for p in cd["points"]]
    return seven, orch, api, vis, ret, dec


print("=== CORE SUITE (protocol: seven x21, orchid x5, api-tools, vision, retrieval 6, decode C1-C16) ===")
for q in QUANTS:
    seven, orch, api, vis, ret, dec = core(q)
    ok, tot = sum(p for _, p in seven), len(seven)
    fails = sorted({c for c, p in seven if not p})
    print(f"{LABELS[q]:10s} seven {ok}/{tot} (fails: {fails})  orchid {sum(o == 100 for o in orch)}/5 occ={orch}  "
          f"api {sum(api)}/{len(api)}  vision {vis}  retrieval {sum(ret)}/{len(ret)}  decode {dec}")

print()
print("=== TOOL-EVAL-BENCH (10 runs each, 88 cases, hardmode, 176 pts) ===")
data = {}
for q in QUANTS:
    totals, hards = [], []
    for i in range(1, 11):
        d = json.load(open(f"{B}/tool-eval-repeats-20261007/{q}/run-{i:02d}/tools.json"))
        totals.append(d["scores"]["total_points"])
        hards.append(sum(s["points"] for s in d["scores"]["scenario_results"] if s["scenario_id"] in HARD_IDS))
    data[q] = (np.array(totals), np.array(hards))
    t, h = data[q]
    tc = stats.t.interval(0.95, len(t) - 1, loc=t.mean(), scale=stats.sem(t))
    hc = stats.t.interval(0.95, len(h) - 1, loc=h.mean(), scale=stats.sem(h))
    print(f"{LABELS[q]:10s} total {list(map(int, t))}  mean {t.mean():.1f} sd {t.std(ddof=1):.2f}  95%CI [{tc[0]:.1f}, {tc[1]:.1f}]")
    print(f"{'':10s} hard  {list(map(int, h))}  mean {h.mean():.1f}  95%CI [{hc[0]:.1f}, {hc[1]:.1f}]")

print()
print("=== PAIRWISE (Welch t, Holm-corrected, 6 pairs) ===")
pairs = [(a, b) for i, a in enumerate(QUANTS) for b in QUANTS[i + 1:]]
rows = []
for a, b in pairs:
    ta, tb = data[a][0], data[b][0]
    tstat, p = stats.ttest_ind(ta, tb, equal_var=False)
    rows.append((p, a, b, ta.mean() - tb.mean()))
rows.sort(key=lambda r: r[0])
m = len(rows)
for rank, (p, a, b, d) in enumerate(rows, 1):
    ph = min(p * (m - rank + 1), 1.0)
    sig = "SIG" if ph < 0.05 else "tie"
    print(f"{LABELS[a]:10s} vs {LABELS[b]:10s}  diff {d:+.1f} pts  Welch p={p:.4f}  Holm p={ph:.4f}  {sig}")

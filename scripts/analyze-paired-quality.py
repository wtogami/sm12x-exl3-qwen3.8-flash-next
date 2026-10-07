#!/usr/bin/env python3
"""Paired McNemar + paired-difference CIs on per-item GSM8K/IFEval data.

Compares quant pairs on identical items (item difficulty cancels), and,
when the EXL3 r2 rerun dirs exist, measures engine jitter from the
run1-vs-run2 flip counts on the same quant. Run with the lm-eval venv
python (scipy)."""
import glob
import json
import math
import os
from statistics import mean

from scipy import stats

QD = os.environ.get("QD", "/tmp/opencode/quality-deep")
QUANTS = ["exl3", "nvfp4", "rha"]
LABELS = {"exl3": "EXL3", "nvfp4": "nvidia NVFP4", "rha": "RedHatAI NVFP4"}
PAIRS = [("nvfp4", "exl3"), ("nvfp4", "rha"), ("exl3", "rha")]
IFEVAL_METRICS = ["prompt_level_strict_acc", "inst_level_strict_acc",
                  "prompt_level_loose_acc", "inst_level_loose_acc"]


def load_gsm8k(dirname):
    f = glob.glob(f"{QD}/{dirname}/qwen38-*/samples_gsm8k_*.jsonl")[0]
    per = {}
    for line in open(f):
        d = json.loads(line)
        per[(d["doc_id"], d["filter"])] = bool(d["exact_match"])
    return per


def load_ifeval(dirname):
    """prompt_level_* are booleans; inst_level_* are per-instruction lists
    (bool() on a non-empty list is always True -> must flatten instead)."""
    f = glob.glob(f"{QD}/{dirname}/qwen38-*/samples_ifeval_*.jsonl")[0]
    per = {}
    for line in open(f):
        d = json.loads(line)
        for m in ("prompt_level_strict_acc", "prompt_level_loose_acc"):
            per[(d["doc_id"], m)] = bool(d[m])
        for m in ("inst_level_strict_acc", "inst_level_loose_acc"):
            for i, v in enumerate(d[m]):
                per[(d["doc_id"], m, i)] = bool(v)
    return per


def compare(a, b, keys):
    """Paired compare dicts a and b on shared keys; returns (awins, bwins,
    diff_pts, mcnemar_p_exact, ci95_pts)."""
    n = len(keys)
    aw = sum(1 for k in keys if a[k] and not b[k])
    bw = sum(1 for k in keys if b[k] and not a[k])
    diff = (aw - bw) / n
    p = stats.binomtest(aw, aw + bw, 0.5).pvalue if aw + bw else 1.0
    var = (aw + bw) / n - diff**2
    se = math.sqrt(var / n)
    return aw, bw, diff * 100, p, stats.t.ppf(0.975, n - 1) * se * 100


def subkeys(per, part):
    return [k for k in per if part in k[1]]


def report(task, loaders, parts):
    print(f"\n================ {task} (paired McNemar, exact) ================")
    data = {q: loaders(f"{q}-{task.lower()}") for q in QUANTS}
    for q in QUANTS:
        keys = list(data[q])
        print(f"{LABELS[q]:15s} pass-rate recomputed: " +
              "  ".join(f"{part}={mean(data[q][k] for k in subkeys(data[q], part))*100:.2f}"
                        for part in parts))
    for a, b in PAIRS:
        shared = set(data[a]) & set(data[b])
        for part in parts:
            keys = [k for k in shared if k[1] == part or part in k[1]]
            aw, bw, d, p, ci = compare(data[a], data[b], keys)
            star = "SIG" if p < 0.05 else "tie"
            print(f"{part:22s} {LABELS[a]:15s} vs {LABELS[b]:15s}: "
                  f"diff={d:+5.2f} pts (CI +/-{ci:.2f}) discordant {aw}/{bw} "
                  f"p={p:.4f} [{star}]")


def jitter(task, loaders, parts, n_runs_desc="run1 vs run2, EXL3, same engine"):
    print(f"\n---- jitter ({task}): {n_runs_desc} ----")
    r1 = loaders("exl3-" + task.lower())
    r2 = loaders("exl3-" + task.lower() + "-r2")
    n_items = len(r1) // len(parts)
    print("r1 rates: " + "  ".join(
        f"{part}={mean(r1[k] for k in r1 if part in k[1] or k[1] == part)*100:.2f}"
        for part in parts))
    print("r2 rates: " + "  ".join(
        f"{part}={mean(r2[k] for k in r2 if part in k[1] or k[1] == part)*100:.2f}"
        for part in parts))
    for part in parts:
        keys = [k for k in r1 if k in r2 and (part in k[1] or k[1] == part)]
        aw, bw, d, p, ci = compare(r2, r1, keys)  # d = rate(r2) - rate(r1)
        flips = aw + bw
        # per-run score SD from discordance: Var(diff)=2*Var(score) -> sd_pts
        sd = math.sqrt(flips / 2) / len(keys) * 100
        print(f"{part:22s} flips={flips:4d}/{len(keys)} ({flips/len(keys)*100:.1f}%) "
              f"net(r2-r1)={d:+.2f} pts (CI +/-{ci:.2f}, McNemar p={p:.3f})  "
              f"=> per-run sd ~{sd:.2f} pts")


def main():
    report("GSM8K", load_gsm8k, ["flexible-extract", "strict-match"])
    report("IFEval", load_ifeval, IFEVAL_METRICS)
    if os.path.isdir(f"{QD}/exl3-gsm8k-r2") and glob.glob(f"{QD}/exl3-gsm8k-r2/qwen38-*/samples_*.jsonl"):
        jitter("GSM8K", load_gsm8k, ["flexible-extract", "strict-match"])
    if os.path.isdir(f"{QD}/exl3-ifeval-r2") and glob.glob(f"{QD}/exl3-ifeval-r2/qwen38-*/samples_*.jsonl"):
        jitter("IFEval", load_ifeval, IFEVAL_METRICS)
    else:
        print("\n---- jitter: r2 rerun samples not yet present ----")


if __name__ == "__main__":
    main()
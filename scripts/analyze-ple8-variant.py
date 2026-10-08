#!/usr/bin/env python3
"""Paired McNemar for the exl3-ple8 FP8-PLE variant vs the three quants.

Same methodology as analyze-paired-quality.py: exact two-sided binomial on
discordant pairs, identical items. The earlier quants' per-item files live
outside the repo (lm_eval sample dirs); override with QD=... Run with the
lm-eval venv python (scipy)."""
import glob
import json
import os

from scipy import stats

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLE8 = os.path.join(REPO, "benchmarks", "ple8-quality-20261007", "samples")
QD = os.environ.get("QD", "/tmp/opencode/quality-deep")


def load_gsm8k(f):
    per = {}
    for line in open(f):
        d = json.loads(line)
        if d["filter"] == "flexible-extract":
            per[d["doc_id"]] = bool(d["exact_match"])
    return per


def load_old_gsm8k(dirname):
    f = glob.glob(f"{QD}/{dirname}/qwen38-*/samples_gsm8k_*.jsonl")[0]
    return load_gsm8k(f)


def load_ifeval(f, metric):
    per = {}
    for line in open(f):
        d = json.loads(line)
        v = d[metric]
        if isinstance(v, list):  # inst_level_*_acc: per-instruction list
            for idx, b in enumerate(v):
                per[(d["doc_id"], idx)] = bool(b)
        else:
            per[d["doc_id"]] = bool(v)
    return per


def load_old_ifeval(dirname, metric):
    f = glob.glob(f"{QD}/{dirname}/qwen38-*/samples_ifeval_*.jsonl")[0]
    return load_ifeval(f, metric)


def mcnemar(a, b):
    """a, b: dict key -> bool. Returns (n01, n10, p, n_pairs)."""
    common = set(a) & set(b)
    n01 = sum(1 for k in common if a[k] and not b[k])
    n10 = sum(1 for k in common if b[k] and not a[k])
    n = n01 + n10
    if n == 0:
        return n01, n10, 1.0, len(common)
    p = stats.binomtest(min(n01, n10), n, 0.5, alternative="two-sided").pvalue
    return n01, n10, p, len(common)


def show(label, n01, n10, p, n):
    verdict = "tie" if p >= 0.05 else ("A>B" if n10 > n01 else "B>A")
    print(f"  {label:<42} {n01:3d}/{n10:3d} discordant (n={n})  p={p:.4f}  {verdict}")


gsm = {
    "ple8-r1": load_gsm8k(f"{PLE8}/gsm8k-r1-samples.jsonl"),
    "ple8-r2": load_gsm8k(f"{PLE8}/gsm8k-r2-samples.jsonl"),
    "exl3-r1": load_old_gsm8k("exl3-gsm8k"),
    "exl3-r2": load_old_gsm8k("exl3-gsm8k-r2"),
    "nvfp4": load_old_gsm8k("nvfp4-gsm8k"),
    "rha": load_old_gsm8k("rha-gsm8k"),
}
print("=== GSM8K (flexible-extract, 1319 items) ===")
print("scores:", {k: round(sum(v.values()) / len(v), 4) for k, v in gsm.items()})
print("ple8 mean of 2 runs:", round((sum(gsm["ple8-r1"].values()) + sum(gsm["ple8-r2"].values())) / 2 / 1319, 4))
print("EXL3 vs ple8 (direct PLE-format A/B, constant trellis experts):")
show("ple8-r1 vs exl3-r1", *mcnemar(gsm["ple8-r1"], gsm["exl3-r1"]))
show("ple8-r2 vs exl3-r2", *mcnemar(gsm["ple8-r2"], gsm["exl3-r2"]))
show("ple8-r1 vs exl3-r2 (cross)", *mcnemar(gsm["ple8-r1"], gsm["exl3-r2"]))
show("ple8-r2 vs exl3-r1 (cross)", *mcnemar(gsm["ple8-r2"], gsm["exl3-r1"]))
print("ple8 vs nvidia (FP8 PLE + NVFP4 experts):")
show("ple8-r1 vs nvfp4", *mcnemar(gsm["ple8-r1"], gsm["nvfp4"]))
show("ple8-r2 vs nvfp4", *mcnemar(gsm["ple8-r2"], gsm["nvfp4"]))
print("ple8 vs RHA (FP8 PLE vs BF16 PLE, NVFP4 experts):")
show("ple8-r1 vs rha", *mcnemar(gsm["ple8-r1"], gsm["rha"]))
show("ple8-r2 vs rha", *mcnemar(gsm["ple8-r2"], gsm["rha"]))
print("reference (recomputed from the three-way legs):")
show("nvfp4 vs exl3-r1", *mcnemar(gsm["nvfp4"], gsm["exl3-r1"]))
show("nvfp4 vs rha", *mcnemar(gsm["nvfp4"], gsm["rha"]))
show("exl3-r1 vs rha", *mcnemar(gsm["exl3-r1"], gsm["rha"]))

print()
print("=== IFEval (541 prompts / 834 instructions) ===")
metrics = ["prompt_level_strict_acc", "inst_level_strict_acc",
           "prompt_level_loose_acc", "inst_level_loose_acc"]
for m in metrics:
    d = {
        "ple8-r1": load_ifeval(f"{PLE8}/ifeval-r1-samples.jsonl", m),
        "ple8-r2": load_ifeval(f"{PLE8}/ifeval-r2-samples.jsonl", m),
        "exl3-r1": load_old_ifeval("exl3-ifeval", m),
        "exl3-r2": load_old_ifeval("exl3-ifeval-r2", m),
        "nvfp4": load_old_ifeval("nvfp4-ifeval", m),
        "rha": load_old_ifeval("rha-ifeval", m),
    }
    sc = {k: round(sum(v.values()) / len(v), 4) for k, v in d.items()}
    print(f"{m}: {sc}")
    print(f"  ple8 mean: {round((sc['ple8-r1'] + sc['ple8-r2']) / 2, 4)}")
    show("ple8-r1 vs exl3-r1", *mcnemar(d["ple8-r1"], d["exl3-r1"]))
    show("ple8-r2 vs exl3-r2", *mcnemar(d["ple8-r2"], d["exl3-r2"]))
    show("ple8-r1 vs nvfp4", *mcnemar(d["ple8-r1"], d["nvfp4"]))
    show("ple8-r2 vs nvfp4", *mcnemar(d["ple8-r2"], d["nvfp4"]))
    show("ple8-r1 vs rha", *mcnemar(d["ple8-r1"], d["rha"]))
    show("ple8-r2 vs rha", *mcnemar(d["ple8-r2"], d["rha"]))
print()
print("Finding: exl3-ple8 (FP8 PLE table, identical quantized experts) matches")
print("EXL3 on GSM8K, so the FP8 table format costs no reasoning quality;")
print("nvidia's deficit belongs to its ModelOpt NVFP4 expert pipeline.")

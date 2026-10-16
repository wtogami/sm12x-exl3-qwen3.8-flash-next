#!/usr/bin/env python3
"""Leakage check: eval targets vs actual calibration corpora, token-level.
For every target item, report the largest n in {64,48,32,24,16} such that any
n-gram of its tokenization appears in the calibration n-gram universe.
Run in-image (needs transformers): tokenizer from the BF16 source snapshot."""
import argparse, json
from transformers import AutoTokenizer
ap = argparse.ArgumentParser()
ap.add_argument("--cal-panel", required=True)
ap.add_argument("--cal-vendor-dir", required=True)
ap.add_argument("--tokenizer", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()
tok = AutoTokenizer.from_pretrained(a.tokenizer)

cal_texts = []
for line in open(a.cal_panel):
    d = json.loads(line)
    if isinstance(d.get("prompt"), str): cal_texts.append(d["prompt"])
    for m in (d.get("messages") or []): cal_texts.append(m.get("content") or "")
for fn in ("wiki.utf8","tiny.utf8","c4.utf8","code.utf8","multilingual.utf8","technical.utf8"):
    sep = None
    p = f"{a.cal_vendor_dir}/{fn}"
    try:
        txt = open(p, encoding="utf8").read()
        cal_texts += txt.split("</doc>\n") if sep == "</doc>\n" else ([txt] if sep is None and fn in ("code.utf8","technical.utf8") else txt.split("\n"))
    except FileNotFoundError: print("WARN missing", p)
print("cal text units:", len(cal_texts))

NS = (64, 48, 32, 24, 16)
gramsets = {n: set() for n in NS}
for t in cal_texts:
    ids = tok(t, add_special_tokens=False)["input_ids"]
    for n in NS:
        for i in range(len(ids) - n + 1): gramsets[n].add(tuple(ids[i:i+n]))
print("cal universe tokens per n:", {n: len(gramsets[n]) for n in NS})

def worst(text):
    ids = tok(text, add_special_tokens=False)["input_ids"]
    for n in NS:
        gs = gramsets[n]
        for i in range(len(ids) - n + 1):
            if tuple(ids[i:i+n]) in gs: return n
    return 0

out = []
pqa = [json.loads(l) for l in open("/hf/evals/pubmedqa/pqa_labeled.jsonl")]
for r in pqa:
    txt = r["question"] + "\n" + " ".join(r["context"]["contexts"])
    out.append({"src": "pubmedqa", "id": r["pubid"], "overlap": worst(txt)})
ife = [json.loads(l) for l in open("/hf/evals/ifeval/ifeval.jsonl")]
for r in ife:
    out.append({"src": "ifeval", "id": r["key"], "overlap": worst(r["prompt"])})

bcb = []
try:
    bcb = [json.loads(s) for s in open("/hf/evals/bigcodebench/hard.jsonl")]
except FileNotFoundError:
    print("WARN no bcb file")
for r in bcb:
    out.append({"src": "bcb-hard", "id": r["task_id"], "overlap": worst(r["instruct_prompt"] + "\n" + r.get("question", ""))})

with open(a.out, "w") as f:
    json.dump(out, f)
from collections import Counter
for src in ("pubmedqa", "ifeval", "bcb-hard"):
    c = Counter(x["overlap"] for x in out if x["src"] == src)
    print(src, "overlap distribution:", dict(c))
    flagged = [x for x in out if x["src"] == src and x["overlap"] >= 16]
    print(f"  flagged (>=16): {len(flagged)}", [f['id'] for f in flagged[:12]])

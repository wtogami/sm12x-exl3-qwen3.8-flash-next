#!/usr/bin/env python3
"""PubMedQA PQA-L gate: published expert-labeled biomedical research-question
answering (yes/no/maybe). Deterministic: greedy decoding, one-word verifiable
answer, exact-match accuracy + macro-F1 over all 1000 labeled items.
Superset of the official 500-item test split; sorted by pubid for reproducibility.
Run inside the image with urllib only; receipts JSONL per row."""
import argparse, json, re, time, urllib.request
ap = argparse.ArgumentParser()
ap.add_argument("--base-url", default="http://127.0.0.1:8001")
ap.add_argument("--model", required=True)
ap.add_argument("--output", required=True)
ap.add_argument("--limit", type=int, default=0)
a = ap.parse_args()
rows = [json.loads(l) for l in open("/hf/evals/pubmedqa/pqa_labeled.jsonl")]
rows.sort(key=lambda r: r["pubid"])
if a.limit: rows = rows[:a.limit]
SYS = ("You are a biomedical research expert. Read the question and the abstract "
       "context (Purpose/Methods/Results). Then answer the question with EXACTLY one "
       "word: yes, no, or maybe. The last line of your reply must be only that word.")
word_re = re.compile(r"\b(yes|no|maybe)\b", re.I)
def ask(row):
    ctx = " ".join(row["context"]["contexts"])[:9000]
    prompt = f"Question: {row['question']}\n\nContext: {ctx}"
    body = json.dumps({"model": a.model, "temperature": 0, "max_tokens": 1024,
                       "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": prompt}]}).encode()
    t0 = time.time()
    req = urllib.request.Request(a.base_url + "/v1/chat/completions", body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read())
    ch = d["choices"][0]
    vis = ch["message"].get("content") or ""
    fr = ch.get("finish_reason")
    pred = None
    for m in word_re.finditer(vis): pred = m.group(1).lower()   # last mention wins
    return {"pubid": row["pubid"], "gold": row["final_decision"], "pred": pred,
            "pass": pred == row["final_decision"], "finish": fr, "seconds": round(time.time()-t0, 2),
            "vis_len": len(vis), "record": "measurement"}
out, acc, preds = [], 0, []
f = open(a.output, "w")
for row in rows:
    r = ask(row); out.append(r); preds.append((r["pred"], r["gold"]))
    f.write(json.dumps(r) + "\n"); f.flush()
n = len(out); acc = sum(1 for r in out if r["pass"])
labels = ["yes", "no", "maybe"]
f1s = []
for lab in labels:
    tp = sum(1 for p, g in preds if p == lab and g == lab)
    fp = sum(1 for p, g in preds if p == lab and g != lab)
    fn = sum(1 for p, g in preds if p != lab and g == lab)
    prec = tp/(tp+fp) if tp+fp else 0.0; rec = tp/(tp+fn) if tp+fn else 0.0
    f1s.append(2*prec*rec/(prec+rec) if prec+rec else 0.0)
summ = {"record": "summary", "accuracy": round(acc/n, 4), "passed": acc, "total": n,
        "macro_f1": round(sum(f1s)/3, 4), "per_class_f1": dict(zip(labels, [round(x,3) for x in f1s])),
        "truncated": sum(1 for r in out if r["finish"] == "length"),
        "missing_answer": sum(1 for r in out if r["pred"] is None)}
f.write(json.dumps(summ) + "\n")
print(f"PUBMEDQA acc={acc}/{n} macro-F1={summ['macro_f1']} trunc={summ['truncated']} missing={summ['missing_answer']}")

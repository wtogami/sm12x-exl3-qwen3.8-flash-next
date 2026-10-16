#!/usr/bin/env python3
"""Assemble the tuned-vs-uniform decision matrix from committed receipts.
Reads: benchmarks/{req3,req4,req6,req6b,req5,base}-quality-v2/*.jsonl, benchmarks/fidelity/*.jsonl.
Prints a per-card table: behavioral gates + fidelity SQNR; flags missing lanes."""
import json, pathlib, statistics

CARDS = [  # label, receipts dir, fidelity file (None = use card-specific)
    ("K3",  "benchmarks/req3-quality-v2", "benchmarks/fidelity/req3-rmse.jsonl"),
    ("K4",  "benchmarks/req4-quality-v2", "benchmarks/fidelity/req4-rmse.jsonl"),
    ("K5",  "benchmarks/req5-quality-v2", "benchmarks/fidelity/k5-rmse.jsonl"),
    ("K6",  "benchmarks/req6b-quality-v2", "benchmarks/fidelity/req6b-rmse.jsonl"),
    ("K6V", "benchmarks/req6-quality-v2", "benchmarks/fidelity/req6-rmse.jsonl"),
    ("base","benchmarks/base-quality-v2", None),
]
def load(d, p):
    f = pathlib.Path(d)/p
    if not f.exists(): return None
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
def frac(rows, keyf):
    if rows is None: return None
    ms=[r for r in rows if r.get('record')=='measurement']
    if not ms: return None
    return sum(1 for r in ms if keyf(r)), len(ms)
def summary(d, p, k):
    rows = load(d, p)
    if not rows: return None
    sm=[r for r in rows if r.get('record')=='summary']
    return sm[-1].get(k) if sm else None
def fmt(x, mul=1, nd=1):
    return f"{x*mul:.{nd}f}" if isinstance(x,(int,float)) else "  -- "
for label, d, fid in CARDS:
    dpath = pathlib.Path(d)
    if not dpath.exists():
        print(f"{label:5} MISSING ({d})"); continue
    api = frac(load(d,'api-tools.jsonl'), lambda r: r.get('passed'))
    sev = frac(load(d,'seven.jsonl'), lambda r: (r.get('contract') or {}).get('quality_contract_passed'))
    orc = frac(load(d,'orchid.jsonl'), lambda r: (r.get('contract') or {}).get('pass'))
    ret = frac(load(d,'retrieval.jsonl'), lambda r: r.get('passed'))
    bm = load(d,'biomed.jsonl'); bmn="--"
    if bm:
        ids={}
        for r in bm:
            if isinstance(r,dict) and 'id' in r and 'pass' in r: ids.setdefault(r['id'],[]).append(bool(r['pass']))
        if ids: bmn=f"{sum(1 for v in ids.values() if sum(v)*2>=len(v))}/{len(ids)}"
    pqa = summary(d,'pubmedqa.jsonl','accuracy'); ife = summary(d,'ifeval.jsonl','prompt_strict')
    bcb = summary(d,'bcb.jsonl','pass_at_1')
    b = load(d,'orchid.jsonl')
    oc=""
    if b:
        occ=[sum(1 for w in (r.get('content') or '').split() if 'orchid' in w.lower()) for r in b if r.get('record')=='measurement']
        if occ: oc=f"μ{statistics.mean(occ):.2f}"
    sq = "--"
    if fid and pathlib.Path(fid).exists():
        s=[json.loads(l) for l in open(fid) if l.strip()][-1]
        sq=f"{s.get('gmean_sqnr_db',0):.2f}dB"
    elif label=="K5" and pathlib.Path("/home/opencode/.cache/huggingface/gg-fid-received/k5-rmse.jsonl").exists():
        pass  # gg-side sweep; manual row
    def f2(x): return f"{x*100:.1f}" if isinstance(x,(int,float)) else "--"
    print(f"{label:5} seven {fmt(sev[0],1) if sev else '--':>2}/{sev[1] if sev else '--':<2} orch {orc[0] if orc else '--'}/20 {oc:>7} api {api[0] if api else '--'}/16 retr {ret[0] if ret else '--'}/6 biomed {bmn:>5} | PQA {f2(pqa):>5} IFE {f2(ife):>5} BCB {f2(bcb):>5} | SQNR {sq}")

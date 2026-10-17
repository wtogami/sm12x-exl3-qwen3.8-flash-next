#!/usr/bin/env python3
"""Extended quality table: ladder + baseline + tuned lanes + x20 defect probes."""
import json, pathlib, statistics, collections

ROWS = [
    ("K3",        "benchmarks/req3-quality-v2",  "benchmarks/fidelity/req3-rmse.jsonl",  None),
    ("K4",        "benchmarks/req4-quality-v2",  "benchmarks/fidelity/req4-rmse.jsonl",  None),
    ("K5",        "benchmarks/req5-quality-v2",  "benchmarks/fidelity/k5-rmse.jsonl",    None),
    ("K6",        "benchmarks/req6b-quality-v2", "benchmarks/fidelity/req6b-rmse.jsonl", None),  # K6 lane used suite; probe shown if exists
    ("K6V",       "benchmarks/req6-quality-v2",  "benchmarks/fidelity/req6-rmse.jsonl",  "benchmarks/k6v-probe20-probe-biomed"),
    ("4.25bpw",   "benchmarks/base-quality-v2",  None,                                   "benchmarks/k425-probe20-probe-biomed"),
    ("tuned-pan", "benchmarks/tuned565-panel-quality-v2",   None, "benchmarks/tuned565-panel-quality-v2-probe-biomed"),
    ("tuned-van", "benchmarks/tuned565-vanilla-quality-v2", None, "benchmarks/tuned565-vanilla-quality-v2-probe-biomed"),
]
def load(d, p):
    f = pathlib.Path(d)/p
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()] if f.exists() else None
def frac(rows, keyf):
    if not rows: return None
    ms=[r for r in rows if r.get('record')=='measurement']
    return (sum(1 for r in ms if keyf(r)), len(ms)) if ms else None
def summ(d,p,k):
    rows=load(d,p)
    if not rows: return None
    sm=[r for r in rows if r.get('record')=='summary']
    return sm[-1].get(k) if sm else None
def biomed(rows):
    if not rows: return None
    ids={}
    for r in rows:
        if 'id' in r and 'pass' in r: ids.setdefault(r['id'],[]).append(bool(r['pass']))
    ok=sum(1 for v in ids.values() if sum(v)*2>=len(v))
    tr=sum(1 for r in rows if r.get('truncated'))
    return f"{ok}/{len(ids)}", tr, [t for t,v in ids.items() if sum(v)==0]
def sq(fid):
    if fid and pathlib.Path(fid).exists():
        s=[json.loads(l) for l in open(fid) if l.strip()][-1]
        return f"{s.get('gmean_sqnr_db',0):.1f}"
    return "--"
print(f"{'card':9} {'PQA':>5} {'IFE':>5} {'BCB':>5} | {'bm20':>5} {'trunc':>5} {'dead-tasks':<14} | seven orch(μ)  SQNR")
for label,d,fid,probe in ROWS:
    pqa=summ(d,'pubmedqa.jsonl','accuracy'); ife=summ(d,'ifeval.jsonl','prompt_strict'); bcb=summ(d,'bcb.jsonl','pass_at_1')
    bm=biomed(load(probe,'biomed20.jsonl')) if probe else None
    b20=f"{bm[0]:>5} {bm[1]:>4}  {','.join(bm[2])[:14]:<14}" if bm else f"{'--':>5} {'--':>5}  {'':<14}"
    sev=frac(load(d,'seven.jsonl'), lambda r:(r.get('contract') or {}).get('quality_contract_passed'))
    orc=frac(load(d,'orchid.jsonl'), lambda r:(r.get('contract') or {}).get('pass'))
    occ=[sum(1 for w in (r.get('content') or '').split() if 'orchid' in w.lower()) for r in (load(d,'orchid.jsonl') or []) if r.get('record')=='measurement']
    f2=lambda x: f"{x*100:.1f}" if isinstance(x,(int,float)) else " -- "
    print(f"{label:9} {f2(pqa):>5} {f2(ife):>5} {f2(bcb):>5} | {b20} | {sev[0] if sev else '--':>3}/{sev[1] if sev else '--':<2} {orc[0] if orc else '--'}/20 μ{statistics.mean(occ):.2f} {sq(fid):>4}")

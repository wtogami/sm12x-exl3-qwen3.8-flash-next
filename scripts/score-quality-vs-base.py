#!/usr/bin/env python3
"""Compare req6-quality ladder receipts against tp2-ep-base-quality baseline.
Pass criteria mirror scripts/summarize-quality.py: api-tools `passed` (16),
seven `contract.quality_contract_passed` (21), orchid `contract.pass` (5),
retrieval `passed` (6)."""
import json, pathlib

def rows(p):
    return [r for r in (json.loads(l) for l in open(p) if l.strip())
            if r.get('record') == 'measurement' and r.get('timed', True)]

def ok(kind, r):
    if kind in ('api', 'retr'):
        return r['passed']
    return r['contract']['quality_contract_passed' if kind == 'seven' else 'pass']

SPEC = [('api-tools.jsonl', 'api', 16), ('seven.jsonl', 'seven', 21),
        ('orchid.jsonl', 'orchid', 5), ('retrieval.jsonl', 'retr', 6)]
A = pathlib.Path('benchmarks/req6-quality'); B = pathlib.Path('benchmarks/tp2-ep-base-quality')
for fn, kind, _ in SPEC:
    ra, rb = rows(A / fn), rows(B / fn)
    pa, pb = sum(ok(kind, r) for r in ra), sum(ok(kind, r) for r in rb)
    fa = [r for r in ra if not ok(kind, r)]
    print(f"{fn:18} req6 {pa}/{len(ra)}  base {pb}/{len(rb)}  delta {pa-pb:+d}", end='')
    if fa:
        ids = [r.get('name') or r.get('id') or r.get('mode') or r.get('suite') or str(list(r)[:3]) for r in fa]
        print(f"  req6-fails: {ids}", end='')
    print()
    if len(ra) != len(rb):
        print(f"  !! row-count mismatch req6={len(ra)} base={len(rb)}")

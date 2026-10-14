#!/usr/bin/env python3
"""req6 repair (d): the 1.6.1 converter records every module - including
BF16 passthroughs - in tensor_storage. The fork's standard-fused-MoE
scanner requires ALL ledger keys to be expert tensors (validator skips
non-exl3 entries; the base card's ledger is experts-only), and silently
declines the fast path otherwise, which then trips the enforce-eager
guard at the vision tower. Drop non-exl3 records. Idempotent."""
import json, sys
p = sys.argv[1]
q = json.load(open(p)); ts = q["tensor_storage"]
keep = {k: e for k, e in ts.items() if isinstance(e, dict) and e.get("quant_format") == "exl3"}
if len(keep) == len(ts):
    print("already filtered"); sys.exit(0)
q["tensor_storage"] = keep
json.dump(q, open(p, "w"))
print(f"ledger {len(ts)} -> {len(keep)}")

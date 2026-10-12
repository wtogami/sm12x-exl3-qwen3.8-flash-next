import json, os, statistics as st, sys
RUN = sys.argv[1]

def load(tag, kind):
    f = os.path.join(RUN, f"{tag}-{kind}.json")
    if not os.path.exists(f):
        return None
    try:
        return json.load(open(f))
    except Exception as e:
        print(f"  !! {os.path.basename(f)} unreadable: {e}")
        return None

def series(j, field):
    if not j:
        return {}
    out = {}
    for p in j.get("points", []):
        key = p.get("concurrency")
        if key is None:
            key = p.get("prompt_tokens")
        vals = [r[field] for r in p.get("runs", []) if isinstance(r, dict) and r.get(field)]
        if not vals and p.get(field):
            vals = [p[field]]                       # some receipts summarise on the point
        if vals:
            out[key] = st.median(vals)
    return out

def table(title, keys, field, unit, is_time=False):
    A1 = series(load("A1", keys[0]), field); A2 = series(load("A2", keys[0]), field)
    B1 = series(load("B1", keys[1]), field); B2 = series(load("B2", keys[1]), field)
    got = sum(1 for d in (A1, A2, B1, B2) if d)
    print(f"\n  {title}  ({unit})")
    if got < 4:
        print(f"  !! only {got}/4 arms produced data for field '{field}' -- "
              "check the field name against the receipt schema")
        if got == 0:
            return
    # Explicit, not substring-sniffed: "second" appears inside
    # effective_prompt_tokens_per_second, which made the prefill table take
    # TTFT's decimal format and run its columns together.
    dec = 2 if is_time else 0
    w = 9 if dec == 0 else 8
    cell = "{:>" + str(w) + "." + str(dec) + "f}"      # numeric cells
    print("    " + "{:>10}".format("point") + "".join("{:>{w}.{d}}".format(h, w=w, d=dec)
          for h in ("A1", "A2", "B1", "B2", "A med", "B med"))
          + "{:>9}".format("B-A %") + "{:>8}".format("A sprd") + "{:>8}".format("B sprd") + "  flag")
    for k in sorted(set(A1) | set(B1), key=lambda x: (isinstance(x, str), x)):
        av = [v for v in (A1.get(k), A2.get(k)) if v]
        bv = [v for v in (B1.get(k), B2.get(k)) if v]
        if not av or not bv:
            print(f"    {str(k)[:9]:>9s} {'-':>9s} {'-':>9s} {'-':>9s} {'-':>9s}   missing arm data")
            continue
        am, bm = st.median(av), st.median(bv)
        # Spread of BOTH arms, not just the control. Judging a delta against the A-arm null
        # alone hides the case that actually happened here at 2048 tokens, where B itself
        # swung 12% between boots: that is instability in one arm, not an arm effect.
        def spread(x, y, m):
            return abs(x - y) / m * 100 if (x and y and m) else float("nan")
        null_a = spread(A1.get(k), A2.get(k), am)
        null_b = spread(B1.get(k), B2.get(k), bm)
        delta = (bm / am - 1) * 100
        floor_pct = (abs(bm - am) / am * 100) if am else 0.0
        flag = ""
        if is_time and abs(bm - am) < 0.05:      # absolute floor: 50 ms
            flag = "delta under 50 ms: not meaningful"
        elif abs(delta) > 3 and not (null_a != null_a and null_b != null_b) \
                and abs(delta) > max(null_a, null_b):
            flag = "beyond 3% and beyond both arm spreads"
        elif abs(delta) > 3:
            flag = "within arm spread -- a tie"
        g = lambda v: (v or 0)
        print("    " + "{:>10}".format(str(k)[:10])
              + "".join(cell.format(g(v)) for v in (A1.get(k), A2.get(k), B1.get(k), B2.get(k), am, bm))
              + "{:>8.2f}%".format(delta) + "{:>8.2f}%".format(null_a) + "{:>8.2f}%".format(null_b)
              + "  " + flag)

#!/usr/bin/env python3
"""Summarise a paired A'/B boot set: point-by-point medians, the B-A
delta, and the same-arm null spread every delta must be judged against.

Usage: analyze-pin-pair.py <receipt-dir>   with A1-decode.json, A2-decode.json, B1-... etc.
"""
print("\n=== A' (pinned round2) vs B (exact-size): same image, same context mode ===")
table("decode throughput", ("decode", "decode"), "decode_tokens_per_second", "tok/s")
table("prefill throughput", ("prefill", "prefill"), "effective_prompt_tokens_per_second", "tok/s")
print("\n  Two boots per arm means the median is the mean of two samples: a single slow run")
print("  moves the whole column. Rows where |B-A| is comparable to the null spread are ties,")
print("  not wins -- read them against the null column, not against zero.")
table("time to first token", ("prefill", "prefill"), "ttft_seconds", "s", is_time=True)
PAIRDONE_MARKER = True

# Paired estimator: KL(BF16-tree || quant-engine) from BF16 top-64+lse vs engine top-20.
# Truncation is handled by tail-closure: p-tokens missing from the engine top-20 share the
# engine tail mass (1 - Q_known) proportionally to their p mass, and the unobserved tails
# contribute P_tail*log(P_tail/Q_tail). Biased-but-monotone approximation; absolute values
# are calibrated against K4's exact tree KLD (basekld-k4.json) computed over the same rows.
import json, math, sys, torch
d = torch.load("/hf/sc-measure/bf16-logits.pt", weights_only=False)
tv, ti, lse = d["topk_vals"], d["topk_ids"], d["lse"]
TRUE = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {"k4": 0.020856}
def est(path):
    eng = torch.load(path, weights_only=False)
    tot, npos = 0.0, 0
    for r, step in enumerate(eng):
        for c, pos in enumerate(step):   # engine pos c -> tree col c-1
            if not pos or c == 0: continue
            t = c - 1
            p_lp = tv[r, t].tolist(); p_ids = ti[r, t].tolist()
            plse = float(lse[r, t])
            pmap = {tok: math.exp(lp - plse) for tok, lp in zip(p_ids, p_lp)}
            qmap = {int(k): math.exp(v) for k, v in pos.items()}
            Pk = sum(pmap.values()); Qk = sum(qmap.values())
            Qt = max(1e-12, 1.0 - Qk)                       # engine tail mass
            Pun = max(0.0, 1.0 - Pk)                        # bf16 mass beyond top-64
            Pmiss = sum(v for t_, v in pmap.items() if t_ not in qmap)
            kl = sum(v * math.log(v / qmap[t_]) for t_, v in pmap.items() if t_ in qmap)
            kl += (Pmiss + Pun) * math.log(max(Pmiss + Pun, 1e-12) / Qt)
            tot += max(kl, 0.0); npos += 1
    return tot / max(1, npos), npos
K4_TRUE = TRUE["k4"]
rows = {}
for name in ("k4", "vendor425"):
    v, n = est(f"/hf/sc-measure/engine-lp-{name}.pt")
    rows[name] = v
    print(f"KL_EST {name}: raw={v:.5f} over {n} positions")
cal = rows["k4"] / K4_TRUE
print(f"CALIB k4 estimator/true ratio: {cal:.3f} (1.0 = unbiased)")
print(f"VENDOR425 ENGINE-KL: raw={rows['vendor425']:.5f}  bias-corrected={rows['vendor425']/cal:.5f}")
print(f"COMPARE vs tree KLDs: k4=0.020856 measured425=0.021494 measured459=0.019843")

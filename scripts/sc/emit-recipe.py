# Emit measured@4.25 recipe: replicate optimize_model's knapsack (code copied verbatim),
# map chosen per-group candidate rates onto every tensor, write the -rcp yaml.
import json, sys, os
from exllamav3 import Config, Model
MEAS, IDX, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
BUDGET = float(sys.argv[4]) if len(sys.argv) > 4 else 4.25
# Engine capability contract (T2 audit 2026-10-17 -> docs/engine-tier-capability.md):
#   uniform-layer     : one rate per layer, any integers 2..6 (l2 measurements; proven fleet-wide)
#   rank-sliced-mixed : per-expert tiers, 2..3 DISTINCT rates, no adjacency (b12x w4a16; adapter len==2)
#   proj-mixed-ep     : per-projection tiers on TP2+EP, 2..3 DISTINCT+CONSECUTIVE within canonical
#                       (3,4,5) (b12x fused_moe trellis; vendor {4,5} is the proven point)
# TIER_WHITELIST constrains the KNAPSACK itself (floor = lowest allowed rate), so a solve can
# never emit a card the engine will reject at boot.
ENGINE_MODE = os.environ.get("ENGINE_MODE", "proj-mixed-ep")
TIER_WHITELIST = tuple(int(x) for x in os.environ.get("TIER_WHITELIST", "").split(",") if x)
meas = json.load(open(MEAS))
model_base = Model.from_config(Config.from_directory(meas["base"]["dir"]))
base_numel = base_cost = 0
for g in meas["groups"]:
    for l in g["layers"]:
        m = model_base.find_module(l)
        base_cost += m.storage_size() * 8; base_numel += m.weights_numel()
num_q = len(meas["alts"])

def optimize(meas):  # optimize_model logic + engine-tier constraint layer
    groups = meas["groups"]
    alt_r = [round(a["bpw"]) for a in meas["alts"]]
    base_r = round(meas["base"]["bpw"])
    ok = (lambda r: True) if not TIER_WHITELIST else (lambda r: r in TIER_WHITELIST)
    allowed = {0} if ok(base_r) else set()
    allowed |= {j + 1 for j, r in enumerate(alt_r) if ok(r)}
    assert allowed, f"whitelist {TIER_WHITELIST} admits no candidate rates"
    floor = min(allowed)
    solution = [floor] * len(groups)
    budget = BUDGET * base_numel - base_cost
    def adjust(dkld): return dkld if dkld > 0 else -((-dkld) ** 0.69)
    while True:
        best, best_r = None, 0.0
        for i, g in enumerate(groups):
            cand, s = g["candidates"], solution[i]
            for j, c in enumerate(cand):
                if j < s or j >= num_q or (j + 1) not in allowed: continue
                dk = adjust(c["dkld"]); db = c["dbits"]
                if s > 0: dk -= adjust(cand[s - 1]["dkld"]); db -= cand[s - 1]["dbits"]
                r = 1e10 * dk / (db + 1)
                if r < best_r and budget > db: best, best_r = (i, j, db), r
        if best is None: break
        i, j, k = best; solution[i] = j + 1; budget -= k
    return solution

sol = optimize(meas)
alt_rates = [round(a["bpw"]) for a in meas["alts"]]
rate_of = {}
for s, g in zip(sol, meas["groups"]):
    r = round(meas["base"]["bpw"]) if s == 0 else alt_rates[s - 1]
    for key in g["layers"]: rate_of[key] = r
spent = sum(g["candidates"][s-1]["dbits"] for s, g in zip(sol, meas["groups"]) if s > 0)
SUFFIXES = {"weight", "bias", "trellis", "suh", "svh", "mcg", "mul1"}
# Base = the tuned recipe's converter-approved key set (same budgeted tensor names).
# Only the measured group tensors get their rates replaced; all other entries carry over.
base_lines = [l.rstrip("\n") for l in open("/hf/sc-measure/base-recipe.yaml")]
tstart = base_lines.index("tensors:")
tensors = {}
for l in base_lines[tstart + 1:]:
    if not l.strip(): continue
    k, v = l.split(":"); tensors[k.strip()] = int(v)
missing = [k for g in meas["groups"] for k in g["layers"] if k not in tensors]
assert not missing, f"measurement keys absent from base recipe: {missing[:3]}"
n_grp = 0
for s_, g in zip(sol, meas["groups"]):
    r = round(meas["base"]["bpw"]) if s_ == 0 else alt_rates[s_ - 1]
    for k in g["layers"]: tensors[k] = r; n_grp += 1
import collections
hist = collections.Counter(tensors[k] for k in tensors if ".experts." in k and "shared" not in k and not k.startswith("mtp."))
# ---- final engine-capability validation (belt and braces; fail at emit, not boot) ----
import re as _re
by_layer = {}
for key, rate in rate_of.items():
    _m = _re.search(r"layers\.(\d+)\.", key)
    if _m: by_layer.setdefault(int(_m.group(1)), set()).add(rate)
viol = []
for lyr, rates in sorted(by_layer.items()):
    rs = sorted(rates)
    if ENGINE_MODE == "uniform-layer" and len(rs) != 1:
        viol.append(f"layer {lyr}: {rs} not layer-uniform")
    if ENGINE_MODE == "rank-sliced-mixed" and len(rs) > 2:
        viol.append(f"layer {lyr}: {len(rs)} distinct rates {rs} > 2")
    if ENGINE_MODE == "proj-mixed-ep":
        if len(rs) > 3 or (len(rs) > 1 and any(b != a + 1 for a, b in zip(rs, rs[1:]))):
            viol.append(f"layer {lyr}: {rs} not <=3 consecutive")
        if not set(rs) <= {3, 4, 5}:
            viol.append(f"layer {lyr}: {rs} outside canonical (3,4,5) family")
if viol and ENGINE_MODE != "none":
    print(f"ENGINE-GUARD VIOLATIONS (mode={ENGINE_MODE}):")
    for v in viol[:8]: print("  " + v)
    print(f"  ... {len(viol)} total")
    sys.exit(2)
print(f"engine-guard OK mode={ENGINE_MODE} whitelist={list(TIER_WHITELIST) or 'any'}; "
      f"{len(by_layer)} layers within contract")

with open(OUT, "w") as f:
    f.write(f"# measured recipe: optimize knapsack @ -b {BUDGET} on panel-l3 measurement (base K3, alts K4/K5/K6)\n"
            f"# key set = tuned recipe (converter-approved); rates: {n_grp} expert tensors from measurement\n"
            f"achieved_bpw: {(base_cost + spent) / base_numel:.4f}\ntarget_bpw: {BUDGET}\nhead_bits: 16\ntensors:\n")
    for k in sorted(tensors): f.write(f"  {k}: {tensors[k]}\n")
print("recipe tensors:", len(tensors), "| expert rate hist:", dict(sorted(hist.items())), "| achieved:", f"{(base_cost + spent) / base_numel:.4f}")

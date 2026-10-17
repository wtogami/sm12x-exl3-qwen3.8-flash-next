# Emit measured@4.25 recipe: replicate optimize_model's knapsack (code copied verbatim),
# map chosen per-group candidate rates onto every tensor, write the -rcp yaml.
import json, sys
from exllamav3 import Config, Model
MEAS, IDX, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
BUDGET = float(sys.argv[4]) if len(sys.argv) > 4 else 4.25
meas = json.load(open(MEAS))
model_base = Model.from_config(Config.from_directory(meas["base"]["dir"]))
base_numel = base_cost = 0
for g in meas["groups"]:
    for l in g["layers"]:
        m = model_base.find_module(l)
        base_cost += m.storage_size() * 8; base_numel += m.weights_numel()
num_q = len(meas["alts"])

def optimize(meas):  # verbatim logic from conversion/optimize_model.py
    groups = meas["groups"]; solution = [0] * len(groups)
    budget = BUDGET * base_numel - base_cost
    def adjust(dkld): return dkld if dkld > 0 else -((-dkld) ** 0.69)
    while True:
        best, best_r = None, 0.0
        for i, g in enumerate(groups):
            cand, s = g["candidates"], solution[i]
            for j, c in enumerate(cand):
                if j < s or j >= num_q: continue
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
with open(OUT, "w") as f:
    f.write(f"# measured recipe: optimize knapsack @ -b {BUDGET} on panel-l3 measurement (base K3, alts K4/K5/K6)\n"
            f"# key set = tuned recipe (converter-approved); rates: {n_grp} expert tensors from measurement\n"
            f"achieved_bpw: {(base_cost + spent) / base_numel:.4f}\ntarget_bpw: {BUDGET}\nhead_bits: 16\ntensors:\n")
    for k in sorted(tensors): f.write(f"  {k}: {tensors[k]}\n")
print("recipe tensors:", len(tensors), "| expert rate hist:", dict(sorted(hist.items())), "| achieved:", f"{(base_cost + spent) / base_numel:.4f}")

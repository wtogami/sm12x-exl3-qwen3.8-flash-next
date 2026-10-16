#!/usr/bin/env python3
"""PLAN A heuristic recipe: tuned@~5.65 avg over routed experts (language-model stack).
Prior: ends-first (first/last layers most sensitive — literature-standard), middle
layers -1 bit, a demoted low-sensitivity band -2. Dense/attention/GDN/shared/indexer
inherit the vendor template's values (16 = BF16). Format == recipe-uniform*.yaml, so
convert_model -rcp consumes it unchanged.
usage: gen-recipe.py [target_expert_bpw=5.65]"""
import sys, json, collections, yaml
TEMPLATE = "/home/opencode/.cache/huggingface/req6-build/recipe-uniform4.yaml"
TARGET = float(sys.argv[1]) if len(sys.argv) > 1 else 5.65
tpl = yaml.safe_load(open(TEMPLATE))
tensors = tpl["tensors"]

def layer_of(k):
    p = k.split(".")
    return int(p[p.index("layers") + 1])

experts = {k: layer_of(k) for k in tensors if ".mlp.experts." in k and not k.startswith("mtp.")}
per_layer = collections.Counter(experts.values())
NL = max(per_layer) + 1
tot = sum(per_layer.values())

def avg_of(prof):
    return sum(per_layer[l] * prof[l] for l in range(NL)) / tot

# Profile family: everything at 6 except a contiguous CENTRAL band at 5 (width w).
# avg(w) = (6*(NL-w) + 5*w)/NL -> solve w for TARGET; scan +-3 for the closest grid point.
best = None
for w in range(0, NL - 4):
    lo = (NL - w) // 2                    # centre the 5-band: max distance from sensitive ends
    prof = {l: (5 if lo <= l < lo + w else 6) for l in range(NL)}
    err = abs(avg_of(prof) - TARGET)
    if best is None or err < best[0]:
        best = (err, prof)
prof = best[1]
avg = avg_of(prof)

out = dict(tensors={k: (prof[layer_of(k)] if k in experts else v) for k, v in tensors.items()},
           head_bits=16)
with open("/nvme1/sc/recipe-tuned565.yaml", "w") as f:
    f.write(f"# heuristic tuned recipe (plan A): LM-expert avg {avg:.3f} bpw; dense 16; ends-first\n")
    yaml.safe_dump(out, f, sort_keys=True)
bands = collections.defaultdict(list)
for l, b in prof.items():
    bands[b].append(l)
print(f"expert avg = {avg:.4f} (target {TARGET}); tot expert tensors {tot}")
for b in sorted(bands, reverse=True):
    print(f"  {b}-bit layers: {bands[b]}")
json.dump({"target": TARGET, "achieved": avg, "profile": {str(k): v for k, v in sorted(prof.items())}},
          open("/tmp/recipe-profile.json", "w"), indent=1)
print("wrote /nvme1/sc/recipe-tuned565.yaml,", len(tensors), "entries")

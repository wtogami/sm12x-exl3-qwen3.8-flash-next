p = "/home/opencode/.cache/huggingface/engine-overlay/exl3.py"
s = open(p).read()

a1 = "        device_index = x.device.index\n        key = ("
assert s.count(a1) == 1
r1 = """        device_index = x.device.index
        # port-exl3-tier-key: cross-layer mixed-tier cards (tuned 5/6 recipes)
        # share shapes/topk but have distinct weight plans per tier; without the
        # tier tuple the first layer's plan is cached and b12x bind rejects
        # later tiers ("experts do not match the EP scratch weight plan").
        layer_bitrates = tuple(sorted(set(getattr(layer, "exl3_layer_bitrates", ()))))
        key = ("""
s = s.replace(a1, r1)

a2 = "            use_ep,\n            projection_mixed,\n            topk,"
assert s.count(a2) == 1
s = s.replace(a2, "            use_ep,\n            projection_mixed,\n            layer_bitrates,\n            topk,")

open(p, "w").write(s)
compile(s, "exl3.py", "exec")
print("both edits applied; compiles")

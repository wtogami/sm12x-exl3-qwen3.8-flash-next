#!/usr/bin/env python3
"""Synthesize missing tensor_storage ledger entries for Swift 6.05's MTP draft.

The exllamav3 vintage that produced this card quantized the MTP module
(mtp_bits 4/6, mul1) but never wrote mtp.* entries into
quantization_config.json, so the fork's prefix gates classify draft tensors
as not-EXL3 and the loader meets trellis keys against the wrong methods.
Our own K4.25 card carries exactly these entries under the same
'mtp.layers.N…' prefix, so synthesizing them from the index restores the
supported contract. Width-640 shared experts are NOT ledgered - they are
dequanted to bf16 by exl3-605-shared-dequant.py instead (Hadamard-128 TP
guard). Entries: quant_format=exl3 + bits_per_weight read from the actual
trellis shape (16*K per trailing-16 block), matching the sidecar schema.

Usage: python3 scripts/exl3-605-mtp-ledger.py --snapshot DIR [--dry-run]
"""
import argparse, json, os, re, sys
from safetensors import safe_open

QUANT_SUFFIX = ("trellis", "suh", "svh", "mul1", "mcg")
SKIP = re.compile(r"shared_expert\.(gate|up|down)_proj$")  # dequanted, not ledgered

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    snap = a.snapshot
    idx_path = os.path.join(snap, "model.safetensors.index.json")
    sidecar_path = os.path.join(snap, "quantization_config.json")
    wm = json.load(open(idx_path))["weight_map"]
    side = json.load(open(sidecar_path))
    ts = side["tensor_storage"]

    mods = {}
    for k in wm:
        m = re.match(r"^(mtp\.[A-Za-z_.0-9]*?)\.(" + "|".join(QUANT_SUFFIX) + r")$", k)
        if m and not SKIP.search(m.group(1)):
            mods.setdefault(m.group(1), set()).add(m.group(2))
    print(f"mtp quantized modules in index: {len(mods)}")
    hdrs = {}
    def header(fname):
        if fname not in hdrs:
            with open(os.path.join(snap, fname), "rb") as f:
                n = struct.unpack("<Q", f.read(8))[0]
                hdrs[fname] = json.loads(f.read(n))
        return hdrs[fname]
    import struct
    added, stored = 0, 0
    for mod, suffixes in sorted(mods.items()):
        if "trellis" not in suffixes:
            continue  # marker-less groups are not exl3 storages
        tr_file = wm[f"{mod}.trellis"]
        tr = header(tr_file)[f"{mod}.trellis"]
        k_bits = tr["shape"][2] // 16
        entry = {"stored_tensors": {}, "quant_format": "exl3", "bits_per_weight": k_bits}
        if "mul1" in suffixes:
            entry["mul1_multiplier"] = 2212286765
        for s in sorted(suffixes):
            name = f"{mod}.{s}"
            meta = header(wm[name])[name]
            entry["stored_tensors"][name] = {"shape": meta["shape"], "n_bytes": meta.get("n_bytes", 0), "dtype": meta["dtype"]}
            stored += 1
        ts[mod] = entry
        added += 1
    print(f"entries added: {added} (stored_tensors described: {stored}); dry-run: {a.dry_run}")
    if not a.dry_run:
        json.dump(side, open(sidecar_path, "w"))
        print(f"sidecar entries now: {len(ts)}")
    return 0

if __name__ == "__main__":
    sys.exit(main())

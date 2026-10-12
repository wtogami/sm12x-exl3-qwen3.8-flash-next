#!/usr/bin/env python3
"""Assert the PLE n-gram table is stored in a form a byte-copy gather can read.

The mmap and streaming lookup paths copy rows as raw bytes:
_gather_mapped_rows_kernel casts the row base to uint8 and moves
ROW_BYTES = embedding_dim * dtype.itemsize, and the only dequantization on the
other side is a multiply by a single global scale.  Three checkpoint properties
follow from that, and the loader does not check all of them:

  * dtype in {BF16, F8_E4M3}            checked by the loader
    ("PLE shard N is stored as X ... checkpoint_mapped cannot convert rows")
  * shard shape, coverage, duplicates   checked by the loader
  * the FP8 scale is ONE scalar         NOT checked -- process_weights_after_loading
    only rejects a scale that was never loaded (a float32-min sentinel), so a
    per-channel or per-group scale reaches PerTensorScaleParameter, which keeps a
    single element instead of refusing.  The numbers change silently.
  * no padding inside a shard tensor    NOT checked -- discover_table_layout keeps
    (path, offset, rows) and derives every row address from itemsize alone, so a
    tensor whose byte span exceeds rows*dim*itemsize would be read at the wrong
    stride, silently, and would also change the page-amplification arithmetic.

So this check covers the two the loader cannot see, plus the repo's own
PLE_BYTES_PER_ELEMENT, which qwen38_ple_memcheck() sizes the pinned table from.
It reads only safetensors headers, so it runs on a host in well under a second,
against the real checkpoints, before any engine exists -- the same argument that
puts the memory preflight in start.sh rather than in the loader.

Deliberately a plain script like the other scripts/test-*.py guards: stdlib only,
no torch, no numpy, no pytest, no container, no GPU, so it also runs on a host.
Checkpoints that are not downloaded are reported as skipped, never as failures,
so a machine holding one profile still gets a useful exit code.

Run:  python3 scripts/test-ple-storage-format.py [snapshot-dir ...]
"""
import json
import os
import re
import struct
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]

ITEMSIZE = {"BF16": 2, "F16": 2, "F32": 4, "F8_E4M3": 1, "F8_E5M2": 1}
ALLOWED_PLE = ("BF16", "F8_E4M3")
FLOATISH = ("BF16", "F16", "F32")
SUFFIX = re.compile(
    r"layers\.(\d+)\.ple\.ple_embedding\.ngram_embedding\.(?:shard_(\d+)\.weight|weight_scale)$"
)


def profiles():
    """{name: repo} from model-profiles.sh, the file that already owns this list."""
    out, cur = {}, None
    for line in (root / "model-profiles.sh").read_text().splitlines():
        m = re.match(r"\s{2}([a-z0-9-]+)\)\s*$", line)
        if m:
            cur = m.group(1)
            out[cur] = {}
            continue
        if line.strip().startswith(";;"):
            cur = None
            continue
        if cur:
            kv = re.match(r"\s*([A-Z_]+)=(.*)$", line)
            if kv:
                out[cur][kv.group(1)] = kv.group(2).strip()
    return out


def header(path):
    with open(path, "rb") as fh:
        n = struct.unpack("<Q", fh.read(8))[0]
        return json.loads(fh.read(n))


def scan(snapshot):
    """{layer: {"shards": {i: (dtype, shape, span)}, "scale": (dtype, shape) | None}}"""
    layers = {}
    for entry in sorted(os.listdir(snapshot)):
        if not entry.endswith(".safetensors"):
            continue
        blob = os.path.realpath(os.path.join(snapshot, entry))
        if not os.path.exists(blob):
            raise SystemExit(f"dangling snapshot link: {entry}")
        for key, meta in header(blob).items():
            if key == "__metadata__" or not isinstance(meta, dict):
                continue
            m = SUFFIX.search(key)
            if not m:
                continue
            layer = layers.setdefault(int(m.group(1)), {"shards": {}, "scale": None})
            dtype = meta["dtype"]
            shape = tuple(meta["shape"])
            span = meta["data_offsets"][1] - meta["data_offsets"][0]
            if m.group(2) is not None:
                layer["shards"][int(m.group(2))] = (dtype, shape, span)
            else:
                layer["scale"] = (dtype, shape)
    return layers


def check(name, layers, declared_bytes, fails):
    """One profile. Appends human-readable failures; returns a summary string."""
    if not layers:
        return "no PLE n-gram tensors found"
    problems, notes = [], []
    for layer, info in sorted(layers.items()):
        shards = info["shards"]
        if not shards:
            problems.append(f"layer {layer}: no shard tensors")
            continue
        dtypes = {d for d, _, _ in shards.values()}
        if len(dtypes) > 1:
            problems.append(f"layer {layer}: mixed storage dtypes {sorted(dtypes)}")
        dtype = sorted(dtypes)[0]
        if dtype not in ALLOWED_PLE:
            problems.append(f"layer {layer}: PLE stored as {dtype}, a byte-copy gather "
                            f"supports only {' or '.join(ALLOWED_PLE)}")
            continue
        if declared_bytes and ITEMSIZE[dtype] != declared_bytes:
            problems.append(f"layer {layer}: checkpoint stores {dtype} "
                            f"({ITEMSIZE[dtype]} B/element) but model-profiles.sh declares "
                            f"PLE_BYTES_PER_ELEMENT={declared_bytes}; the pinned-memory "
                            f"preflight sizes the table from that value")
        shapes = {s for _, s, _ in shards.values()}
        if len(shapes) > 1:
            problems.append(f"layer {layer}: shards disagree on shape {sorted(shapes)}; "
                            f"ROW_BYTES is a single compile-time constant")
            continue
        rows, dim = shapes.pop()
        want = rows * dim * ITEMSIZE[dtype]
        padded = {i: sp for i, (_, _, sp) in shards.items() if sp != want}
        if padded:
            problems.append(f"layer {layer}: {len(padded)} shard(s) whose byte span is not "
                            f"rows*dim*itemsize (e.g. shard {sorted(padded)[0]} spans "
                            f"{padded[sorted(padded)[0]]:,} != {want:,}); row addressing "
                            f"assumes no padding inside a tensor")
        scale = info["scale"]
        if dtype == "F8_E4M3":
            if scale is None:
                problems.append(f"layer {layer}: FP8 PLE with no weight_scale tensor")
            else:
                sdt, sshape = scale
                if tuple(sshape) not in ((), (1,)):
                    problems.append(f"layer {layer}: weight_scale has shape {tuple(sshape)}, "
                                    f"not a single scalar; dequantize() multiplies once by a "
                                    f"global scale, and PerTensorScaleParameter would keep one "
                                    f"element of a per-channel scale rather than refuse")
                if sdt not in FLOATISH:
                    problems.append(f"layer {layer}: weight_scale dtype {sdt} is not a float type")
        elif scale is not None:
            problems.append(f"layer {layer}: BF16 PLE carries a weight_scale tensor; "
                            f"the unquantized method would ignore it and the numbers would "
                            f"quietly differ from the checkpoint's intent")
        notes.append(f"layer {layer}: {len(shards)} shards {dtype} [{rows}, {dim}], "
                     f"ROW_BYTES={dim * ITEMSIZE[dtype]}, "
                     f"scale={tuple(scale[1]) if scale else 'none'}")
    fails.extend(f"{name}: {p}" for p in problems)
    return "; ".join(notes) if not problems else f"{len(problems)} problem(s)"


def main():
    args = sys.argv[1:]
    fails = []
    checked = skipped = 0
    if args:
        targets = [("explicit", Path(a)) for a in args]
    else:
        cache = Path(os.environ.get("HF_CACHE", Path.home() / ".cache/huggingface")) / "hub"
        if not cache.is_dir():
            print(f"no Hugging Face cache at {cache}; nothing to check")
            return 0
        targets = []
        for name, prof in sorted(profiles().items()):
            repo = prof.get("MODEL_REPO", "")
            if not repo:
                fails.append(f"{name}: model-profiles.sh branch sets no MODEL_REPO")
                continue
            want = "models--" + repo.replace("/", "--")
            dirs = sorted((cache / want / "snapshots").glob("*/"))
            if not dirs:
                skipped += 1
                print(f"  SKIP  {name:16s} {repo} not downloaded")
                continue
            targets.append((name, dirs[0], int(prof.get("PLE_BYTES_PER_ELEMENT", "0") or 0)))
    for target in targets:
        name, snap = target[0], target[1]
        decl = target[2] if len(target) > 2 else 0
        try:
            layers = scan(snap)
        except SystemExit:
            raise
        except Exception as exc:                       # unreadable checkpoint is a real failure
            fails.append(f"{name}: cannot read {snap}: {exc}")
            continue
        print(f"  OK    {name:16s} {check(name, layers, decl, fails)}")
        checked += 1
    print()
    if fails:
        print(f"PASSED {checked - len({f.split(':')[0] for f in fails})} profile(s), "
              f"{len(fails)} violation(s):")
        for f in fails:
            print(f"  - {f}")
        return 1
    print(f"PASSED: {checked} profile(s) stored as a byte-copyable global-scale table; "
          f"{skipped} not downloaded")
    return 0


if __name__ == "__main__":
    sys.exit(main())

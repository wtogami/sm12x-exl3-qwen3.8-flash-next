#!/usr/bin/env python3
"""Hardlink-dedup n-gram shards across card out/ dirs.
Byte-identical files ONLY: group by sha256, then replace duplicates with
hardlinks to one canonical inode. Never touches non-matching files."""
import hashlib, os, sys, glob
CARDS = sys.argv[1:] or [
    "/home/opencode/.cache/huggingface/req3-build/out",
    "/home/opencode/.cache/huggingface/req4-build/out",
    "/home/opencode/.cache/huggingface/req5-build/out",
    "/home/opencode/.cache/huggingface/req6-build/out",
    "/home/opencode/.cache/huggingface/req6b-build/out",
]
def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 24), b""):
            h.update(chunk)
    return h.hexdigest()
groups = {}
for d in CARDS:
    for f in sorted(glob.glob(os.path.join(d, "model-ngram-*.safetensors"))):
        st = os.stat(f)
        if st.st_nlink > 1:
            print(f"skip (already linked x{st.st_nlink}): {f}"); continue
        print(f"hash: {f}", flush=True)
        groups.setdefault(sha256(f), []).append(f)
linked = freed = 0
for dg, files in groups.items():
    if len(files) < 2:
        print(f"unique ({os.path.getsize(files[0])/2**30:.1f} GiB): {files[0]}"); continue
    canon = files[0]
    for dup in files[1:]:
        tmp = dup + ".linktmp"
        os.link(canon, tmp); os.replace(tmp, dup)
        linked += 1; freed += os.path.getsize(dup)
        print(f"linked {dup} -> inode of {canon}", flush=True)
    ino = os.stat(canon).st_ino
    assert all(os.stat(f).st_ino == ino for f in files), "inode check failed"
print(f"DONE: groups={sum(1 for g in groups.values() if len(g)>1)} files_linked={linked} bytes_freed={freed/2**40:.2f} TiB")

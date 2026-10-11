#!/usr/bin/env python3
"""Turn OFF btrfs compression for just the PLE n-gram table blobs.

Why: both hosts mount the checkpoint dataset compress=zstd:1 and the PLE
tensors do compress (measured zlib 0.784 on a real BF16 n-gram shard).  A
compressed extent must be read whole to be decompressed -- fs/btrfs/direct-io.c
returns -ENOTBLK and falls back to buffered I/O for compressed extents -- so a
cold 4 KiB random read pulled 26.0x the logical bytes off the device, against
1.00x for the same bytes stored uncompressed.  Until this is fixed, streaming
measurements on this dataset measure zstd and the page cache, not the SSD, and
'fio --direct=1' is not a valid control.

Mechanism: chattr +C on an EMPTY file, then write.  Measured on these hosts:
+C on a file, or on a directory for files created inside it, gives ratio 1.0000
and zero 'encoded' extents.  chattr -c does NOT override a mount-level
compress=, and 'btrfs property set compression' is rejected on regular files
because that property is subvolume-scoped.  +C governs future writes only, so
existing extents are replaced by rewriting the file.

Integrity: Hugging Face names a blob with the sha256 of its content (verified
here on a full 8.6 GiB blob), so the filename is the oracle.  Each blob is
hashed while copying and checked against its name before the original is
replaced; a mismatch aborts.  Files are replaced one at a time by an atomic
rename inside the blobs directory, so snapshot symlinks keep resolving and an
interrupted run leaves the source intact.

Scope: this changes the write path (nodatacow) and the on-disk size (the file
grows to its logical size) of the listed blobs only.  Bytes are unchanged, and
nothing else on the filesystem is affected.
"""
import argparse, hashlib, os, re, stat, struct, subprocess, sys

NGRAM = re.compile(r"ngram_embedding\.shard_\d+\.weight$")
HEX64 = re.compile(r"^[0-9a-f]{64}")
CHUNK = 8 << 20


def die(msg):
    raise SystemExit(f"ple-uncompress: {msg}")


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def encoded_extents(path):
    """Number of compressed/encoded extents, or None if filefrag cannot tell."""
    r = run(["filefrag", "-v", path])
    if r.returncode != 0 or "FIBMAP" in r.stderr or "Inappropriate ioctl" in r.stderr:
        return None
    out = r.stdout
    return sum(1 for line in out.splitlines()
               if re.search(r"\b(encoded|compressed)\b", line))


def read_header(blob):
    with open(blob, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        import json
        return json.loads(f.read(n))


def scan(snapshot):
    """[(entry, blob, ple_bytes, file_bytes)] for blobs holding PLE shards."""
    seen, hits = {}, []
    for entry in sorted(os.listdir(snapshot)):
        if not entry.endswith(".safetensors"):
            continue
        blob = os.path.realpath(os.path.join(snapshot, entry))
        if blob in seen:
            continue
        seen[blob] = True
        if not os.path.exists(blob):
            die(f"dangling snapshot link: {entry}")
        try:
            header = read_header(blob)
        except Exception as e:
            die(f"cannot read header of {blob}: {e}")
        ple = 0
        for k, v in header.items():
            if k == "__metadata__" or not isinstance(v, dict):
                continue
            if "data_offsets" in v and NGRAM.search(k):
                ple += v["data_offsets"][1] - v["data_offsets"][0]
        if ple:
            spans = [(v["data_offsets"][0], v["data_offsets"][1])
                     for k, v in header.items()
                     if k != "__metadata__" and isinstance(v, dict)
                     and "data_offsets" in v and NGRAM.search(k)]
            hits.append((entry, blob, ple, os.path.getsize(blob), spans))
    return hits


def ple_encoded_extents(path, spans):
    """Bytes of compressed extents that intersect the PLE tensor ranges.

    filefrag -v columns are: idx, logical_start, logical_end, phys_start,
    phys_end, length_blocks, [expected], flags.  A whole-file count would blame
    other tensors sharing a shard for the PLE table, so restrict to the tensor
    byte ranges.  Returns ((compressed_bytes, extents_in_range), block) or
    (None, None) when the extent map cannot be read.
    """
    r = run(["filefrag", "-v", path])
    if r.returncode != 0 or "FIBMAP" in r.stderr or "Inappropriate ioctl" in r.stderr:
        return None, None
    block = 4096
    m = re.search(r"blocks of (\d+) bytes", r.stdout)
    if m:
        block = int(m.group(1))
    # filefrag prints "lo..hi:" for a multi-block extent but a bare "lo:" for a
    # single-block one, which shifts every later column.  Parse the logical
    # range explicitly and derive length from it.
    row = re.compile(r"^\s*(\d+):\s*(\d+)(?:\.\.\s*(\d+))?:\s*"
                     r"(\d+)(?:\.\.\s*(\d+))?:\s*(\d+):(.*)$")
    compressed = 0
    in_range = 0
    for line in r.stdout.splitlines():
        m = row.match(line)
        if not m:
            continue
        lo_blk, hi_blk = int(m.group(2)), int(m.group(3) or m.group(2))
        flags = m.group(7)
        lo = lo_blk * block
        length = (hi_blk - lo_blk + 1) * block
        if not any(lo >= a - block and lo < b for a, b in spans):
            continue
        in_range += 1
        if re.search(r"\b(encoded|compressed)\b", flags):
            compressed += length
    return (compressed, in_range), block


def rewrite(blob, dry):
    name = os.path.basename(blob)
    oracle = HEX64.match(name)
    if not oracle:
        die(f"blob {name} is not named by content sha256; no integrity oracle")
    oracle = oracle.group(0)      # HF blobs are bare hex; allow a trailing suffix
    size = os.path.getsize(blob)
    if dry:
        print(f"  would rewrite {name[:16]}\u2026  {size/2**30:.2f} GiB")
        return
    tmp = blob + ".uncompress.tmp"
    if os.path.exists(tmp):
        die(f"refusing to clobber existing {tmp}")
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
        os.close(fd)
        r = run(["chattr", "+C", tmp])          # must be empty at this point
        if r.returncode != 0:
            die(f"chattr +C failed on {tmp}: {r.stderr.strip()}")
        h = hashlib.sha256()
        src = os.open(blob, os.O_RDONLY)
        dst = os.open(tmp, os.O_WRONLY)
        done = 0
        try:
            while True:
                buf = os.read(src, CHUNK)
                if not buf:
                    break
                h.update(buf)
                os.write(dst, buf)
                done += len(buf)
            os.fsync(dst)
        finally:
            os.close(src); os.close(dst)
        if done != size:
            die(f"short copy of {name[:16]}\u2026: {done} != {size}")
        got = h.hexdigest()
        if got != oracle:
            die(f"sha256 mismatch for {name[:16]}\u2026: computed {got}, name says {oracle}")
        enc = encoded_extents(tmp)
        if enc:
            die(f"{name[:16]}\u2026 still has {enc} encoded extents; +C did not apply")
        os.replace(tmp, blob)                   # atomic, same directory
        print(f"  rewrote {name[:16]}\u2026  {size/2**30:.2f} GiB  "
              f"encoded_extents={enc}  sha256 verified against filename")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshot", help="a .../snapshots/<rev>/ directory")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-compressed-mib", type=int, default=64, metavar="MIB",
                    help="skip a file unless at least this many bytes of its PLE "
                         "ranges are compressed (default 64: rewriting a multi-GiB "
                         "file to decompress a stray extent is a bad trade)")
    a = ap.parse_args()

    snap = os.path.abspath(a.snapshot)
    if not os.path.isdir(snap) or not os.path.exists(os.path.join(snap, "config.json")):
        die(f"{snap} does not look like a checkpoint snapshot dir")
    fstype = run(["stat", "-f", "-c", "%T", snap]).stdout.strip()
    if fstype != "btrfs":
        die(f"{snap} is on {fstype}, not btrfs; the compression flag is meaningless")
    opts = run(["findmnt", "-no", "OPTIONS", "-T", snap]).stdout.strip()
    print(f"dataset {snap}\n  mount: {opts}")
    if "compress" not in opts:
        print("  NOTE: no compress= on this mount; check whether there is anything to do")

    hits = scan(snap)
    if not hits:
        die(f"no PLE n-gram shard tensors found under {snap}")
    ple = sum(h[2] for h in hits)
    tot = sum(h[3] for h in hits)
    print(f"  {len(hits)} blob(s) contain PLE shards: {ple/2**30:.2f} GiB of PLE "
          f"inside {tot/2**30:.2f} GiB of files")
    print(f"  (other tensors in those files are rewritten byte-for-byte; btrfs "
          f"cannot decompress a byte range of a shared file)")
    already = 0
    plan = []
    comp_total = 0
    for entry, blob, p, t, spans in hits:
        (comp_bytes, in_range), _ = ple_encoded_extents(blob, spans)
        if comp_bytes is None:
            state, do = "extent map unavailable (need filefrag)", True
        elif comp_bytes == 0:
            state, do = "PLE bytes already plain", False
        elif comp_bytes < a.min_compressed_mib * 2**20:
            state, do = (f"{comp_bytes/2**20:.1f} MiB compressed in {in_range} PLE "
                         f"extents, below --min-compressed-mib "
                         f"{a.min_compressed_mib}: skipping"), False
        else:
            state, do = (f"{comp_bytes/2**30:.2f} GiB compressed in "
                         f"{in_range} PLE extents"), True
        print(f"    {entry[:32]:32s} ple={p/2**30:6.2f} GiB file={t/2**30:6.2f} GiB "
              f"[{state}]")
        comp_total += comp_bytes or 0
        if do:
            plan.append(blob)
    free = os.statvfs(snap).f_bavail * os.statvfs(snap).f_frsize
    print(f"  PLE table stored compressed: {comp_total/2**30:.2f} GiB of "
          f"{ple/2**30:.2f} GiB ({100*comp_total/ple:.1f}%)")
    if not plan:
        print("nothing to do: every PLE blob is already uncompressed")
        return
    need = max(h[3] for h in hits) + (64 << 20)
    if free < need and not a.dry_run:
        die(f"needs {need/2**30:.1f} GiB free for the largest single rewrite; "
            f"{free/2**30:.1f} GiB available")
    print(f"  peak extra space during rewrite: {need/2**30:.1f} GiB "
          f"({free/2**30:.1f} GiB free)")
    for blob in plan:
        rewrite(blob, a.dry_run)
    if not a.dry_run:
        print("done.  Check: filefrag -v <blob> | grep -c encoded   (expect 0)")
        print("NOTE: restart any server using this profile; pages already in the "
              "page cache belong to the old inodes and are not reused.")


main()

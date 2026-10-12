# Deferred plan: filesystem matrix for the streaming PLE read path

**Status: deferred. Nothing here blocks stage B.** The read-path decisions are already settled
by `benchmarks/gg-stageB-readpath-20261012/` and `docs/ssd-stream-findings.md`. This exists
because one measurement produced a real finding, and following it to an answer needs disk we
do not currently have free. Write time: 2026-10-12.

## 1. Why this exists

On `gg`, `O_DIRECT` is **not honest** against the FP8 checkpoint as it is currently stored:

| evidence | value |
|---|---|
| extents flagged `encoded` in the 8.1 GiB blob | **4,010 of 4,048** |
| `lsattr` on that blob | no user-visible compression flag (zstd gave up on FP8 bytes) |
| device sectors per O_DIRECT request | **9.83** (4,915 bytes for a 4,096-byte read) |
| device requests vs issued requests | 1.06 (almost no merging, so it is size inflation, not queueing) |
| fs device amplification, all 12 O_DIRECT cells | **1.163-1.165x**, invariant to threads and memory cap |
| page cache grown by O_DIRECT runs | 494-497 MiB, ~7.9% of requests |
| page cache grown by page-cache runs | 3,993-4,655 MiB, 73.7% of requests |

`filefrag` shows the file living in encoded extents, which is btrfs' representation for
compressed inodes -- including inodes whose data turned out incompressible. O_DIRECT over
encoded extents goes through the buffered path. So the mode we added to get a clean,
predictable, cache-free control is neither clean nor cache-free on the machine that serves
traffic.

**Headline metric for everything below: `device_sectors_per_request`.** 8.00 means the device
was asked for exactly what we needed. Above that is tax. It is unit-valued and it is not
confusable with throughput, which is why it is the primary column and not pages/s.

## 2. Predictions to falsify, not a survey

Write these before running. A cell that matches a prediction confirms the model; a cell that
doesn't is the interesting result.

| # | prediction | if false, then |
|---|---|---|
| P1 | btrfs `nodatacow,compress=no` + `chattr +C` gives 8.00 sectors/request, amplification 1.00x, O_DIRECT page-cache growth ~0 | the fallback is not extent-flag-driven; it is btrfs general, or my attribution is wrong |
| P2 | xfs and ext4 give 8.00 sectors/request on the same fixture | the ~1.2x is generic page-cache-adjacent readahead, not btrfs |
| P3 | btrfs `compress=zstd:1` with a *compressible* fixture shows the largest amplification and the biggest cache growth, and `du` < apparent size | compression is not the trigger and the encoded-extent story is incomplete |
| P4 | raw-device fio gives the highest IOPS of all, but **only after the region has been written and settled** | unwritten-LBA reads are being answered without media, so raw is not a floor and must be pre-written before any use |
| P5 | pages/s ranking (pcache > odirect) persists across filesystems, while the *gap* shrinks where O_DIRECT becomes truly direct | the gap was an artifact of one filesystem's fallback, and the access-method recommendation weakens |

P1 is the one that changes deployment advice. Everything else is context.

## 3. Step 0 needs no carving, and is the highest-value cell

Copy one FP8 blob (8.1 GiB) into a NOCOW directory **on `gg`'s own btrfs** and re-run two
cells. This tests the fix on the machine that matters, before any of the below.

```bash
# on gg, as rtx
mkdir -p ~/nocow-test/table && chattr +C ~/nocow-test/table
cp /home/rtx/.cache/huggingface/hub/blobs/23/23224da1*.blob-ish-file ~/nocow-test/table/blob
# (or: cp --reflink=never, and verify below; reflink within a NOCOW dir should not CoW)
filefrag -v ~/nocow-test/table/blob | grep -c encoded     # want 0
lsattr ~/nocow-test/table/blob                            # want the 'C' flag
du --apparent-size -h ~/nocow-test/table/blob; du -h ~/nocow-test/table/blob   # want equal

cd ~/sm12x-exl3-qwen3.8-flash-next
BLOB=$HOME/nocow-test/table/blob THREADS="8 12" CAPS= STEPS=48 TOKS=2048 \
  TARGET=132734 DEV=nvme0n1 MWS=0 IMAGE=localhost/qwen38-exactpin:local \
  CONTAINER=qwen38-exl3-ple8 bash scripts/run-stageB-readmatrix.sh
```

`CAPS=` empty skips the capped cells. If step 0 satisfies P1, the deployment advice becomes
"store the FP8 table in a `chattr +C`, `compress=no` directory" and the rest of this plan is
supporting detail rather than a prerequisite.

Note the practical wrinkle: `chattr +C` affects **newly created** files only, so an existing
HF cache must be re-laid-out or the download must run with the target directory already
flagged. Any doc text must say which.

## 4. Carving `/nvme1` (root, on `burst`)

Facts from 2026-10-12 so nobody re-derives them: `/dev/nvme1n1` is a Samsung PM9A1 2 TB
(`MZVL22T0HBLB`), single partition `nvme1n1p1`, **already XFS**, mounted `/nvme1`, 141 GiB
used and it is all benchmark fixtures (`/nvme1/bench`, 101 GiB). `mkfs.xfs`, `mkfs.ext4`,
`mkfs.btrfs`, `fio`, `chattr` all present. SELinux is **Permissive**. The host has ~1 TB RAM,
so uncapped page-cache cells on an 8 GiB region are cache-hit theatrics -- always use caps.

```bash
# 0. look first
findmnt /nvme1; du -xsh /nvme1/*

# 1. tear down. THIS ENDS THE DEFERRED SLC-DECAY TEST at the umount, which is the agreed
#    trade: that test is documentation-only and it is redone below as the settle phase.
sudo umount /nvme1
sudo wipefs -a /dev/nvme1n1p1
sudo sgdisk --zap-all /dev/nvme1n1
sudo partprobe /dev/nvme1n1

# 2. five slices of ~337 GiB. Sized above the PM9A1's ~236 GB SLC region deliberately:
#    fixtures smaller than the SLC cache cannot produce honest cold-read numbers, and that
#    mistake has already cost this project one retracted claim.
sudo sgdisk -n1:0:19% -n2:19%:38% -n3:38%:57% -n4:57%:76% -n5:76%:95% /dev/nvme1n1
sudo partprobe /dev/nvme1n1; sleep 2
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS /dev/nvme1n1

# 3. filesystems (p5 deliberately left unformatted for the raw baseline)
sudo mkfs.xfs   -f -K -L fsb-xfs /dev/nvme1n1p1
sudo mkfs.ext4  -q -L fsb-ext4 -E lazy_itable_init=0,lazy_journal_init=0 /dev/nvme1n1p2
sudo mkfs.btrfs -f -q -L fsb-bnc /dev/nvme1n1p3
sudo mkfs.btrfs -f -q -L fsb-bc  /dev/nvme1n1p4

# 4. mounts: identical options except the axis under test
sudo mkdir -p /fsb/xfs /fsb/ext4 /fsb/bnc /fsb/bc
sudo mount -o noatime,inode64                               /dev/nvme1n1p1 /fsb/xfs
sudo mount -o noatime                                       /dev/nvme1n1p2 /fsb/ext4
sudo mount -o noatime,compress=no,nodatacow,discard=async   /dev/nvme1n1p3 /fsb/bnc
sudo mount -o noatime,compress=zstd:1,discard=async         /dev/nvme1n1p4 /fsb/bc

# 5. NOCOW must be inherited from the directory BEFORE any file is created
sudo mkdir -p /fsb/bnc/table && sudo chattr +C /fsb/bnc/table
sudo chown -R rtx:rtx /fsb
sudo setfacl -m u:rtx:rw /dev/nvme1n1p5     # for fio raw; do not chmod 666 a block device
```

Not added to `/etc/fstab` on purpose. If it should survive reboots, use UUIDs from `blkid`.

## 5. Fixtures, and verifying them before believing them

Per volume: one **300 GiB** file. `bnc`, `xfs`, `ext4` get incompressible bytes; `bc` gets a
compressible filler so P3 has something to bite on. Write with `dd`/`fio`, not `cp` from
sparse, and fsync.

```bash
mkfifo /tmp/gen 2>/dev/null
# incompressible: os.urandom streams at roughly 1-2 GB/s, faster than /dev/urandom pipelines
python3 -c 'import os,sys
f=open("/fsb/xfs/table","wb")
while f.tell() < 300*2**30: f.write(os.urandom(1<<20))
f.flush(); os.fsync(f.fileno())'
# compressible filler for the bc volume
python3 -c 'f=open("/fsb/bc/table","wb")
blk=b"qwen38"*65536
while f.tell() < 300*2**30: f.write(blk)
f.flush(); os.fsync(f.fileno())'
```

**Gates. A cell whose fixture fails a gate is not a data point.** Run these and record the
output alongside the receipts:

```bash
for v in xfs ext4 bnc bc; do
  echo "== $v"
  filefrag -v /fsb/$v/table 2>/dev/null | grep -c encoded       # bnc/xfs/ext4 want 0
  lsattr /fsb/$v/table | awk '{print "    flags:", $1}'         # bnc wants C
  sync
  printf "    apparent %s   on-disk %s\n" \
    "$(du --apparent-size -h /fsb/$v/table | cut -f1)" "$(du -h /fsb/$v/table | cut -f1)"
done
```

Expect: `bc` on-disk far below apparent (compression active), the other three equal (P3's
control), `bnc` with zero encoded extents and the `C` flag (P1's precondition). `chattr +C`
failing to inherit is silent and would turn the nocow cell into a lie -- which is the exact
class of broken-but-plausible experiment this project has repeatedly produced, so the check is
mandatory, not advisory.

## 6. Settle phase, which is where the deferred decay experiment lives

After writing, the SLC cache is full of the fixture and reads are not steady state. Let the
volumes sit, sampling qd1 and qd8 randread IOPS hourly so the fold curve is recorded instead
of assumed:

```bash
for h in $(seq 0 12); do
  printf "%s  " "$(date -Is)"
  fio --name=qd1 --filename=/fsb/xfs/table --rw=randread --bs=4k --iodepth=1 --numjobs=1 \
      --ioengine=libaio --direct=1 --runtime=30 --time_based --group_reporting \
      --output-format=json | python3 -c 'import json,sys; d=json.load(sys.stdin)["jobs"][0]; print(f"qd1 {d[\"read\"][\"iops\"]:.0f}")'
  sleep 3300
done
```

This is the SLC-fold question, asked per filesystem, on freshly written 300 GiB fixtures --
strictly more useful than the run it replaces, which was on one XFS volume with 100 GiB of
data. Read the matrix cells **only after the curve is flat**, and record which hour it
flattened: that number is what the docs warning ("do not benchmark a just-copied checkpoint")
should quote.

## 7. Cells

```bash
cd ~/sm12x-exl3-qwen3.8-flash-next
for v in xfs ext4 bnc bc; do
  BLOB=/fsb/$v/table THREADS="4 8 12 16" CAPS="8 32" STEPS=48 TOKS=2048 \
    TARGET=132734 DEV=nvme1n1 MWS=0 IMAGE=<any python3 image, or run host-side> \
    bash scripts/run-stageB-readmatrix.sh
done
```

burst has ~1 TB RAM, so caps must be **larger** than on gg to be meaningful at 300 GiB:
8 GiB (cache holds 2.7% of the fixture) and 32 GiB (10.7%). Add an uncapped run only to
confirm it is meaningless -- it is the control that shows why caps exist.

Useful extras the gg matrix lacked:

- **sequential** and **2 MiB-stride** passes, because real n-gram row ids may cluster more
  than uniform sampling assumes.
- a **Zipf-skewed** id stream (`--hot-fraction 0.05 --hot-share 0.8`) to bracket the hot-set
  hypothesis. Still the one measurement that could overturn the C/N default: if real n-gram
  access is skewed, page cache is worth far more than C/N predicts.
- **fio raw** on `/dev/nvme1n1p5` for the no-filesystem floor. Pre-write it first (P4):
  `fio --name=w --filename=/dev/nvme1n1p5 --rw=randwrite --bs=4k ...` then settle, or reads of
  never-written LBAs may not touch the media at all.

Per-cell honesty gates, all already enforced by `bench-ple-readpath.py`: `--evict-first
--require-cold` (abort rather than report cache as device), `--baseline-seconds` (background
traffic above 5% of the measured rate invalidates the cell), and the two flags that exist
because the counters lied before -- `!fs-amplification` and `!direct-populated-cache`.

## 8. Known limits of the current evidence

- One drive model (990 PRO on gg, PM9A1 on burst) and one host for the matrix that matters.
- `--tensor` targeting is unusable on gg's checkpoint: the n-gram table is **128 shards of
  0.37253 GiB across all 22 blobs**, so every cell reads a whole blob. Multi-shard fd handling
  is untested and stage B needs it.
- The gg matrix repeats 30% of pages by self-overlap against 15.5% for a real 261,632-token
  prefill and 2.1% for a 32,768-token one, so its uncapped hit rate is an **upper bound** on
  self-overlap benefit. Fixtures 300 GiB+ against a 47.684 GiB requirement largely fix this.
- 495 MiB of O_DIRECT cache growth from 99% encoded extents is a 7.9% resident fraction with
  no confirmed explanation. Candidates: btrfs frees the temporary pages encoded reads land in,
  or only some extent boundaries fall back. Resolving it is a `filefrag`-against-hit-offsets
  exercise, not a new filesystem.

## 9. What this changes, and what it cannot

It can change: the recommended storage layout for the PLE table (`chattr +C` + `compress=no`
is a concrete, cheap instruction); whether `O_DIRECT` is worth shipping as an option at all;
and the wording of the benchmarking warnings in the streaming docs.

It cannot change: the thread-count decision, the CPU budget (~2.7 cores at 182k pages/s), the
requirement figures (128k-170k pages/s), or the conclusion that stage B's sizing case is
prefill rather than decode. Those are settled by the receipts already in the repo.

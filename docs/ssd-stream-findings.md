# P2 pre-work findings (streaming PLE from NVMe)

Analysis only. Nothing in the P2 plan has been executed. These are measurements
taken on `burst` and `gg` on 2026-10-10/11 while re-deriving the assumptions in
`.opencode/plan/2026-10-11-ssd-ple-streaming.md`. Three of them are wrong, and
one of the three removes most of P2's stated motivation.

Reproduce any of it with the snippets in the appendix; every number below is a
measurement on this hardware, not a citation.

---

## 1. The pinned-memory ceiling is PyTorch power-of-two rounding, not a kernel limit

**This is the load-bearing finding.** The plan's section 5 records a ceiling it
could not explain: "64 GiB single pinned allocation succeeds, 70 GiB fails",
with cgroups, `RLIMIT_MEMLOCK` and `CommitLimit` all eliminated and the
mechanism left "unidentified". `platform-config.sh:150-229` then encodes an
empirical 55%-of-installed-RAM refusal bracket derived from that bracketing.

The mechanism is in `ATen/core/CachingHostAllocator.h`:

```cpp
// Round up the allocation to the nearest power of two to improve reuse.
// These power of two sizes are also used to index into the free list.
const auto roundSize = [&]() -> size_t {
  if (size <= pinned_max_round_threshold() &&
      size <= pinned_max_cached_size()) {
    return c10::llvm::PowerOf2Ceil(size);
  } else {
    return size;
  }
}();
```

Both thresholds default to `std::numeric_limits<size_t>::max()`
(`c10/core/AllocatorConfig.h:361,363`), so the rounding always applies.
Measured on torch 2.13.0+cu130 (the v0.31.0 image):

| requested (pinned) | actually mapped |
| --- | --- |
| 3 MiB | 4 MiB |
| 5 MiB | 8 MiB |
| 9 MiB | 16 MiB |
| 300 MiB | 512 MiB |
| 1.5 GiB | 2 GiB |
| 5 GiB | 8 GiB |

Worst case approaches 2x; the table sizes land at 1.34x.

### It reproduces every unexplained number in the plan

| claim | plan's figure | next_pow2 explains it |
| --- | --- | --- |
| gg FP8 host commitment | "system Shmem 66.18 GiB against 48.87 GiB derived" | 47.684 -> 64 GiB, + 1.184 -> 2 GiB = **66.0** |
| burst live worker | `RssShmem` 130.28 GiB | 95.367 -> **128 GiB**, + 2 GiB = **130.0** |
| "64 GiB pins, 70 GiB fails" on a 125.68 GiB host | unexplained | 64 -> 64 fits; 70 -> **128 GiB**, larger than RAM |
| "1.35x the derived figure" in the memcheck warning | empirical fudge | exactly `128/95.367 = 1.342` |

Direct evidence from the live `exl3` worker on burst (`/proc/143540/smaps`):

```
128.000 GiB  rss=128.000 GiB  pow2=True  /dev/zero (deleted)   <- PLE table, needs 95.367
  2.000 GiB  rss=  2.000 GiB  pow2=True  /dev/zero (deleted)   <- token embed, needs 1.184
```

So `exl3` (BF16) does not fail on `gg` because 95.4 GiB of pinned memory is too
much for a 125.68 GiB host. It fails because 95.367 GiB rounds to 128 GiB,
which is larger than the machine. The 55% bracket in `qwen38_ple_memcheck()` is
a coincidence of that rounding boundary, and it will mis-refuse on any host
whose RAM is not near a power of two.

### The workaround is verified and needs no engine change

`cudaHostAlloc` bypasses the caching host allocator and does not round:

```
cudaHostAlloc(  5.000 GiB) -> maps   5.008 GiB   EXACT   (pin_memory:  8.00 GiB)
cudaHostAlloc( 47.684 GiB) -> maps  47.692 GiB   EXACT   (pin_memory: 64.00 GiB)
cudaHostAlloc( 95.367 GiB) -> maps  95.375 GiB   EXACT   (pin_memory:128.00 GiB)
```

Power-of-two tiling inside torch works too, because the free list is indexed by
pow2 sizes, so a tile that is already a power of two is not grown:

```
FP8 table as ONE 47.684 GiB    committed 64.008 GiB   waste 34.2%
FP8 table TILED  32+15.684     committed 48.000 GiB   waste  0.7%
BF16 table as ONE 95.367 GiB   committed 128.000 GiB  waste 34.2%
BF16 table TILED  64+31.367    committed  96.00 GiB   waste ~0.7%
```

No `PYTORCH_CUDA_ALLOC_CONF` key controls this in torch 2.13: the parser accepts
only `backend`, `expandable_segments`, `garbage_collection_threshold` and
`roundup_power2_divisions` (the last one is for the device allocator; measured
to have no effect on pinning).

### Consequence for P2

`Qwen4ExpPLEPinnedHostEmbedding.allocate_embedding_weight()`
(`vllm/models/qwen4_exp/nvidia/ple.py:462`) is the single call site. Routing it
through `cudaHostAlloc` (or tiling) saves 32.6 GiB on FP8 and 32.6 GiB on BF16,
and it means:

* `exl3` BF16 needs 95.4 GiB, not 128 GiB. On a 125.68 GiB host that is a
  different problem entirely, and it is worth re-measuring before designing an
  I/O path around it.
* The plan's P2 value item "reclaiming ~48 GiB for KV cache on hosts that can
  run ple8" is largely available for free: ple8 commits 64 GiB where it needs
  47.7.
* The `qwen38_ple_memcheck()` 55% bracket should be replaced by
  `next_pow2(table) + next_pow2(embed)` versus installed RAM, which is the
  quantity the allocator actually requests.

Caveat: unverified on `gg`, which is `exl3-ple8`-serving right now and cannot be
disturbed. The prediction is that a `cudaHostAlloc` of 47.684 GiB succeeds there
where `pin_memory` of the same size is at the edge. Confirm on `burst` first.

---

## 2. Page-cache eviction: the plan's "unsolved without root" is solvable without root

Plan section 6 says a `posix_fadvise(POSIX_FADV_DONTNEED)` attempt "did not
evict", that this produced the fake 67k/83k IOPS numbers, and that fixing it
needs root for `drop_caches`. Neither host gives sudo. That conclusion is wrong
in both halves.

### fadvise works unprivileged

On a 2 GiB file on `burst`'s btrfs, as `opencode`, no root and no `fsync`:

```
after read:            RES = 304M
after fadvise DONTNEED: RES = 0B        rc=0
```

`vmtouch -e` on a 512 MiB file evicted all 131072 pages in 0.000042 s.

### Why it silently does nothing, which is what produced the fake numbers

`fadvise(DONTNEED)` returns 0 whether or not it evicted anything. It calls
`mapping_try_invalidate()`, and the predicate is `mapping_evict_folio()`
(`mm/truncate.c`):

```c
if (folio_test_dirty(folio) || folio_test_writeback(folio))
        return 0;
/* The refcount will be elevated if any page in the folio is mapped */
if (folio_ref_count(folio) >
                folio_nr_pages(folio) + folio_has_private(folio) + 1)
        return 0;
```

So it skips **dirty** pages and **mapped** pages. Both were reproduced:

* dirty: write a file, do not `sync`, `fadvise` -> resident stays nonzero;
  after `sync` -> 0. The plan's "freshly written 4 GiB file" is exactly this.
* mapped: fault all 65536 pages of a 256 MiB file through a live `mmap`, then
  `fadvise` from a second fd -> `mincore` still reports 65536/65536, `fincore`
  still 256M, `rc=0`. After `munmap`, the same call drops it to 0.

### And root would not have fixed that case

`/proc/sys/vm/drop_caches` reaches the same predicate:
`drop_pagecache_sb()` -> `invalidate_mapping_pages()` ->
`mapping_try_invalidate()` -> `mapping_evict_folio()`
(`fs/drop_caches.c:41`). Its own kernel-doc says it "removes pages that are
clean, unmapped and unlocked". Granting sudo for `drop_caches` therefore buys
nothing for a table a live engine has `mmap`'d, which is precisely the P2 case.
`invalidate_inode_pages2_range()` does unmap first, but no syscall reaches it
on an arbitrary file.

### What does work unprivileged, including on a live mapping

From inside the process that owns the mapping: `madvise(MADV_DONTNEED)` on the
range, then `fadvise(DONTNEED)` on the fd. Measured on a fully-faulted 512 MiB
mapping:

```
mapped+faulted              : mincore 131072/131072   fincore 502.9M
after MADV_DONTNEED          : mincore 131072/131072   fincore 502.9M   (PTEs dropped, cache kept)
then fadvise DONTNEED        : mincore        0/131072   fincore      0B
```

`process_madvise(2)` on another same-uid process returns EINVAL here
(`errno=22`, kernel 7.2.8, `ptrace_scope=0`), so the in-process route is the
usable one; the engine has to do it to itself.

### Verification is also unprivileged

`mincore(2)` on your own mapping, and `fincore`, both work as `opencode`. The
plan's protocol requirement "never infer cache state from throughput alone;
verify residency explicitly" is satisfiable today.

### The likelier cause of the void numbers

`/tmp` is a 498 GiB tmpfs on `burst`, including `/tmp/opencode`, which the
environment guidance recommends for scratch. A "4 GiB test file" written there
is shmem: fully resident, not page cache, and unfreeable by any amount of
`fadvise`. Any I/O measurement against it measures RAM. Benchmark files must
live on the btrfs dataset (`/home/...`), which is also where the checkpoint
actually is.

So: no sudo grant is warranted. It would not unblock the mapped case, and the
unmapped case needs nothing.

---

## 3. Stage A's control is invalid on this filesystem: btrfs compression

Plan section 4 says `fio --direct=1` "bypasses the page cache by construction,
which removes the whole class of error made during this session". On `burst`
and `gg` that is false for the checkpoint files specifically.

Both hosts mount the checkpoint dataset as btrfs with `compress=zstd:1`:

```
burst  /dev/nvme0n1p3  rw,relatime,seclabel,compress=zstd:1,ssd,discard=async,...
gg     /dev/nvme0n1p3  rw,relatime,seclabel,compress=zstd:1,ssd,discard=async,...
```

The real PLE n-gram tensors do compress. Measured zlib ratio on
`...ngram_embedding.shard_0.weight`, dtype BF16, shape `[2500012, 160]`:
**0.7844**. `filefrag` on the live blob reports 2238 of 2265 extents flagged
`encoded`, i.e. stored compressed.

### Compressed extents make O_DIRECT fall back to buffered I/O

`fs/btrfs/direct-io.c`, in `btrfs_dio_iomap_begin()`:

```c
/*
 * Ok for INLINE and COMPRESSED extents we need to fallback on buffered
 * io. ... For COMPRESSED we _have_ to read the entire extent in so we can
 * decompress it, so there will be buffering required no matter what we
 * do, so go ahead and fallback to buffered.
 *
 * We return -ENOTBLK because that's what makes DIO go ahead and go back
 * to buffered IO.
 */
if (btrfs_extent_map_is_compressed(em) || em->disk_bytenr == EXTENT_MAP_INLINE) {
```

Compressed extents are 32 blocks = 128 KiB (`filefrag` on the shard), so one
4 KiB demand read pulls the whole extent.

### Measured, on identical bytes differing only in compression

512 MiB sliced out of the real BF16 n-gram tensor, written twice to the same
dataset: once normally (`encoded` extents), once after `chattr +C`:

| | `--direct=1`, qd1, 4 KiB random, cold |
| --- | --- |
| compressed (`encoded` extents) | page cache 0 -> **2112 pages**; device read **26.0x** the logical bytes |
| `chattr +C` (no compression) | page cache 0 -> 4 pages; device read **1.00x** |

The `--direct=1` run on the compressed copy also reported *higher* throughput
than the direct run on the uncompressed copy, because it was partly serving
from the cache it had just polluted. That is the same failure signature the
plan recorded as "67k and 83k IOPS at queue depth 1, with 32 workers slower
than 1 worker".

### Alignment is a second, independent trap

With a page-aligned buffer, `O_DIRECT` on btrfs is genuinely direct (4 pages
resident, 1.0x amplification). Move the same buffer 512 bytes and the call
still returns full length with no error, while resident pages go 0 -> 320:

```
ALIGNED    : 32/32 ok  pages 0 ->   4   genuinely direct
MISALIGNED : 32/32 ok  pages 0 -> 320   silently buffered
```

`check_direct_IO()` in btrfs compares buffer alignment against
`fs_info->sectorsize`, so 4 KiB is the requirement on this filesystem, and a
prototype using `preadv` into a pinned staging slab must assert alignment or it
will quietly benchmark the page cache. Note this is the opposite of an error:
silent degradation, no return code.

### What this changes in the staged plan

* Stage A must point at a non-compressed dataset, or at files carrying
  `chattr +C`, or it measures zstd throughput and cache, not the device.
* Stage B's design input changes: if the streamed table stays on a compressed
  btrfs dataset, the row->page amplification in plan section 3 (12.8x BF16,
  25.6x FP8) is really ~26x on the wire, per read, plus a decompress. Turning
  compression off for the table is a bigger single win than anything in the
  I/O path so far.
* Whatever filesystem the streaming prototype reads from should be the one the
  shipping image reads from; results from a clean XFS scratch device do not
  transfer to this `compress=zstd:1` btrfs dataset without a matching arm.

### Honest device numbers, uncompressed, cold, `burst` (HP SSD FX900 Pro 4TB)

```
qd1    19.3k IOPS    clat  46.1 us
qd8   148.0k IOPS    clat  49.0 us
qd32  164.0k IOPS    clat 189.8 us
qd128 164.0k IOPS    clat 776.3 us      (saturated at ~164k)
```

The plan's arithmetic target of ~38k page-reads/s at `max_num_seqs=16` needs
qd>=4 and is comfortably under the qd8 knee, so the plan's conclusion holds for
the right reason: bandwidth and IOPS are not the constraint. Serialization,
cache state and read amplification are.

---

## 4. The mmap path is not *blocked* on discrete RTX; it is *slow* there

Plan section 3 states the architectural constraint as: a discrete RTX PRO 6000
reports `PAGEABLE_MEMORY_ACCESS_USES_HOST_PAGE_TABLES=0`, "so mmap PLE is
disabled on RTX", and calls this "the architectural constraint that decides the
design", concluding that the gather must move off the GPU.

The attribute readings are confirmed on both hosts (driver 615.71.09):

```
NVIDIA RTX PRO 6000 Blackwell Workstation Edition
    CU_DEVICE_ATTRIBUTE_PAGEABLE_MEMORY_ACCESS                        = 1
    CU_DEVICE_ATTRIBUTE_PAGEABLE_MEMORY_ACCESS_USES_HOST_PAGE_TABLES  = 0
    CU_DEVICE_ATTRIBUTE_CONCURRENT_MANAGED_ACCESS                     = 1
    CU_DEVICE_ATTRIBUTE_SYSTEM_MEMORY_INTEG_UNIFIED_ADDRESSING        = 1
```

But the attribute being 0 does not make the dereference illegal; it makes it
expensive. `require_pageable_access()` treats 0 as unsupported, which is a
correctness-and-performance *policy*, not a hardware *barrier*. Measured here,
using the same indirection the vendored `_gather_mapped_rows_kernel` uses (host
virtual addresses held in a device int64 tensor, cast to a pointer in-kernel):

* the kernel **does** read a read-only file-backed mapping on this card, and the
  bytes are **identical to `pread`** on the same offsets (6/6 sampled pages);
* so the plan's sentence "the CUDA driver rejects them at startup" describes the
  repo's guard firing, not the driver refusing the access.

### A cold-read measurement that needs no cache eviction at all

Worth keeping as protocol: instead of faulting a page in and trying to evict it,
read file offsets that have **never been touched**, one per trial, through a
fresh mapping. Cold by construction, nothing to flush, no root, and it works
while another engine is serving.

| measurement | value |
| --- | --- |
| single never-faulted host page, GPU kernel read | p50 **55.3 us**, p90 72.6 us, max 85.8 us (n=40) |
| same page afterwards (resident) | **11.6 us** per read |
| gather of 256 rows x 320 B, one per page, mapping resident | 12.4 us per row |

That last row is the one that decides the design. ~12 us per 320-byte row is
~25 MB/s effective: a decode step needs 16 rows x 48 PLE-bearing layers, and
each access is an uncoalesced PCIe round trip against pageable host memory.
This is the same wall upstream reports in PR #58439 (cold 30K-token prefill:
**88 s with GPU faults alone vs 1.6 s with 64 CPU threads faulting first**).

So the plan's conclusion -- stream it on the CPU, do not let the GPU fault the
table -- is **right**, but the stated reason is wrong, and the wrong reason
matters: it implies an implementer must build a CPU gather because the GPU
*cannot* touch the mapping. It can. The reason to build the CPU gather is
per-access PCIe latency, which is a number you can measure before committing to
the design.

### Not established, and confounded

A 1024-row cold gather failed with `CUDA out of memory`. That box had only
0.77 GiB of free VRAM because the live `exl3` engine holds 92 of 97.9 GiB, so
this is **not** evidence about UVM page-table limits; it is an artifact of
co-tenancy. Re-measure on an idle GPU before drawing anything from it. Nothing
in this section was measured on `gg`.

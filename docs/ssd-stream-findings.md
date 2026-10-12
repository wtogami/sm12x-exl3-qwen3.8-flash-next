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
mechanism left "unidentified". `platform-config.sh` then encoded an empirical
55%-of-installed-RAM refusal bracket derived from that bracketing; superseded on
prod by `memcheck: gate on the pinned size PyTorch actually asks for`, below.

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

The failure found in practice runs the other way, which is worse: the bracket
**permits configs that cannot start**. A 65 GiB table is 52.7% of a 125.68 GiB
host, inside the bracket, but 65 GiB is 1 GiB past a power of two, so it pins
128 GiB and the request is 130 GiB against 125.68 installed. A gate that refuses
a runnable config is annoying; one that green-lights an unrunnable config has
the gate's whole purpose backwards. Both the rounding model and this case are
now tested in `scripts/test-ple-memcheck.py`.

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
  quantity the allocator actually requests. **Done on prod**, refusing where
  demand lands within `max(8 GiB, 5% of RAM)` of installed and warning above 70%,
  the latter chosen because 130/183 GiB = 71.0% is the highest share with a
  recorded successful pin. Every verdict the pre-existing suite asserted still
  holds, so the change was not fitted to its own tests.

Caveat: unverified on `gg`, which is `exl3-ple8`-serving right now and cannot be
disturbed. The prediction is that a `cudaHostAlloc` of 47.684 GiB succeeds there
where `pin_memory` of the same size is at the edge. Confirm on `burst` first.

---

## 1b. Both fixes were tested on gg, and the cheap one works

The allocation probe ran on `gg` itself (125.68 GiB, the host that cannot fit the
monolithic pin), with its serving engine stopped and restored afterwards -- Shmem
returned to its exact pre-test 66.18 GiB and the worker again mapped 64.000 + 2.000
GiB.

* **`cudaHostAlloc(95.368 GiB)` succeeded in 42 s**, mapping `pow2=False`, i.e. the
  exact table size with no rounding. `torch.empty(pin_memory=True)` of the same
  tensor failed *instantly* with the documented `CUDA error: out of memory`. That is
  the whole thesis of section 1 demonstrated constructively: the table was never too
  big for `gg`, only the 128 GiB it rounds to was.
* **Pow2 tiling works too and needs no allocator bypass.** Decomposing the request
  into binary-sized `torch` allocations committed 95.38 GiB instead of 128.00 -- the
  same 32.6 GiB saving -- using only `pin_memory`, so it is a change to
  `allocate_embedding_weight()` alone, with no raw pointer lifetime to own. It is
  the cheaper patch and lands the same result. Each tile rounds to itself, so the
  waste collapses from 32.6 GiB to the sum of per-tile round-ups.
* **Both together fit**: 97.38 GiB pinned (table + rounded embedding), 22.76 GiB
  still available, `swap_used` unchanged at 0.04 GiB throughout, so no silent
  swapping flattered the result.

Two costs worth knowing before anyone celebrates:

* **Page-locking 95 GiB takes 42 s**, and the engine already spends ~40 s starting.
  Roughly doubles cold start, and it is on the critical path before the first token.
* **22.76 GiB of headroom is thin.** The host that demonstrably *served* this shape
  had 53 GiB free (130 of 183). Nothing has served a request at 77.5% of RAM, and
  the gate's 70% warn tier is deliberately left where it is for that reason -- see
  `memcheck: separate what has served from what merely pinned`.

Measurement correction: an earlier single-shot copy test reported the new pinned
memory as no faster than pageable (13.4 vs 13.4 GiB/s), which would have doomed the
idea. It was a broken harness -- one cold copy, no warmup, sampling the last 256 MiB.
With warmup and repeats: **26.4 GiB/s pinned vs 13.5 GiB/s pageable, 1.96x**, and
flat across every offset of the 95 GiB buffer (25.8 GiB/s at the very tail), on a
single NUMA node (`node0`, 126 GiB) so there is no far-node penalty for a gather
that reads the whole table. The cold first copy measured 12.1 GiB/s, which is exactly
the phantom 13.4 -- first-touch, not a property of the memory.

Consequence for the plan: streaming is no longer the only route to `exl3` BF16 on
`gg`. A tiling change to one function costs 32.6 GiB of waste and 42 s of startup;
streaming costs an I/O subsystem and buys a 100x smaller RAM footprint. Which is
right depends on whether the goal is "serve BF16 on this host" or "free the RAM" --
see the re-staging note in section 5.

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

### Turning it off per-file, without root

| method | works? | measured |
| --- | --- | --- |
| `chattr +C` on the **file**, before writing | yes | ratio 1.0000, `encoded=0` |
| `chattr +C` on a **directory** | yes, inherited by new files | 128 MiB of compressible data inside: `encoded=0`; same data outside: `encoded=1024` |
| `chattr -c` on the file | **no** | still compresses: ratio 0.375, `encoded=4096` |
| `btrfs property set -ts <file> compression ""` | **no** | `ERROR: object is not compatible with property: compression` -- the property is subvolume-scoped |
| `btrfs property set` on a subvolume | needs root, and `opencode` cannot create subvolumes here | `Operation not supported` |

So the unprivileged deployment shape is a dedicated directory with `chattr +C`
holding the table files, and they must be **rewritten** -- `+C` and `-c` only
affect future writes, never existing extents. Note `+C` also sets nodatacow,
which removes COW as a variable too; for a read-once streaming table that is the
desirable direction, but it is a second change, not just a compression change.

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

---

## 5. Prior art, checked against the sources (corrections to plan section 3)

The plan's table is broadly right. These are the places where the primary
sources say something more specific, or something different.

| item | status as of this check | the part that matters here |
| --- | --- | --- |
| [vLLM #54129](https://github.com/vllm-project/vllm/pull/54129) | open, 28 Aug 2026 | CPU gather from shard mappings + H2D, piecewise CUDA graphs split around the PLE op, optional `VLLM_PLE_MMAP_PREWARM`. Numbers are **DGX Spark only** (25.1 tok/s warm C1, ~18 tok/s first cold request). No discrete-RTX benchmark. Do not relabel as sm_120. |
| [vLLM #54070](https://github.com/vllm-project/vllm/pull/54070) | open **draft**, "Depends on #53899" | The RTX PRO 6000 matrix the plan quotes. Author caveat: boots were still partly cached. Strongest evidence in the whole survey that **cache state and the cgroup limit are part of the benchmark spec**, not incidental. |
| [vLLM #53908](https://github.com/vllm-project/vllm/issues/53908) | open issue, no PR | "The goal is not to include the auxiliary GPU in TP or EP"; only the n-gram table on the second device. |
| [vLLM #58439](https://github.com/vllm-project/vllm/pull/58439) + [#58835](https://github.com/vllm-project/vllm/pull/58835) | both open | This is what the repo vendors. #58835 is the `MADV_WILLNEED` + `MADV_POPULATE_READ` fill: serial touch 4.6-4.7 ms -> 0.36 ms. Independent commenter's port onto v0.30: 62.5 -> 93.1 tok/s on coding-agent sessions, cold 50K prefill 1,144 -> 1,896 tok/s. #58439 itself calls #54129 "the portable path ... which works on discrete GPUs". |
| [SGLang #36567](https://github.com/sgl-project/sglang/pull/36567) | open **draft** | `SGLANG_QWEN4_PLE_NVME_PATH`; Rust persistent io_uring, page-aligned storage, bounded submit batches, GIL-free reads; pinned staging; overlap with the preceding decoder layer. TP1 + FP8 E4M3 only. Its own caveat: "An exact end-to-end rerun on this Rust implementation remains before the draft is marked ready for review", so the 14.39/24.77 tok/s figures are **predecessor-reader** qualification, not the submitted reader. |
| [sglang-ssd-stream](https://github.com/garnermccloud/sglang-ssd-stream) + [checkpoint](https://huggingface.co/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream) | third-party | 51,200,245,760-byte read-only sidecar holding 128 FP8 shards; **32 MiB registered page pool + two 16 MiB pinned staging**; dedupes the exact 4 KiB pages per step; converts on a side CUDA stream. RTX PRO 6000: RAM 148.5-156.2, SSD 164.7, but random-row-heavy requests **126-137**, with the explicit disclaimer "The 164.7 tok/s run is not a claim that SSD is faster than RAM." |
| [llama.cpp #28136](https://github.com/ggml-org/llama.cpp/pull/28136) | open | Author: "In an ideal world, maybe this would even be handled by something more elegant like `io_uring`. But, this works." The Linux table the plan quotes is RX 9070 XT/Vulkan, 23,664-token prompt, **cold eviction verified with mincore**: mmap demand-fault 18.01 s (207,913 major faults) vs 1 worker 15.75 s vs 32 workers **2.39 s**; warm, the two are within noise (154.6 vs 154.0 ms). Windows regression is real but warm-cache-specific: "2-4% slower than the mmap path, not faster", ~27 GiB table staying warm in NTFS cache. |

### Two things the survey adds that the plan does not have

**a) An O_DIRECT alignment requirement coarser than 4 KiB is possible.**
`open(2)` says the restrictions on buffer address, length and file offset "vary
by filesystem", and misaligned operations "may either fail with EINVAL or fall
back to buffered I/O" -- exactly the silent degradation measured in section 3.
`statx(STATX_DIOALIGN)` reports the real per-filesystem requirement. A prototype
should query it, not hardcode 4 KiB. A 160-byte FP8 row is not a valid direct-I/O
unit under any of these rules; the read unit is always the encompassing page.

**b) GPUDirect Storage is probably not available on this card.**
NVIDIA's GDS release notes: "Data Center and Quadro (desktop) cards with compute
capability > 6 listed here are supported in GDS mode. All other cards are
supported only in compatibility mode."
([release notes](https://docs.nvidia.com/gpudirect-storage/release-notes/index.html))
Compatibility mode is a bounce through a pinned host buffer, i.e. what the
streaming design already does by hand. So "use cuFile" is not a shortcut here;
it must be verified as native GDS or not claimed at all.

### Academic context, and where it is being over-read

* **Engram** ([arXiv 2601.07372](https://arxiv.org/html/2601.07372v1)) is the
  relevant model family. Collision handling: "we employ K distinct hash heads for
  each N-gram order n". Its offload claim -- "offloading a 100B-parameter table
  entirely to host memory incurs negligible throughput overhead (< 3%)", peak
  2.8% -- is **host DRAM over PCIe, not NVMe**. It should not be cited as
  evidence that NVMe works.
* **CXL pooling of Engram memory**
  ([arXiv 2603.10087](https://arxiv.org/html/2603.10087)) independently confirms
  the access shape this plan assumed: ~5 KB per token per layer as "8 hash-mapped
  320 bytes data segments, distributed in discrete, sparse addresses". That is
  the origin of the 12.8-rows-per-page amplification. It is CXL, not disk.
* **LongCat-Flash-Lite** ([arXiv 2601.21204](https://arxiv.org/html/2601.21204)):
  sub-table decomposition with different vocabulary sizes reduces collisions;
  "hash collisions force a single embedding vector to superimpose the semantics
  of multiple distinct n-grams".
* **SCONE** ([NeurIPS 2025](https://proceedings.neurips.cc/paper_files/paper/2025/hash/2e067924aeeb02ae9919803fd08d8b4b-Abstract-Conference.html))
  stores precomputed n-gram embeddings in "off-accelerator memory" -- which is
  not a measured NVMe result.
* **Row packing vs collisions.** Nothing in the survey establishes a layout that
  makes the 16 hashed rows of one token land in one physical page. Collision
  mitigation improves representation, not locality. Dedup only saves requests
  when addresses genuinely share a page, so the amplification figure stands as
  designed, not as measured-then-removed.

### The table layout is prime-separated, which settles pages-per-token exactly

`_make_vocab_layout` (`vllm/models/qwen4_exp/nvidia/ngram_embedding.py`) sizes each
n-gram head with `_nth_prime_after(ngram_vocab_size_base - 1, global_head + 1)`, and
each head's block starts where the previous one ended. For this model
(`ngram_vocab_size_base = 20,000,000`, `ngram_heads = (ngram_size - 1) *
heads_per_ngram = 16`) the sizes are the 16 successive primes 20,000,003 ..
20,000,171, summing to 320,001,446, which `split_ngram_parts = 128` rounds up to
320,001,536 = 128 x 2,500,012. That reproduces the checkpoint exactly: 128 shards
contiguous 0..127, every one `[2500012, 160]`, and `ceil(320,001,446 / 128) =
2,500,012` is the observed rows-per-shard. The padding rule was inferred from the
arithmetic and the `make_ngram_vocab_size_divisible_by: 128` config key rather than
read from code; the 90 dead rows are never addressed, so nothing downstream depends
on that inference.

Two consequences, both better than an estimate:

* **The 16 rows of one token can never share a page, and the reason is now proven
  rather than asserted.** Prime block sizes put every head start at an unrelated
  residue mod 4096 -- measured 0, 960, 128, 2496, 1152, 3648 for the first six heads
  in BF16. The bullet immediately above says nothing in the survey establishes a
  layout that co-locates them; the layout itself forbids it, and padding each block
  to a page multiple would change the prime arithmetic and the checkpoint bytes to
  buy 3.0% of the pages for 1.6x the disk, which section 6 already measured and
  rejected.
* **Pages per token is 17.25, not the 16 this write-up had been using.** A row
  touches one page unless it straddles. For a single contiguous, page-aligned table
  the straddle rate is exact rather than random: starts sit at `row * ROW_BYTES`, so
  mod 4096 they occupy only `4096 / gcd(ROW_BYTES, 4096)` residues, and for BF16
  (320 B, `gcd = 64`) exactly 4 of 64 residues straddle -- 6.25%, giving
  `16 x 1.0625 = 17.0000`. FP8 (160 B, `gcd = 32`) gives 4 of 128, 3.125%, 16.5000.
  A 200,000-token Monte Carlo on that contiguous model reproduces both figures
  exactly.
* **But the shipping checkpoint is not one contiguous table, and that costs another
  1.5%.** It is 128 independent tensors, each carrying its own byte phase, so the
  clean modular argument does not apply. Measured on the real file offsets: 24
  distinct phases mod 4096 across the 128 shards and **zero** page-aligned (first
  phases 96, 104, 232, 608, 616, 1120 -- header lengths and preceding tensors shift
  every one). A 200,000-token Monte Carlo over those actual offsets gives
  **17.2519** pages/token for BF16 and **16.5333** for FP8, versus the 17.0000 and
  16.5000 the closed form predicts. Both numbers are reported because both are
  correct for their own model: 17.0000 is a property of the prime layout, 17.2519 is
  what a reader of the files as shipped pays. Staging the table into a page-aligned
  copy would recover only 1.5% of the pages, so the phase tax is not worth a second
  copy of 95 GiB.

A 2048-token chunk therefore touches about 35,300 pages, and covering that inside a
3.6 s compute budget needs about 9,800 pages/s.

The `12.8 rows per page` figure quoted from arXiv 2603.10087 is the *within-head*
density (`4096 / 320`), not a divisor for pages-per-token. The two must not be
conflated: 16 rows drawn from 16 separate blocks cost 17.25 pages however dense
each block is.

A row also never crosses a shard *file*, structurally rather than rarely:
`discover_table_layout` partitions rows, not bytes -- shard i owns rows
`[i * S, min((i+1) * S, num_rows))` -- so a streaming resolver needs no cross-file
logic at all.

The plan's section 8 question 5 -- "SSD Stream used only 10 GB RAM resident,
plausible?" -- is answered by its own repo: a 32 MiB page pool plus two 16 MiB
staging buffers replaces a 47.68 GiB table. ~10 GiB of residue is ordinary
engine and CUDA context, so yes, plausible.

---

## 6. What this does to the plan's staging

### P2's headline motivation is now mostly a 30-line allocation fix

Plan section 1 frames the problem as "95.40 GiB BF16 / 47.68 GiB FP8 of pinned
host RAM", and section 2 lists P2's remaining value as enabling BF16 on hosts
under ~100 GiB and reclaiming ~48 GiB on hosts that run ple8. Both were measured
against an allocator that asks for 128 GiB and 64 GiB respectively. Before
designing an NVMe engine, measure the two cheap replacements:

1. `cudaHostAlloc` at the exact size (verified exact at 5, 47.684 and 95.367 GiB), or
2. pow2 tiling inside torch (verified: FP8 table 64.008 -> 48.000 GiB).

Both remove ~32.6 GiB per table with no I/O path, no CUDA graph surgery, no
splitting custom op, and no new toolchain.

### The order of investigations should change

| plan stage | status after this pass |
| --- | --- |
| Stage A: fio at qd1/8/32 | done in section 3; needs re-running on gg, and on a non-compressed dataset |
| Stage B: CPU parallel `pread` prototype | **still the right bet**, but one cheaper test comes first (below) |
| Stage C: io_uring | still correctly deferred; llama.cpp's own author calls io_uring "the elegant version, not the necessary one" |
| new step 0 | fix the pinned allocation size, then re-ask whether P2 is still wanted |

### The cheap test that should precede Stage B

The whole Stage B design exists to hide per-access latency behind the preceding
decoder layer. That hiding can be demonstrated without writing any engine code:
issue the ~38k 4 KiB reads/second that a 16-sequence decode step needs, from N
parallel `pread` workers into an aligned pinned buffer, on a `chattr +C` copy of
the table, and compare elapsed time against the ~3.6 s of compute the plan says
a 2048-token chunk affords. If N workers cannot beat that with the table
completely cold, nothing downstream rescues the design; if they beat it by 5x,
the remaining risk is entirely in CUDA graph splitting and dequant placement.

Stage B's remaining hard parts, unchanged and still unmeasured, are exactly the
plan's section 8 question 4: interaction with MTP=3 speculative tokens and a
`max_num_seqs=16` full CUDA graph capture, and where dequant happens once the
gather leaves the GPU.

### Benchmark protocol changes forced by sections 2 and 3

1. Scratch files live on the btrfs dataset, never under `/tmp` (498 GiB tmpfs on
   `burst`; tmpfs is shmem and cannot be evicted at all).
2. `--direct=1` is not a valid control on compressed extents. Either `chattr +C`
   the test file and rewrite it, or query `statx(STATX_DIOALIGN)` and verify.
3. `O_DIRECT` buffers must be page-aligned, and alignment must be *asserted*,
   because a 512-byte miss is silent and returns full length.
4. Prove cold state with `mincore`/`fincore`, or construct coldness by reading
   never-touched offsets. Do not infer it from throughput.
5. Never call `fadvise(DONTNEED)` against a file the engine has mapped and
   believe the rc=0; it skips mapped pages, and so does root's `drop_caches`.
6. On `burst`, `podman --memory=Ng` does enforce (`memory.max` observed inside
   the container at the requested value), so the plan's requirement to cap the
   container is satisfiable rootless. Writing `memory.reclaim` or `memory.high`
   is not (not writable by `opencode`, host or in-container).

---

## 7. RedHatAI's C16 plateau is not a storage effect

Prompted by this profile holding the most compressed PLE bytes of anything that
actually serves on `burst` (69.11 GiB of its 95.37 GiB table inside compressed
extents): could btrfs zstd be why `nvfp4-redhatai` alone stops scaling past C8,
594.5 tok/s at C16 against EXL3's 855.7? No. Five measurements, three of them
independent, kill it.

| control | measured | consequence |
| --- | --- | --- |
| serving path | `platform-config.sh:31` sets `DEFAULT_PLE_MMAP=0` on x86_64; the three RTX finals holding a runtime capture (`exl3-final`, `exl3-ple8-final`, `nvfp4-final`) all show `VLLM_PLE_CPU_OFFLOAD=1` and a `PleOffloadWorker` | the table lives in pinned host RAM; decode issues no filesystem reads |
| storage traffic | live worker `read_bytes: 36864` against `RssShmem: 136,613,268 kB` | 36 KiB of block I/O for a whole session, with a 95 GiB table in play |
| residency | `fincore` on the RHA PLE blob: RES 95.4G / SIZE 95.4G, 25,000,126 pages; `Cached: 692 GiB` of 995 GiB | even a mapped path would find every page warm |
| table identity | shard_0 of exl3 and of RHA are both `[2500012, 160] BF16`; first and last 32 MiB sha256 equal (`85514f34…`, `178df174…`) | the PLE axis is *controlled* between the two builds, not merely similar |
| compression direction | exl3 80.47/95.37 GiB compressed (84.4%) vs RHA 69.11/95.37 (72.5%) | the better-scaling build is the more compressed one |

### The identity result is new to this repo

EXL3 and RedHatAI carry byte-identical n-gram tables, which nothing here
recorded before. Their blobs share **zero** filenames (checked pairwise across
all four repos: exl3 vs RHA 0, RHA vs nvidia 0; only exl3 vs exl3-ple8 share,
9 blobs / 62.2 GiB of identical expert tensors). The files differ, not the
data: EXL3 spreads its 128 shard tensors across 14 files, RedHatAI keeps all
95.37 GiB in a single `model-00001-of-00005.safetensors`, so the same bytes sit
at absolute offset 5,613,256,936 in one and 20,768 in the other. Different
offsets give different extent boundaries, which is exactly why identical logical
data reads as 84.4% versus 72.5% compressed. This is also the evidence that
`performance-comparison.md`'s "same table as RHA" note was literally true, and
the reason the section 3 compression finding cannot be the C16 differentiator.

### What the compression finding is actually for

Not for these numbers. It bears on:

* cold load, where ~19% fewer device bytes trades against decompressing the
  whole table — roughly a wash on a sequential read, and unmeasured here;
* the **mapped** path: the Spark default, and any future streaming engine, where
  a random 4 KiB gather against a compressed extent pays the full ~26x;
* nothing on the pinned RTX serve path.

### A repo comment that is wrong, deliberately left unedited

`platform-config.sh:28-31` and `start.sh:28-34` both justify `DEFAULT_PLE_MMAP=0`
by asserting the CUDA driver rejects the checkpoint mapping on discrete cards.
Section 4 showed that is false, and three RTX legs are direct
counter-evidence — `exl3-ple8-mmap-final`, `resident-v031-mmap` and
`rtx-structured-final` all ran `VLLM_PLE_MMAP=1` on this platform and started;
the last one's startup log reads "PLE mmap: readahead pre-pass active (posix_fadvise WILLNEED), 14/14 shard
files opened" and "layer 1 attached, 128 shards, 320001536 rows x 320 B
(95.37 GiB on disk), 32 workers". The default is still right — section 4 measures
~55 us per cold page — but the stated reason is the false premise P2 was parked
on. Left untouched only because that file needs its own approval; the fix is to
keep the default and correct the comment, not to flip the gate.

### Two archive defects this turned up

1. `benchmark-decode.py:151` and `benchmark-prefill.py:135` hardcoded a
   `--profile` enum missing `nvfp4-redhatai` while `model-profiles.sh` knows four
   quants. `benchmark-suite.sh:27,30` passes `--profile "$QUANT"`, so
   `QUANT=nvfp4-redhatai` died in argparse before issuing a request, and the
   workaround stamped RedHatAI receipts `quant_profile: "nvfp4"` — the NVIDIA
   profile, whose `PLE_BYTES_PER_ELEMENT` is 1, i.e. an FP8 table claim about a
   BF16 checkpoint. The field is write-only across the repo, so widening the enum
   cannot move a measurement: done in `530b4cc`. Historical receipts still carry
   the wrong label and cannot be corrected without breaking provenance.
2. No RedHatAI `runtime.json` exists anywhere in the archive, and the four-way
   and C16-repeat directories hold none for any build, so the PLE mode of the very
   blocks analysed here was never captured. That is not a formality: three RTX
   legs *did* run the mapping, so the mode genuinely varies on this platform.
   Fixed since, and the mechanism was not an env read: the mode is not an
   environment variable on the current recipe at all. `start.sh:49-56` passes
   `--engram-config '{"cpu_offload": true, "checkpoint_mapped": true|false}'`, so
   the authoritative record is the container's argv. That is also why
   `runtime.json` stopped carrying it: `0066e6a` deleted the
   `VLLM_PLE_CPU_OFFLOAD` allowlist entry and the
   `or item.startswith("VLLM_PLE_MMAP")` clause without replacing them with an
   argv read, and nothing in the launcher sets those variables any more.
   `scripts/server_state.py` now reads argv in one place; the profilers embed a
   `server` block, whose `claims_disagree` flags a receipt that contradicts its
   own endpoint; and `benchmark-suite.sh` writes `runtime.json` per leg.

## Appendix: one-liners for each measurement

All run as `opencode`, no root. `CKPT` = any checkpoint blob under the HF `blobs/` dir.

```bash
# 0. confirm the trap: is your scratch on tmpfs? (burst: yes, 498 GiB)
stat -f -c '%T %n' /tmp /tmp/opencode /home/opencode
findmnt -no OPTIONS -T /home/opencode          # expect compress=zstd:1 on both hosts

# 1. is the table actually compressed on disk?  (encoded extents == yes)
filefrag -v "$CKPT" | awk 'NR>3{for(i=1;i<=NF;i++) if($i ~ /encoded/) c++} END{print c+0}'
python3 -c "
import json,struct,zlib,os
f=open(os.path.realpath('$CKPT'),'rb'); n=struct.unpack('<Q',f.read(8))[0]
h=json.loads(f.read(n)); k=max((k for k in h if 'ngram_embedding' in k),
    key=lambda k:h[k]['data_offsets'][1]-h[k]['data_offsets'][0])
s,e=h[k]['data_offsets']; f.seek(8+n+s); d=f.read(1<<20)
print(k, h[k]['shape'], h[k]['dtype'], 'zlib', round(len(zlib.compress(d,1))/len(d),4))"

# 2. per-file compression control:  +C works, -c does NOT (verified)
: > x.bin && chattr +C x.bin && <write x.bin> && filefrag -v x.bin | grep -c encoded   # 0
: > y.bin && chattr -c y.bin && <write y.bin> && filefrag -v y.bin | grep -c encoded   # nonzero

# 3. page-cache: evict, verify, and the mapped-page failure mode
vmtouch -e file && fincore file                       # works, unprivileged
python3 -c "import os,ctypes,subprocess
libc=ctypes.CDLL(None); libc.mmap.restype=ctypes.c_void_p
libc.mmap.argtypes=[ctypes.c_void_p]*2+[ctypes.c_int]*2+[ctypes.c_int,ctypes.c_long]
libc.mincore.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.POINTER(ctypes.c_ubyte)]
libc.pread.argtypes=[ctypes.c_int,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_long]
libc.pread.restype=ctypes.c_ssize_t
F='file'; N=os.path.getsize(F); PG=4096
fd=os.open(F,os.O_RDONLY); a=libc.mmap(None,N,1,1,fd,0); b=(ctypes.c_ubyte*N).from_address(a)
for i in range(0,N,PG): b[i]                          # fault every page
v=(ctypes.c_ubyte*(N//PG))(); libc.mincore(ctypes.c_void_p(a),ctypes.c_size_t(N),v)
print('resident',sum(v),'/',N//PG)
libc.posix_fadvise(fd,0,0,4); libc.mincore(ctypes.c_void_p(a),ctypes.c_size_t(N),v)
print('after fadvise',sum(v),'/',N//PG,' <- unchanged: mapped pages are skipped, rc was 0')
libc.madvise(ctypes.c_void_p(a),ctypes.c_size_t(N),4)  # MADV_DONTNEED
libc.posix_fadvise(fd,0,0,4); libc.mincore(ctypes.c_void_p(a),ctypes.c_size_t(N),v)
print('after madvise+fadvise',sum(v),'/',N//PG,' <- now cold, still no root')"

# 4. O_DIRECT amplification, aligned vs misaligned
python3 -c "see section 3; use a page-aligned buffer from an anonymous mmap,
then the same buffer +512 to watch it silently become buffered I/O"
fio --name=t --filename=f --rw=randread --bs=4k --iodepth=1 --numjobs=1 \
    --direct=1 --ioengine=psync --runtime=8 --time_based --group_reporting

# 5. pinned-allocation rounding (the section 1 finding)
#    verified output: 8.008 GiB for the pin, then exactly +5.000 for cudaHostAlloc
python3 -c "
import re, torch, ctypes
def sm():
    t=0
    for l in open('/proc/self/smaps'):
        m=re.match(r\'^([0-9a-f]+)-([0-9a-f]+) \\S{4}\\s+\\S+\\s+\\S+\\s+\\S*\\s*(.*)$\', l)
        if m and \'deleted\' in m.group(3): t += int(m.group(2),16)-int(m.group(1),16)
    return t
G=2**30; b=sm()
t=torch.empty(int(5*G/2), dtype=torch.bfloat16, device=\'cpu\', pin_memory=True)
print(\'pin_memory    5 GiB -> mapped %.3f GiB\' % ((sm()-b)/G))
p=ctypes.c_void_p()
ctypes.CDLL(\'libcudart.so\').cudaHostAlloc(ctypes.byref(p), ctypes.c_size_t(int(5*G)), 1)
print(\'after cudaHostAlloc 5 GiB, cumulative %.3f GiB (i.e. +5.000)\' % ((sm()-b)/G))"

# 6. the ceiling, arithmetic only -- why 64 pins and 70 does not on a 125.68 GiB host
python3 -c "
from math import log2
for gib in (47.684, 70, 95.367): print(gib, '->', 2**round(log2(gib)+0.5001))"
```

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

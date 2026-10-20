# P2 pre-work findings (streaming PLE from NVMe)

Analysis only. Nothing in the P2 plan has been executed. These are measurements
taken on `burst` and `gg` on 2026-10-10/11 while re-deriving the assumptions in
`.opencode/plan/2026-10-11-ssd-ple-streaming.md`. Three of them are wrong, and
one of the three removes most of P2's stated motivation.

## Contents

**Pre-work and read-path studies**
- [P2 pre-work findings (streaming PLE from NVMe)](#p2-pre-work-findings-streaming-ple-from-nvme)
- [Stage B works: the FP8 table served by CPU row copy with 62 GiB less pinned memory](#stage-b-works-the-fp8-table-served-by-cpu-row-copy-with-62-gib-less-pinned-memory)

**Design, first matrix, and the regime decomposition**
- [The plan already told me not to do this, and I did it anyway](#the-plan-already-told-me-not-to-do-this-and-i-did-it-anyway)
- [The matrix that booted nothing, and the five defects it surfaced first](#the-matrix-that-booted-nothing-and-the-five-defects-it-surfaced-first)
- [The 2×2 result: v1 costs 22% of decode, and most of it is the graphs, not the streaming](#the-22-result-v1-costs-22-of-decode-and-most-of-it-is-the-graphs-not-the-streaming)

**v2: the staged design, what it fixed, what it broke**
- [v2 as built: staging in `prepare_inputs`, and why that place in particular](#v2-as-built-staging-in-prepareinputs-and-why-that-place-in-particular)
- [v2 works: 2.21 GiB of Shmem, FULL graphs intact, and two of my explanations were wrong](#v2-works-221-gib-of-shmem-full-graphs-intact-and-two-of-my-explanations-were-wrong)
- [Async staging failed twice over, and the second failure was the dangerous one](#async-staging-failed-twice-over-and-the-second-failure-was-the-dangerous-one)
- [Where v2's cost actually is: the CPU is waiting for the GPU, and the gather is slower than the prototype said](#where-v2s-cost-actually-is-the-cpu-is-waiting-for-the-gpu-and-the-gather-is-slower-than-the-prototype-said)

**The v0.32 rebase and TP power matching**
- [Rebase onto vLLM v0.32: v1 was not broken -- the graph regime it forces changes output](#rebase-onto-vllm-v032-v1-was-not-broken----the-graph-regime-it-forces-changes-output)
- [TP2 on this host: throughput, power envelope, and what changes in the output](#tp2-on-this-host-throughput-power-envelope-and-what-changes-in-the-output)
- [Why streaming is now permitted at TP2, and where it still is not](#why-streaming-is-now-permitted-at-tp2-and-where-it-still-is-not)

**Cost anatomy and the gather fix**
- [v2 phase split on burst, and what the gather cost actually is](#v2-phase-split-on-burst-and-what-the-gather-cost-actually-is)
- [Removing the per-group overhead: 2.49 ms -> 0.056 ms for a decode-size batch](#removing-the-per-group-overhead-249-ms---0056-ms-for-a-decode-size-batch)
- [In-engine effect of the single-call gather: decode loss 7.8% -> 3.4%, output still byte-identical](#in-engine-effect-of-the-single-call-gather-decode-loss-78---34-output-still-byte-identical)
- [What the remaining `ids` cost is made of](#what-the-remaining-ids-cost-is-made-of)

**Small hosts: the measured cache ladder**
- [Streaming on genuinely small hosts: measured cache budget on gg](#streaming-on-genuinely-small-hosts-measured-cache-budget-on-gg)
- [gg cache ladder re-run under one recorded power envelope (supersedes the tables above)](#gg-cache-ladder-re-run-under-one-recorded-power-envelope-supersedes-the-tables-above)

**Pin strategy: BF16 qualification, then the default**
- [BF16 profile qualified: `exact` returns ~33 GiB at both TP widths, output unchanged](#bf16-profile-qualified-exact-returns-33-gib-at-both-tp-widths-output-unchanged)
- [`PLE_PIN_STRATEGY=exact` is the launcher default](#plepinstrategyexact-is-the-launcher-default)

**The operator surface: one knob**
- [Streaming gets one knob: `PLE_STREAM=1` is the shipping design; `piecewise` is the diagnostic](#streaming-gets-one-knob-plestream1-is-the-shipping-design-piecewise-is-the-diagnostic)

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
  the cheaper patch *in this specific sense* -- no raw pointer lifetime to own -- and
  lands the same RAM result. Whether it is cheaper *overall* is still open: the
  exact-size route in 1c, if it holds at full scale, needs no change to the
  gather kernel at all, which tiling does. Each tile rounds to itself, so the
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

## 1c. The exact-size test reported a failure that was not the design's, and killed the engine doing it

Section 1b left one design open: skip tiling entirely by handing
`get_cuda_view_from_cpu_tensor` a single **exact-size** externally-pinned buffer
(95.368 GiB for BF16), dodging the pow2 rounding with one allocation instead of
two. The first full-scale attempt appeared to refute it. **That refutation was a
probe artifact.** The run is recorded here anyway, because both the artifact and
the outage are traps for the next person.

What the first attempt printed:

```
cudaHostAlloc(95.368 GiB)                 rc=0   42.0 s  (2.27 GiB/s)  page_aligned=True
torch.frombuffer(...).view(bfloat16)      is_pinned() = False
get_cuda_view_from_cpu_tensor             RuntimeError cuda_view.cu:53: cudaHostAlloc failed: out of memory
cudaHostRegister, sweep 1/8/16 GiB        rc=0 at every size, is_pinned() = False at every size
```

Two separate claims are hiding in that output, and only one of them is true. The
true one: the op took its fallback branch, and the fallback branch really does
double the reservation -- the source pins this down, the pinned path calls
`cudaHostGetDevicePointer` on the existing buffer while the non-pinned path
`cudaHostAlloc`s a second one and `cudaMemcpy`s into it. 190 GiB of demand
instead of the 128 GiB the rounding cost, so the failure reading was right.

The false one: `is_pinned() = False` as a statement about the buffer. That
process imported torch and vllm, allocated, registered, and called the op. It
never touched CUDA. So `is_pinned` was querying an uninitialized runtime, and
the op made the same query through the same door. The controlled version, 8 GiB,
one buffer, identical registration, only context state differing:

```
cudaHostRegister rc=0  (no CUDA work yet in this process)
  state BEFORE context init:  torch.cuda.is_initialized=False  driver=PinnedHost(rc=0)  is_pinned=False
  state AFTER  context init:  torch.cuda.is_initialized=True   driver=PinnedHost(rc=0)  is_pinned=True
```

The driver calls the pointer pinned host memory in **both** rows, with a
succeeding `rc`. Only torch's answer moves. And the op is on exactly that path:
it does not query the driver itself, it dispatches `aten::is_pinned` through the
torch stable ABI with a `std::nullopt` device. An uninitialized context sends it
down the copy branch for any buffer, however genuinely pinned.

This reconciles the two measurements I had left flagged as contradictory. The
512 MiB probe reported `is_pinned()=True` and aliased because it had already run
device-side gathers, i.e. it had a context; the 1/8/16 GiB sweep had not. Never
a size effect. Two further artifacts from that broken run, so nobody re-reads its
output as data:

* every `driver says Unregistered` line is void -- it used `cuPointerGetAttributes`,
  which returned `rc=1`, so the call failed and read nothing. The singular
  `cuPointerGetAttribute` with `CU_POINTER_ATTRIBUTE_MEMORY_TYPE` returns
  `rc=0 PinnedHost` on those same pointers;
* the full-scale case never checked whether `torch.frombuffer` preserved the
  buffer address. It does: `data_ptr == alloc_ptr` in every case of the corrected
  run.

With a context established first, all three buffer flavors alias at 8 GiB, and
each is verified by something other than pointer arithmetic:

```
A  torch pin_memory=True (control)   is_pinned=True  same_addr=True  MemAvailable delta +0.00  sentinel=True
B  torch.empty + cudaHostRegister    is_pinned=True  same_addr=True  MemAvailable delta +0.00  sentinel=True
C  cudaHostAlloc + frombuffer        is_pinned=True  same_addr=True  MemAvailable delta +0.00  sentinel=True
```

The delta column is the load-bearing one and it was simply missing before: had
the op copied, host residency would have risen by the buffer size regardless of
what any pointer said. The sentinel (write through the CPU tensor, read back
through the CUDA view) is a second confirmation that does not depend on
accounting either.

### It holds at full scale: one allocation, no rounding, no copy

The redo, engine stopped, cgroup capped at 104 GiB, CUDA context established
first, on `gg` (121.68 GiB available, `Shmem 0.00` asserted before starting):

```
cudaHostAlloc(95.368 GiB)        rc=0 in 63.8 s (1.49 GiB/s)   page_aligned=True
ptr identity                    : data_ptr == alloc_ptr   True
driver memory type              : PinnedHost (rc=0)
is_pinned                       : True
view                            : same_addr=True in 0.00 s   MemAvailable delta -0.00 GiB
sentinel (CPU write -> GPU read): True
gather 200k rows x 160 bf16     : 4.04 ms = 49.54 M rows/s
```

So the exact-size design **works**: one allocation, 95.368 GiB committed instead of
128.000, aliased into the device with no copy. The pow2 rounding -- the entire
32.6 GiB -- is not a constraint of the allocator, it is a constraint of
`pin_memory`, and the fix is one allocation rather than a tiling scheme.

Two things to keep honest about that output. `63.8 s` against the `42.0 s` the
same call took an hour earlier means page-locking the table costs 42-64 s with
1.5x run-to-run spread, so any startup estimate built on one sample is soft. And
the gather line is *not* a GPU-compute number: the view is aliased host memory, so
every one of those 200,000 rows is a random 320-byte PCIe read, which works out to
15.8 GB/s effective. That is the path this host already uses for PLE today, and
the decode requirement (993 tok/s x 16 rows = 15,888 rows/s) is roughly 3,100x
below the 49.54 M rows/s measured here, so host-resident gathering is not the
bottleneck at either profile.

### What this decides, and the one thing it costs

RAM-wise the two candidate fixes are now nearly identical: exact-size commits
95.368 GiB, two-tile pow2 commits 96.000 (64+32), so tiling wastes 0.632 GiB more
-- negligible. The difference is entirely structural:

* **exact-size** needs no change to the gather kernel, but it takes the
  allocation outside torch, so something has to own the `cudaHostAlloc` pointer;
* **tiling** is torch-native but needs the kernel to resolve a row against
  multiple base pointers.

### The lifetime question is resolved, and one route to it is a trap

The pointer-lifetime cost turns out to be avoidable, but the obvious way to pay it
fails silently, so both halves are worth writing down.

**Works.** Expose the `cudaHostAlloc` pointer through an *owning* object that is a
plain buffer-protocol object -- a Python subclass of a ctypes array type, which
gets a `__dict__` and so can carry the pointer and free it in `__del__`:

```
tensor made; is_pinned=True
after del(owner) + gc:   no free  -> owner STILL ALIVE, torch anchored it
read through the GPU view: VALID, sentinel intact
after dropping tensor+view: cudaFree fired from OwnArr.__del__
```

`torch.frombuffer` keeps the exporter alive, and the alias path's
`from_blob(..., [base = cpu_tensor](void*) {})` deleter keeps the CPU tensor alive.
Those two chains compose: the pinned buffer is released exactly when the last GPU
view is released, which is the correct lifetime, with no registry and no manual
teardown. And since the serving launch carries no `--enable-sleep-mode`, weights
are never released at runtime anyway, so even a mis-designed owner could only
leak, not dangle.

**Silently broken -- and the evidence for that took two attempts to get right.** The
modern-looking equivalent, PEP 688 `__buffer__` / `__release_buffer__` on the owner,
does not work: torch releases the export *inside* `frombuffer`, so the owner dies
while the tensor still hands out its address.

The first version of this section claimed the proof was a `CUDA error: invalid
argument` on first read. **That was wrong, and it was the same red herring described
below** -- the probe's `__release_buffer__` called `cudaFree`, which frees nothing, so
the error was sticky state rather than a use-after-free. Measured again with the
correct deallocator, `cudaFreeHost`:

```
MemAvailable before alloc :  48.56 GiB
tensor still in scope     :  48.59 GiB      (delta +0.03 -> the 4 GiB is NOT held)
events                    :  alloc 0x7faec0000000, cudaFreeHost rc=0
is_pinned while alive     :  False          <- the real observable
next cudaHostAlloc(4 GiB) :  addr 0x7faec0000000   <- the SAME address, re-handed
first tensor now reads    :  0xab 0xab 0xab 0xab   <- another owner's bytes
```

So the failure is not a fault, it is **silent corruption**: the buffer is returned to
the driver while a tensor still references it, reads keep working until the allocator
re-hands that address, and then the tensor quietly returns someone else's data. The
earlier clean read (`sentinel=True`) was unreused pages, i.e. luck. `is_pinned()`
flipping to False is what actually distinguishes released from held.

**A sticky error is the most misleading thing CUDA has done in this investigation.**
Calling `cudaFree()` on `cudaHostAlloc` memory returns `cudaErrorInvalidValue` and
frees nothing (`cudaFreeHost` is the deallocator). The missed free is bad enough, but
the second-order effect cost more debugging time: the failed call leaves an error
status that the *next* torch CUDA operation reports as `CUDA error: invalid argument`,
even on a buffer that was never touched. Demonstrated directly -- a bogus `cudaFree`
followed by an alias read of a separate, valid buffer fails, and the same sequence
passes once `cudaFreeHost` is used. So "invalid argument" in this area is evidence
about the last failed call, not about the memory being read; check the deallocator
before believing any corruption story, including one I wrote earlier in this file.
`_release()` therefore uses `cudaFreeHost`, checks the return code, and clears the
error status on failure so a bad teardown cannot impersonate a bug elsewhere.

**A methodology note on the test that got this wrong first.** The preceding run
deleted the ctypes array and gc'd, saw the read still succeed, and concluded torch
did not anchor -- but the array came from `from_address`, which is a *non-owning*
view, and nothing in that script ever called `cudaFree`. The memory could not have
gone away, so the test could not have failed, and its verdict was meaningless in
both directions. Any lifetime test needs an owner whose destructor is observable;
otherwise it is measuring nothing. The same mistake recurred immediately after this
was written: a `MemAvailable` check labelled "memory still held" when the delta of
`+0.03 GiB` meant the opposite, because the comparison had its sign backwards. A
measurement whose interpretation is an `if` over two similar-looking numbers needs to
print the numbers and the conclusion, and the conclusion above was corrected by the
address-reuse observation rather than by that check.

Practical consequence for a loader patch: allocate with `cudaHostAlloc`, wrap with
a ctypes-array subclass that owns the pointer, hand it to `torch.frombuffer`, then
`get_cuda_view_from_cpu_tensor` -- and initialize the CUDA context before any
`is_pinned` call, per the rule below.

**The rule this buys:** any probe that calls `is_pinned`,
`cudaPointerGetAttributes`, or a vLLM op that dispatches them, must initialize
CUDA first (`torch.zeros(1, device="cuda")`). A `False` from `is_pinned` on a
pointer the driver accepts is a statement about the process, not the memory.

### Why the engine died: `cudaHostRegister` and `cudaHostAlloc` are not equally safe

The 21:38 CDT outage on `gg` (container `Exited (137)`) was caused by the first,
unguarded version of this probe, not by the engine. Kernel evidence:

```
[167139.9] python3: page allocation stall for 10 secs: order:0, mode:GFP_HIGHUSER|__GFP_ZERO|__GFP_COMP
         PID 104923 Comm:python3 UID:1002          <- the probe; the engine was PID 103172/103174
[167216.4] <allocation failure dump>               = 21:38:17 CDT (boot + timestamp)
          shmem 70,459,496 kB = 67.2 GiB           = the engine, still alive at that instant
          inactive_anon 119,668,516 - shmem = 49.2 GiB  = the probe's 95 GiB, ~half faulted in
          free 192,207 pages = 751 MiB,  Free swap 343,740 kB of 8 GiB
[     --  ] engine container died 21:38:36, exit 137
```

The operational lesson is an asymmetry between the two CUDA APIs that no guard of
mine was pointing at:

* **`cudaHostRegister` is the dangerous one.** It takes an existing lazily-mmap'd
  region and converts reservation into resident pages, faulting them in
  progressively with no up-front accounting against `MemAvailable`. It ran for
  77 seconds and climbed 49 GiB before anything refused. `torch.empty` returning
  in 0.0 s is what makes this invisible: the cost arrives during registration.
* **`cudaHostAlloc` refuses cleanly** (rc=1002 / a Python exception) before
  committing, which is why the second 95 GiB attempt produced an error rather than
  an OOM storm -- swap usage afterwards was 0.07 GiB.

My guards were on the allocation and not on the registration.

And the OOM killer's choice is the part to design around: it killed the **67 GiB
bystander**, not the offender that was still climbing. A probe that over-commits
does not lose the probe, it loses whatever else is resident. Non-negotiable rules
for host-memory probes on a box that also serves:

1. `podman run --memory=<cap>` so the cgroup kills the offender instead of the
   kernel hunting a victim host-wide. Now proven rather than assumed, with the
   exact test the failed run needed: a container capped at 2 GiB touching 8 GiB
   died with `rc=137` inside its cgroup while host `MemAvailable` went
   122 -> 122 GiB, unmoved. That pre-flight is cheap (~5 s) and belongs at the
   top of every host-memory probe, *before* the expensive allocation;
2. a raised `--oom-score-adj` on the probe container, so if the host is threatened
   anyway the probe is selected, not the engine;
3. pre-flight asserts that no earlier probe is still *alive*, not just that
   `MemAvailable` looks adequate -- the specific error here was assuming a 120 s
   client timeout had killed the container, when a podman container outlives its
   launching ssh session. The `MemAvailable 24.1 GiB` in the follow-up log was
   that zombie, visible in my own output and misread as host noise;
4. no host-memory-exhaustive probe at all while the engine is up.

### What `gg`'s steady state actually looks like

Recorded because three separate numbers here look like regressions and are not.
Measured after a clean stop, a confirmed-idle host (`MemAvailable 122.23 GiB`,
`Shmem 0.00`, `swap 0.07 GiB`), and a fresh start:

```
serving:      http=200 within ~270 s of podman start (log: "Model loading took
              72.24 GiB memory and 194.1 s")
Shmem:        66.18 GiB          worker pin: 64.000 + 2.000 GiB rw-s /dev/zero
MemAvailable: ~49.3 GiB with the engine up, ~122 GiB with it stopped
swap:         ~2.3 GiB at rest (vllm 1454 MiB, VLLM::Worker 526, EngineCore 282)
during load:  the worker transiently pins an extra 2 GiB (68.000 total), which
              disappears once serving
```

Two of those would be read as damage by anyone monitoring during an incident, so
both were checked directly.

**The transient 68 GiB pin is not a leak.** It is load-time staging; the steady
figure is 66.18 GiB / `64 + 2`.

**The ~2.3 GiB of swap is not residue from the OOM either.** A restart into a host
with 122.23 GiB available and 0.07 GiB swapped reproduced it anyway, with the same
per-process split. It is the engine's own cold anon being reclaimed during model
load, when the host briefly holds ~121 GiB of anon plus the 66 GiB pin; the kernel
swaps pages that are then never touched again, and the process is functional
across a full serving cycle with them outstanding. Reading `SwapFree` as an
incident signal on this host is a trap -- it measures load-time pressure, not
damage. (This was misread in session: the swapped pids were indeed new, and "new
pid, therefore my restart caused it" was the wrong inference. Clearing it required
no action because there was nothing to clear.)

The engine was restored at 21:42 CDT and verified back to its exact pre-incident
steady state: `http=200`, `Shmem 66.18 GiB`, worker mappings `64.000 + 2.000 GiB`,
and three identical greedy requests byte-identical. During load it transiently
pins an extra 2 GiB (68.000 total), which disappears once serving -- worth knowing
before reading a mid-startup `Shmem` as a regression.

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

## 3b. Fill level is not what moves random reads; FTL state is

The plan's Stage B needs to know whether a nearly-full serving drive changes the
streaming budget. A 100 GiB uncompressed random-read dataset was measured on a
**Samsung PM9A1 2TB** (`MZVL22T0HBLB-00BL7`) on `burst`, newly provisioned as
single-XFS-AG-ish `noatime` `nodiscard`, at four occupancy checkpoints plus a
post-cleanup arm. Sweeps: `fio --rw=randread --bs=4k --direct=1 --ioengine=libaio
--size=100g --runtime=45 --time_based --numjobs=1`, after a 300 s (baseline) or
600 s dwell. Pages/token is 17.25 for the shipping BF16 layout (section 5), so
the 2048-token chunk budget is 35,328 pages / 3.6 s = **9,813 pages/s**.

```
condition        qd8 IOPS   x need    qd32 IOPS  x need   qd8 p99    qd32 p99   temp
baseline 5.7%      160,953   16.4x      207,752   21.2x     70.1 us     207.9 us   43C
fill 50%            84,776    8.6x      210,667   21.5x    268.3 us     230.4 us   45C
fill 75%            97,059    9.9x      211,775   21.6x    134.1 us     230.4 us   46C
fill 90%            97,030    9.9x      211,412   21.5x    134.1 us     230.4 us   45C
cleanup 5.5%        96,572    9.8x      211,583   21.6x    134.1 us     230.4 us   40C
```

**What is solid.** Queue depth 32, the depth a real prefetcher would run at, is
insensitive to everything tested: 207,752 -> 211,412 IOPS (+1.8%) across a 5.7%
to 90% sweep, and it did not move even in the arm run immediately after the
largest write of the study. Temperature never exceeded 46 C, so nothing here is
thermal. At production depth the device is throughput-saturated with respect to
this workload, and the plan's "IOPS is not the constraint" survives a full drive
with 21x margin.

**What is not solid, and why the shape of the qd8 curve matters more than its
size.** qd8 fell 39.7% and never came back -- but it does not track fill. Ranked
by occupancy the numbers are 160,953 / 84,776 / 97,059 / 97,030 / 96,572, i.e.
non-monotonic: the 50% arm is *worse* than the 90% arm. Ranked by the write
volume immediately preceding each arm (1,700 GiB / 954 GiB / 572 GiB, with the
baseline having only its own 100 GiB dataset build behind it) the ordering is
monotonic: the biggest preceding write produced the worst qd8 and the only tail
outlier in the study (p99.9 1,548 us, versus 84-293 us everywhere else, single
unrepeated observation). Recent FTL churn is the better predictor, not occupancy.

**The recovery arm is void, and it is void for a reason worth remembering.**
Deleting the 1.7 TiB filler did not restore qd8, and that does not prove the
degradation is permanent: `/nvme1` is mounted without `discard`, `fstrim` returns
`Operation not permitted` unprivileged, and the drive does support trim
(`discard_max_bytes` = 2 TiB, granularity 512). The FTL really was never told those LBAs were free (`/nvme1` is mounted without
`discard`, `fstrim` is `EPERM` unprivileged, and `fstrim.timer` is weekly so it could not
have covered a file created and deleted inside the window), which made "the drive still
believes it is ~90% full" the natural explanation. It is also the explanation I recorded
as leading and recommended spending root to settle.

**It was wrong. Trim changes nothing.** With root, after the fact: qd1 12,594 against
12,729 before (-1.1%), qd8 96,904 against 96,448 (+0.5%). Root settled the question by
disproving the story, which is exactly why it was worth asking for.

**Two disclosed protocol defects.** `fio --numjobs=2` with a single `--filename`
starts both jobs at offset 0 on the same file, so every fill pass wrote the whole
range twice: the log shows `io=1700GiB` against an 849 GiB target. Occupancy
percentages are correct (measured with `du`), but write volume and therefore GC
work were double what was intended -- which strengthens, not weakens, the
write-churn reading above. And qd1/qd128 were only measured at baseline; the
degraded state is unmeasured at both.

**The degraded state is a latency effect, not a throughput effect, and that is
what makes it design-critical.** qd1 and qd128 were missing from the sweep, so
the drive was re-measured after a further 25 minutes idle, still at 8% occupancy
and 42 C:

```
          baseline   idle-25m      chg   p50 base   p50 new    lat chg   x need
qd1        20,846     12,729    -38.9%      43.3 us     71.2 us    +64.4%    1.3x
qd8       160,953     96,448    -40.1%      44.8 us     75.3 us    +68.1%    9.8x
qd32      207,752    209,317     +0.8%     146.4 us    144.4 us     -1.4%   21.3x
qd128     208,027    210,434     +1.2%     602.1 us    593.9 us     -1.4%   21.4x
```

Only the latency-bound depths moved. Mean service time at qd1 went 48.0 -> 78.6
us, while the aggregate ceiling at qd32/qd128 is unchanged to within noise and
their p50 actually improved slightly. A fixed extra per-request cost that
disappears once enough requests are in flight is absorbed by the device's
internal parallelism; it is invisible at depth and it is the whole story at
qd1. Which regime the streaming path lives in is therefore not a tuning detail,
it decides whether the design has 21x headroom or 1.3x.

So the earlier conditional -- "if qd1 degrades the way qd8 did, a per-token synchronous
read lands near 1.3x budget" -- is now measured, at 1.3x against the prefill figure.
Which regime the streaming path runs in therefore decides whether there is 21x headroom
or a hairline margin.

**The degradation is data placement, and rewriting the file reverses it.** Three
explanations survived the fill study -- occupancy, untrimmed garbage, persistent FTL/GC
state -- and they were separated by experiment rather than by argument:

```
state                                        qd1        qd8
baseline (freshly written)                  20,846    160,953
filler removed, 10 min idle                 12,656     96,572
filler removed, 25 min idle                 12,729     96,448
after sudo fstrim                           12,594     96,904   <- trim did nothing
after rewriting the same 100 GiB in place   21,300    159,000   <- full recovery
same, a further 12 min idle                 21,000    160,000   <- and it held
```

Deleting and trimming do nothing because the bytes never moved; rewriting allocates new
physical extents and the reads are fast again, immediately and still 12 minutes later.
The variable is **where the data sits** -- not how full the drive is, and not whether
free space was reported to the FTL. **How much this settles, and how much it does not.** What is settled: occupancy is
not the variable, trim is not the lever, and rewriting the file restores read speed.
What is *not* settled is why, and the 12-minute persistence is weak evidence -- the
rewrite ran at 4,936 MiB/s and the drive's TurboWrite region is roughly 236 GB on a
2 TB part (about 230 GB dynamic plus 6 GB static, per TechPowerUp's 980 PRO 2 TB
teardown data; Tom's Hardware measured 238 GB absorbing writes at ~5.1 GB/s before
degrading to 1.5-2 GB/s), so a 100 GiB rewrite fits *entirely inside SLC*, and Samsung
is documented to fold that cache back slowly -- Tom's Hardware recovered only the static
portion in a 30-minute idle window. Twelve minutes therefore cannot distinguish
"new physical placement" from "still resident in SLC". Trim's irrelevance and fill's
irrelevance stand regardless of which it is.

The discriminator is time, not another knob: re-measure after hours of idle, with
nothing written in between. **That run finished, and it says placement.**

```
  2026-10-11 22:54  immediately after rewriting the 100 GiB file in place
      qd1  21.5k IOPS     qd8  160k IOPS
  2026-10-12 01:35  after 2h40m of idle with nothing written to the device at all
      qd1  21.0k IOPS     qd8  160k IOPS      (-2.3%, 0.0%)
```

The pre-rewrite slow state was 12.7k qd1 / 96.4k qd8, so the recovery is **+65% at qd1 and
+66% at qd8, and it survived two hours forty** when twelve minutes could not rule out the
alternative. Whatever the rewrite changed, it is durable on the timescale that matters for a
serving host -- it is not a write cache about to fold back.

Two consequences, one of them a feature idea:

* **A slow drive may not be a slow drive.** If random reads to the PLE file are measuring
  60-70% of expectation, a rewrite recovers two thirds of the throughput with no root and no
  re-download. For a design whose entire budget is device IOPS at 32 GiB, that is the
  difference between feasible and not.
* **So the preflight should measure the drive, not assume it.** The streaming gate already has
  to reason about RAM; adding a 30-second qd1/qd8 `fio`-style probe against the table file gives
  a placement check the same way `--require-cold` gives a coldness check. Cheaper than explaining
  a 40% throughput shortfall, and it turns "the drive is slow" into a measured claim.

Boundaries on this result: one drive (PM9A1 on `burst`), one rewrite, one idle window. `gg`'s
990 PRO measured 19,859 qd1 / 121,243 qd8 in the baselines, close to `burst`'s fast state, but
**`gg` has not had a placement probe since** and it is the machine everything here is quoted
against. Also, before rewriting a Hugging Face blob, check its link count -- cache blobs are
frequently hardlinked, and rewriting through a hardlink silently rewrites every model that shares
it. **[Later correction: that is the wrong check for a xet cache; see below.]** Also, before anyone rewrites a Hugging Face blob, check its link count: cache blobs are
frequently hardlinked, and rewriting through a hardlink silently rewrites every model that shares
it.

**Retracting operational advice given in session.** I said a streaming design must budget
against the drive's post-full-write state because "a serving drive that has ever been
filled will not come back without trim". Both halves are false: it comes back by
rewriting the table, and trim is not the lever. "A drive that has ever been written
heavily" is therefore not a permanent condition, and the sentence above about qd1
viability should be read against the current placement rather than the drive's history.
The defensible statement is narrower: if random-read latency to the PLE file is 60-70%
worse than expected, suspect either stale placement or a drained
write cache, and rewrite the file or re-fetch the checkpoint -- rewriting works
whichever of the two is at fault. Fill level genuinely does not matter -- qd32 held 207,752 -> 211,412 IOPS
across a 5.7% to 90% sweep.

**Both hosts are submit-limited, not device-limited.** `burst` got the same worker sweep
as `gg`:

```
                1 job     2 jobs     4 jobs     8 jobs
burst PM9A1      207k      420k       838k       988k IOPS  (3.9 GiB/s at 4k)
gg 990 PRO       107k      249k       523k       722k IOPS  (2.8 GiB/s at 4k)
```

Against `gg`'s own measured 14,066 pages/s, four workers on `gg` is 37x headroom and
eight is 51x; `burst` at four workers is 60x against its own 16,420. The single-job
ceiling is where a naive design lives, and near-linear scaling to 8 jobs on both drives
is the strongest single argument in this study that Stage B must be multi-threaded:
neither drive is anywhere near its own limit, one submitting thread is the limit.

### The binding requirement is decode, not the prefill chunk

Everything above budgets against the prefill chunk: 35,328 pages per 3.6 s of
compute = 9,813 pages/s. That is the plan's *second* workload number. Its first is
larger and is the one that runs most of the time -- plan section 4: "decode at
`max_num_seqs=16` needs roughly 38k page-reads/s". The `x need` column above is
against the prefill figure only, so it is the lenient reading.

The decode figure deserves to be checked against measured throughput rather than
arithmetic, and the repo has the receipts: 87 concurrency-16 runs with
`decode_tokens_per_second`, split by the pages/token that profile actually costs
(17.25 BF16, 16.5333 FP8, from section 5).

```
profile   c16 runs   median tok/s  -> pages/s      max tok/s  -> pages/s
bf16          63            601       10,372            892       15,382
fp8           24            864       14,279            993       16,420
```

The plan's 38,000 pages/s would require 2,203 tok/s at c16 for BF16, against a
best run ever recorded of 993 -- 2.3x above anything these builds have produced. It
is close to 16x the single-stream rate (121 tok/s at c1 = 1,936), i.e. it reads
like a linear-scaling extrapolation, which is exactly the assumption the c16
plateau of section 7 contradicts.

But corrected downward it is still *bigger* than the prefill number I was using:
15.4k (BF16) and 16.4k (FP8) pages/s at the best observed rate, 1.6-1.7x the
9,813. Crossed against the degraded drive, that flips the last paragraph's
conclusion the unfavourable way:

```
requirement                         qd1deg    qd8deg    qd32deg
prefill only,  9,813 p/s             1.3x      9.8x      21.3x
bf16 median,  10,372 p/s             1.2x      9.3x      20.2x
bf16 max,     15,382 p/s             0.8x FAIL  6.3x      13.6x
fp8  median,  14,279 p/s             0.9x FAIL  6.8x      14.7x
fp8  max,     16,420 p/s             0.8x FAIL  5.9x      12.7x
plan's 38k,   38,000 p/s             0.33x FAIL 2.5x       5.5x
```

**A per-token synchronous read path is not thin here, it is short**: the degraded
drive misses the requirement at the FP8 median (0.9x) and at the maximum for both
profiles (0.8x), and it is 3x short of the plan's own figure. Two independent
corrections -- the drive's shallow-depth latency and the fact that decode, not
prefill, sets the rate -- point the same way, so the design consequence is single
valued: submit at depth (>=8, 32 comfortably) and treat every qd1 number,
including the promising baseline ones, as inapplicable.

**And a third caveat that outranks the other two: these receipts are not from `gg`.**
The requirement above is derived from `benchmarks/`, whose 48 host-named receipts are
all `burst`, with the rest from `ostrich`, `raptor`, `kiwi`, `emu` and `dodo`. None
are from `gg`, which is a materially different machine. A decode-rate requirement is
a property of the host producing tokens, so 16,420 pages/s is what `burst` would need
to feed, used here as a stand-in for `gg` purely because nothing had been measured on
`gg`. Section 3c carries the `gg`-specific figure. The rule this session established:
compare a host only against itself -- `gg` pinned baseline vs `gg` exact-size vs `gg`
streaming -- never against a receipt from another box.

A related gap in the archive, found while looking for a pinned FP8 baseline to
compare the mmap numbers against: `benchmarks/exl3-ple8-final/` has `runtime.json`,
`tools.json`, `seven.jsonl` and friends but **no `clients.json`**, while
`benchmarks/exl3-ple8-mmap-final/` has a complete `clients.json` (c16 660 tok/s).
So the repo contains checkpoint-mapped PLE8 throughput with no pinned PLE8
throughput to subtract it from -- the "what does streaming cost" question has never
been answered on any host, not just on `gg`.

Two caveats on the requirement itself. These receipts come from assorted dev builds
with the table resident in RAM, which is the correct basis -- the streaming path
has to keep up with today's decode rate to avoid regressing it, not with a slower
one. And if a future build does lift c16 decode toward the plan's implied 2,200
tok/s, the requirement rises proportionally; that is a further argument for
engineering to >=38k pages/s rather than to the measured 16.4k.

**None of these numbers transfer to the serving host.** Three different drives
are in play: section 3's FX900 Pro is `burst`'s root device, this study is
`burst`'s PM9A1 scratch, and serving runs on `gg`'s single **Samsung 990 PRO
4TB**. `gg` also differs in the other direction: its checkpoint subvol is
`compress=zstd:1 ... discard=async`, so deletes *do* reach the FTL promptly there
(the no-trim caveat above is `burst`-specific) while section 3's compression
traps *do* apply there. The missing measurement that matters most is therefore
990 PRO + uncompressed PLE file (`chattr +C`, section 3) at qd1/qd8/qd32, not
another fill sweep on a drive nobody serves from.

## 3c. `gg`'s own drive, measured honestly, for the first time

Every disk number above belongs to `burst`. The transfer question -- does the
serving host behave the same -- was answered without writing anything, because two
earlier assumptions turned out not to hold on this filesystem.

**The checkpoint's PLE bytes carry the compressed-extent flag but are not
compressed.** `filefrag` reports `encoded` on every extent of the safetensors
blobs, yet `btrfs filesystem du` shows `Total == Exclusive` and
`st_blocks*512 / st_size = 1.0000`. zstd:1 gave up on FP8 payload bytes and stored
them at full size behind an encoded extent.

**O_DIRECT is honest on those extents.** Section 3's fallback trap is therefore
narrower than stated: it is triggered by *actually compressed* extents, not by the
`encoded` flag alone. Verified against the real serving file with block-layer
counters rather than page-cache deltas:

```
arm                          app read    device read
direct=1 randread 4k qd32     8.62 GiB     8.43 GiB   -> 98% real device traffic
direct=1 read  256k qd8     107.00 GiB    98.56 GiB   -> honest at 7.35 GiB/s (spec peak)
direct=0 randread 4k qd32     1.20 GiB     1.51 GiB   -> buffered, and 7x slower
```

Note the third row: on this host the buffered path is the slow one, at 20.9k IOPS
against 151k direct, so section 3's warning matters more here than it did on
`burst`, where fallback merely produced flattering numbers.

**Two instrumentation dead ends, recorded so they are not retried.** Net
`Cached`/`SwapCached` delta cannot detect a fallback on a host already holding
~114 GiB of cache: a 2 GiB re-read window displaces existing cache and the net
movement is ~0. And `/proc/<pid>/io` is useless under `libaio`, because
`io_submit` bypasses per-task I/O accounting -- both threads reported
`read_bytes: 0` through a run that moved 8.4 GiB of real device traffic.
`/proc/diskstats` around the arm is the instrument that works, unprivileged.

Measured on the real FP8 blob, full 8 GiB span, **while the engine was serving**
(so contention is included, which makes these conservative):

```
qd      IOPS   x req    p50 us    p99 us   p99.9 us
 1    19,859   1.2x       39.7      49.9        56.1
 8   121,243   7.4x       47.4     329.7       593.9
32   122,571   7.5x      197.6     823.3      1351.7
128  124,985   7.6x      806.9    2867.2      3915.8
```

Single-job throughput saturates by qd8 at ~122k, far below the 990 PRO's ~1M
spec, which raised a real question: device, submit path, or btrfs per-IO cost.
It is the submit path -- scaling is near-linear in worker count:

```
numjobs=1   107k      numjobs=4   523k IOPS (2.0 GiB/s)
numjobs=2   249k      numjobs=8   722k IOPS (2.8 GiB/s)
```

Consequences, in order of importance. The plan's Stage B needs a *multi-threaded*
(or multi-queue io_uring) submitter for the same reason llama.cpp #28136 needed 32
workers: one thread cannot get this device past ~150k IOPS however deep its queue
is. Against the FP8 decode requirement of 16,420 pages/s, four jobs give ~32x
headroom. And qd1 remains the wrong shape on this host too -- 1.2x, clearing with
no margin, consistent with `burst`'s degraded 0.8-0.9x being a symptom of the same
design error rather than of that drive's state.


### The `gg` baseline: measured, and it rewrites the requirement

`benchmarks/gg-ple8-pinned-20261011/` is the first `gg`-produced receipt in this repo,
run with the repo's own instrument configured to match the historical ones (`mtp 3`,
`static`, 256 output tokens, 2 warmup + 3 runs, concurrency 1/2/4/8/16, co-located
client, against the serving `qwen38-exl3-ple8` container) and a `runtime.json` beside
it naming the GPU and container. This is the pinned `round2` arm -- the baseline every
later arm is compared against.

```
  c    min  median     max  spread   pages/s @16.53   qd8 headroom   qd1 headroom
  1    127     132     135    6.1%        2,179         55.6x           9.11x
  2    217     227     229    5.5%        3,760         32.2x           5.28x
  4    349     385     385   10.5%        6,360         19.1x           3.12x
  8    586     593     600    2.3%        9,802         12.4x           2.03x
 16    845     851     889    5.2%       14,066          8.6x           1.41x
```

So the requirement for *this* host is **14,066 pages/s** at the c16 median, or
14,694 using the fastest run. Against `gg`'s own disk that is 8.6x at qd8 and 1.41x at
qd1 -- the same qualitative verdict as before, now with both numerator and denominator
from the same machine.

Worth stating plainly because it partly rescues the number I was criticising: `burst`'s
fp8 c16 median is 864 tok/s and `gg`'s is 851, so the borrowed requirement was wrong by
only 1.5%. Decode throughput here is GPU-bound, so a much larger host buys almost
nothing on this axis -- `burst`'s advantage shows up in the *max* (993 vs 889) rather
than the median, and of course in 995 GiB of RAM versus 126 GiB. The provenance rule
still stands, but for this metric it happened to be nearly free. It would not be for a
CPU-bound or storage-bound measurement, and the per-concurrency rows are also the first
`gg` evidence of the c16 plateau this time on the target hardware: throughput gains
only 1.44x from c8 to c16, so per-token page pressure rises far slower than concurrency.


---

## 4. The mmap path IS blocked on discrete RTX -- my earlier correction here was wrong

This section previously argued that plan section 3 overstated the architectural
constraint, and that checkpoint-mapped PLE on a discrete RTX PRO 6000 was *slow* rather
than *disabled*. **That was mistaken, and the plan was right.** Measured by trying to run
it on `gg` with `PLE_MMAP=1`:

```
ngram_embedding.py:77   require_pageable_access(device.index)
ple_pageable.py:90      raise RuntimeError(...)

RuntimeError: Engram checkpoint_mapped requires a GPU that reads pageable host memory
through the host page tables; device 0 reports
CU_DEVICE_ATTRIBUTE_PAGEABLE_MEMORY_ACCESS_USES_HOST_PAGE_TABLES=0 (rc=0)
```

The container exited 60 s into startup. The gate is not defensive pessimism, it reflects
what the code does: `MappedTable` opens the checkpoint with a plain
`mmap(..., MAP_SHARED, PROT_READ)`, calls `madvise(MADV_RANDOM)` to defeat readahead, and
`_gather_mapped_rows_kernel` -- a Triton kernel -- **dereferences that pageable host
pointer from the GPU**. On a discrete part the GPU cannot walk host page tables for
unregistered memory, so the access is illegal rather than merely slow.

**Where my reasoning went wrong, in case it recurs.** I had measured host-memory gather
throughput on `gg` and found it functional but mediocre, then concluded the mmap path was
merely slow. But what I measured was **pinned** host memory via UVA, which a discrete GPU
*can* dereference -- it is exactly how the engine serves today. Pinned and pageable are
different capabilities here, and the attribute in question describes only the latter. A
measurement of the adjacent supported thing was read as a measurement of the thing under
test.

That distinction also explains the design being corrected, rather than a knob to turn.
`PagePrefetcher` already does the CPU-side half -- a `ThreadPoolExecutor` named `ple-touch`
that faults in both ends of every row (`touch()`), plus pinned staging for the *step
inputs* (ids, `query_start_loc`, ngram context). What it cannot do is move the gathered
table bytes, because the kernel reads them straight out of the page cache mapping. So on
discrete GPUs the entire payload path depends on an attribute the hardware does not set.

**What stage B still needs, and why the disk numbers say it works.** The missing piece is
a CPU-side gather into pinned staging followed by an async H2D copy -- i.e. route the
payload through pinned memory, which discrete GPUs *can* read, instead of depending on
pageable access. The volumes make this unscary: at `gg`'s measured 851 tok/s the useful
payload is 851 x 16 rows x 160 B = **2.2 MB/s**, and even at 10k prefill tok/s it is
25.6 MB/s, against 26 GiB/s of measured pinned H2D capability. The hard resource is page
reads, not the copy: 16.5333 pages/token means 165k pages/s at that prefill rate, which
exceeds `gg`'s single-worker 122k IOPS ceiling and needs the 2-4 workers already called
for. The module is 547 lines with the layout discovery, shard location, verification and
prefetch scaffolding already written, so this is a bounded change rather than a new
subsystem -- but it is *code*, not configuration, and no flag on the current image will
do it.

### Status: closed as infeasible on this hardware class, not blocked

Arm C (`PLE_MMAP=1`, GPU-dereferenced file mapping) is not waiting on anything. The verdict
has three parts and they are not the same claim:

1. **The mechanism cannot work on a discrete RTX, and no flag or driver setting changes that.**
   `require_pageable_access` (ple_pageable.py:67, called at ngram_embedding.py:77) checks
   `CU_DEVICE_ATTRIBUTE_PAGEABLE_MEMORY_ACCESS_USES_HOST_PAGE_TABLES`, which reports 0 on the
   RTX PRO 6000. The kernel it gates (`_gather_mapped_rows_kernel`, defined at :186 and invoked
   at :329) is handed the mapping pointer and dereferences it, so there is no path around the
   gate that keeps the design's premise. The capability requires the GPU to walk host page
   tables -- a property of a coherent link (NVLink-C2C on GB10/GH200), not of a driver option.
2. **The design itself is sound, for the machine it was written on.** vLLM #58439/#58835 target
   GB10, where the attribute is set. Closing arm C here is a statement about the host, not a
   criticism of the upstream design.
3. **The objective survives; only this route to it is closed.** Streaming the table from NVMe
   without pinning it is viable on this GPU class by the copy shape -- sglang-ssd-stream is
   validated on the same card, and `benchmarks/gg-stageB-readpath-20261012/` measures a threaded
   CPU gather at 182,535 pages/s against a 128k-170k pages/s requirement. Arm C's slot in the
   plan is filled by stage B, not left empty.

**The two escape hatches are not arm C.** Making the mapping dereferenceable means registering
it: `cudaHostRegister` over the range forces residency -- it faulted in 49 GiB unrefusably and
OOM-killed the serving engine, which is how the session started -- so you pay the RAM the design
existed to avoid and have simply rebuilt arm B. Registering a *bounded* window instead is a
pinned staging buffer, which is stage B by another name. Either way the design stops being arm C
at the moment it starts working.

### A trap in this file for the next reader: `PagePrefetcher` is not a copy path

`PagePrefetcher` (:412) pins buffers that look like staging -- `torch.empty(max_tokens,
dtype=input_ids.dtype).pin_memory()` at :471, plus query-offset and context slots at :472-474
and `copy_(..., non_blocking=True)` at :485-488. Those carry the **step's inputs**, not table
bytes. Its purpose is stated at :498-500 and in the module docstring: it issues `readahead` for
a step's pages "at once" so the GPU's page faults "wait on reads that are already in flight in
parallel instead of issuing them one at a time." The docstring is explicit that there is "no
CPU-gather/host-to-device staging path" and that "the prefetch is only a hint: the lookup is
correct whether or not it has finished."

Anyone auditing this for a port to discrete GPUs will find those `pin_memory()` calls and
reasonably conclude a copy path exists. It does not.

The same docstring also concedes a structural weakness that outlives the hardware question:
because a decode step's rows are unknown until the previous step samples, **the prefetch has no
lead over the lookup** -- so on a cold table the GPU and the prefetch race for the same pages,
and "loading the weights evicts it from the page cache" means every start begins cold. Worth
remembering when comparing stage B against a hypothetical GB10 deployment: zero-copy is not
automatically the faster design here, it is the lower-memory one.


The arm C receipt at `/home/rtx/arms/C-mmap-20261011-2308/` holds only a 372-byte
`start.log` (the container id, no engine stderr): the failure text lives in this document
rather than beside it, because the log was read from `podman logs` in-session and not filed.
Weaker than the other arms' receipts; noted so nobody treats the absence of a transcript as an
absence of evidence.

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

### The sglang reader does not need host-page-table access at all

Re-read against the section 4 finding, because the two are easy to conflate: `sglang-ssd-stream`'s
reader is CPU-initiated and never asks the GPU to touch host pages. Its own description of
the hot path:

1. copy the required row IDs asynchronously into **pinned** host memory;
2. **deduplicate the filesystem pages** touched by those rows;
3. read the pages concurrently through **Rust and `io_uring`**;
4. restore duplicate rows in their original order;
5. convert FP8 or BF16 staging data on a **separate CUDA stream**;
6. synchronise only if the SSD work outlasts the overlapping GPU computation.

It owns "a 32 MiB registered page pool and two 16 MiB pinned staging buffers" -- about
**64 MiB of extra fixed memory for a 47.68 GiB table** -- and explicitly has "no
multi-gigabyte private row cache", relying on reclaimable filesystem cache. That is the
stage-B shape, and it is why the attribute that blocks this repo's mmap path is simply not
involved: every byte the GPU reads is in registered memory.

It is also **validated on the exact GPU in `gg`**, an RTX PRO 6000 Blackwell 96 GB, with a
comparison the plan should quote instead of my extrapolations: RAM-loaded 148.5-156.2 tok/s
vs SSD Stream 164.7 tok/s, with the honest caveat that "the 164.7 tok/s run is not a claim
that SSD is faster than RAM" and that **requests dominated by new, random lookup rows
measured 126-137 tok/s** -- roughly 10-15% below the RAM-loaded range on the worst-case
workload, alongside "no swap, OOM, restart, or host-memory growth". Its RTX profile defaults
to a 131,072-token context, with 262,144 an explicit tested override and DGX Spark at 262K.

Three consequences for this repo's own design notes:

* **page deduplication is worth much less than the bullet above originally claimed, and my
  own measurement refutes the first version of it.** It said 16 rows per token routinely share
  pages, and that the 16.5333 pages/token figure was the result of deduplication. Both halves
  are wrong, and the second was internally inconsistent: 16.5333 is *above* 16, so nothing was
  being saved. The read-path instrument measured why -- `pages_per_row` is 1.0147 to 1.05 on
  random ids, which is exactly the straddle rate a 160-byte row against a 4 KiB page predicts
  (160/4096 = 3.9%). Random row ids land on distinct pages; 16 x 1.033 = 16.53 is the whole
  story. So the volume is set by **byte amplification, 25.6x** (4096 bytes fetched per 160 used),
  which deduplication cannot reduce. Dedup pays only where ids actually collide inside a step --
  repeated n-grams across adjacent positions, duplicate lookups across the batch -- and that
  collision rate is a property of real traces this repo has never captured. sglang dedups
  anyway, which is cheap and correct, but it is not what keeps its volume sane.
* **it is page-cache-backed, not O_DIRECT** -- "Linux may retain frequently used pages in
  reclaimable filesystem cache". So compression is transparent to it, `chattr +C` is not a
  precondition, and the real enemy is kernel readahead, which they defeat by requesting
  exact 4 KiB pages "instead of triggering large mmap readahead windows". Any streaming doc
  must say which access method a requirement applies to.
* **the mmap and streaming designs are different lineages, not two settings.** This repo's
  `PLE_MMAP=1` comes from vLLM #58439/#58835, written for GB10's unified memory; sglang's
  reader is an independent CPU-side implementation. "mmap-based" in a comparison table hides
  that distinction, and it is the distinction that decides whether the feature runs on a
  discrete RTX at all.

### Retracted: "zstd gave up on the FP8 bytes" -- it did not, and that changes the trade

I explained the production blob's 4,010 encoded extents by saying compression was attempted and
declined, so the extents were an artefact with no benefit attached. That was an inference, never a
measurement. Measured, on 256 MiB of real FP8 weight bytes taken from the byte-verified copy:

```
  256 MiB of real FP8:  zstd-1 -> 86.0%   gzip-1 -> 87.1%
```

So **FP8 weight data compresses about 14%**, btrfs compresses it because it can, and the encoded
extents are exactly what a `compress=zstd:1` mount should produce -- no download-path mystery
required. It also retires the directory-inheritance hypothesis: `lsattr -d` on every level of the
production cache tree shows no `c` flag anywhere.

This makes the NOCOW recommendation a genuine trade rather than a free win. Choosing a NOCOW layout
for the 47.684 GiB table gives up roughly **6.7 GiB of disk** to gain the retention and throughput
measured above. On the target host, ~6.7 GiB of disk against ~8-14% throughput and model-matching
hit rates is not close -- but it should be priced on both sides instead of asserted as free, and the
BF16 case needs the same arithmetic rather than the 84.4% figure I quoted from a different file.

Two mechanism notes worth keeping, both from fixtures that failed loudly rather than quietly:

* **`chattr +C` on a directory applies only to files created afterwards.** Setting it after a copy
  leaves the copy on CoW with compressed extents, and the file looks fine. A layout4 fixture built
  that way reported 4,639 encoded extents while labelled "nocow"; the encoded-count assertion is
  what caught it. Any install instruction must therefore read: flag the directory, *then* download.
* **`chattr +c` is not `compress-force`.** Forcing compression on genuinely incompressible data
  still falls back to plain extents, so an encoded-extent fixture has to be made from data that
  really compresses -- which, per the measurement above, FP8 does.

### Encoded extents damage page-cache retention, and that is what "O_DIRECT is not direct" really was

The 1.64x steady-state shortfall had two candidate causes -- more passes needed, or btrfs
encoded extents. **10 passes settled it, and settled it in favour of layout.** Same host, same
drive, same 8 threads, same request pattern; the control file is a `--reflink=never` copy with
identical md5, differing only in `chattr +C` / `compress=no`:

| layout | cell | pages/s | hits | cache fraction | dev/req |
|---|---|---|---|---|---|
| encoded | uncapped | 215,589 | 88.6% | whole region | 0.114 |
| encoded | 4 GiB | 163,749 | **9.2%** | 39.6% | 0.908 |
| encoded | 3 GiB | 147,699 | **−15.0%** | 27.1% | 1.150 |
| NOCOW | uncapped | 227,779 | **90.0%** | whole region | 0.100 |
| NOCOW | 4 GiB | 177,409 | **42.9%** | 39.6% | 0.571 |
| NOCOW | 3 GiB | 168,317 | **31.1%** | 27.1% | 0.689 |

Four things follow, and the third is the one that makes the attribution a proof rather than a
correlation:

* **Stationarity is refuted outright.** At a 4 GiB cap the encoded file went from 12.3% hits at
  3.2 passes to **9.2% at 10 passes**. It got worse with more passes. This is not an asymptote
  we had not reached.
* **The model is right about LRU and wrong only about the filesystem.** NOCOW hit rates land
  *above* the C/N prediction (+3.3pp at cap4, +3.9pp at cap3), and the uncapped cells land on
  `1 - 1/passes` almost exactly: 90.0% against 90.0% predicted for 10 passes. The encoded
  uncapped cell reads 88.6% for the same reason -- a 14% metadata tax on the single cold pass.
* **`O_DIRECT` behaves completely differently between the two, which only layout explains.**
  Page-cache growth across 21M requests: **0.51 GiB encoded, 0.00 GiB NOCOW.** Device pages per
  request: **1.16 encoded (flagged), 1.00 NOCOW.** No placement effect and no queue-depth effect
  causes the buffered path to populate cache for a direct read. So the whole "O_DIRECT is
  neither direct nor cache-free" finding reduces to one thing: this file's extents are encoded.
* **NOCOW is a prerequisite, not a tuning knob.** +8.3% throughput at the 4 GiB cap, +14% at
  3 GiB, hit rates that match the model instead of missing by 1.64x, the metadata tax gone, and
  an honest O_DIRECT.

Two caveats kept attached. The NOCOW copy was also **freshly written**, and the placement
finding elsewhere in this document means fresh writes can be genuinely faster -- so part of the
throughput gap may be placement. The two mechanisms are separable, though: fresh placement does
not make a direct read populate the page cache, so the 0.51-vs-0.00 GiB result stands on layout
alone. An encoded-but-freshly-written copy is isolating the rest. And the trade is not free for
every profile: compression shrinks a BF16 table by ~16% (95.37 -> 80.47 GiB measured earlier),
which reduces bytes on the wire, while encoded extents damage retention -- unresolved for BF16,
flagged rather than guessed.

### A counter I trusted without calibrating, and the claim it manufactured

**Retracted: the section here previously claimed the drive's own DRAM cache had absorbed
4.97 million page requests.** It had not. `disk_reads()` was reading field f[6] of
`/proc/diskstats` as "sectors read"; f[6] is **milliseconds reading**. Sectors is f[5], and
reads completed is f[3]. Every device number that instrument produced was therefore
milliseconds divided by eight, which is why a 20 GiB cold read appeared to touch the device
27,279 times while showing 76 us median latency -- a latency that was, in fact, the device
answering, in perfect contradiction to the traffic number sitting next to it.

The calibration that settled it, run against a quantity known in advance:

```
  dd of exactly 1 GiB, iflag=direct, from an evicted region:
    f[3] reads completed  +     8,192          (1 GiB in 128 KiB requests)
    f[5] sectors read     + 2,097,152          <- exactly 1 GiB
    f[6] ms reading       +     2,568          <- what I was dividing by 8

  corrected instrument, 619 MiB of O_DIRECT reads on a host-cold region:
    requested 158,560 pages   device 158,560 pages   one request per page, no merging
    hit rate 0.0   drive-absorbed 0   residency 136,129 -> 4 pages at the right offset
```

What survives of the original point, and what does not:

* **Physically, drives do have DRAM and SLC caches**, and `diskstats` cannot see them; that
  much is not in doubt. What was in doubt is whether it had *here*, and the honest answer is
  the retracted measurement demonstrated nothing. The in-flight test is the direct one --
  the same 1 GiB region read twice with `O_DIRECT`, host-cold both times, where any second
  pass served without device traffic can only have come from inside the drive.
* **The instrumentation discipline is the real lesson and it generalises:** a counter that
  has never been checked against a known quantity is not evidence, no matter how plausible
  its output looks. This is the sixth self-inflicted measurement fault today and the first
  one where the failure produced numbers that were *believable* -- the others at least looked
  strange. The instrument now carries the field table, a `device_reads_completed` cross-check
  (requests vs pages: a big divergence means merging, which means the arithmetic changed
  underneath you), and a residency probe over the region actually touched rather than from
  offset 0 -- that second bug made "verified cold" mean "looked at somewhere cold".
* **One earlier claim needs re-verifying rather than inheriting**: the 98%-honest device
  traffic figure for `gg`'s FP8 blob came from a pre-instrument ad-hoc comparison against
  fio's own byte count, so it may well be fine -- fio was on the other side of that ratio.
  It is queued for the same dd-style calibration on `gg` rather than being trusted by
  association.

### `posix_fadvise(DONTNEED)` semantics, and one of my controls that could not fail

Demonstrated, with the caveat in the second bullet being the interesting part:

* Whole-range `DONTNEED` works decisively: 2.00 GiB resident became 0.00.
* Per-page `DONTNEED` **does bound the cache for pages this process just read**: starting
  from a cold region, immediate per-page eviction left residency at ~0 after 399k page
  requests that would otherwise have left ~1.5 GiB behind, and a 100k-page lag window left
  0.641 GiB -- residency tracked the window size, which is the signature of the mechanism
  working rather than being skipped.
* The isolation test that "proved" per-page eviction was inert **was itself broken**: it
  ran a whole-range evict first, so the pages it then tested per-page had already been
  dropped, and the result was guaranteed before the interesting call happened. Fourth
  self-inflicted no-fail experiment this session. The corrected conclusion comes from the
  residency measurements above, not from that test.

Practical consequence for stage B: a streaming reader can keep its own page-cache footprint
bounded unprivileged, which is what "bounded footprint" meant in the plan. It cannot
reclaim pages that other things made resident, and should not try -- the kernel does that
under pressure for free, since they are clean file-backed pages.

### Two filesystem facts from `gg`'s real blob that the fio baselines could not show

From the matrix validation pass (4 cells, 12 steps each, whole 8.1 GiB FP8 blob, background
device traffic measured at exactly 0 p/s so this is not another process):

| cell | pages/s | requested | device pages | ratio | page cache grown |
|---|---|---|---|---|---|
| `pcache` uncapped, 8 threads | 131,407 | 402,264 | 585,165 | **1.45x** | +1,778 MiB |
| `pcache` 4 GiB cap, 8 threads | 129,173 | 402,264 | 588,685 | 1.46x | +1,794 MiB |
| `odirect` uncapped, 8 threads | 125,936 | 402,264 | 619,203 | **1.54x** | **+430 MiB** |
| `odirect` 4 GiB cap, 8 threads | 121,886 | 402,264 | 619,686 | 1.54x | +431 MiB |

**1. A 4 KiB random read costs about 1.5 device pages on this filesystem.** Requests
themselves are what `pages_per_s` counts, so the requirement table is unaffected in its own
units, and the fio ceilings are in requests/s too, so the comparison still holds. What changes
is the bandwidth and the interpretation of the counters: ~130k pages/s of requests is
~190k pages/s = ~745 MiB/s at the drive, and a receipt where device pages exceed requests is
not a broken measurement. Previously this surfaced only as a *negative* hit rate and a
negative `drive_cache_absorbed`, which looks like bad arithmetic. The instrument now reports
`fs_device_amplification` with `device_sectors_per_request` beside it so a cluster read can be
told from a metadata walk.

**2. `O_DIRECT` on this blob is not fully direct.** An "O_DIRECT" run grew the host page cache
by 430 MiB across 402,264 pages of requests -- 27% of them. That is the partial-silent-fallback
failure mode measured earlier on compressed extents, now showing up on the FP8 table that was
previously rated 98% honest. Both statements can be true: most traffic reaches the device, some
of it is buffered. The practical consequences:

* `O_DIRECT` cannot be treated as a clean control for "what the drive alone would do". A
  direct-mode measurement on this filesystem needs the page-cache-growth check that replaced
  the old fixed 0.5 GiB threshold (`!direct-populated-cache`, comparing growth to pages
  requested).
* It also means `O_DIRECT` does not fully protect against cache pressure on a tight host:
  it still occupies some page cache, which is exactly the resource being economised.
* **The access methods are NOT within 4% -- that figure came from 12-step cells and the full
  matrix refutes it.** Page cache beat `O_DIRECT` by +29.6% at 4 threads, +18.5% at 8, +11.4%
  at 12, and the gap reversed to -2.0% and -4.9% at 12-16 threads under a 4 GiB cap. Short
  cells understate badly (131k pages/s over 12 steps against 182k over 48 on the same blob),
  so the validation pass was not qualified to answer this. Full matrix:
  `benchmarks/gg-stageB-readpath-20261012/`.

Short cells understate the rate: the same blob at 8 threads measured 157,843 pages/s over 40
steps against 131,407 over 12, so the matrix uses 48 steps per cell and single-cell numbers
from shorter runs should not be quoted against it.

### What the read path costs to submit in Python

The instrument is Python threads, and that turned out to be a result rather than an
inconvenience. Two artefacts had to be removed first: claiming pages one at a time through
a shared Condition made throughput *fall* with thread count (115k pages/s at 2 threads,
23k at 16), and modelling one step as 16 rows (a single token) made every high-thread run
barrier-bound rather than I/O-bound. With a static partition and a realistic prefill-sized
batch of 2,048 tokens:

| regime | threads | pages/s | notes |
|---|---|---|---|
| page-cache hits | 2 | 172,411 | 4.3 ms CPU per 1k pages: about half a core at the required rate |
| page-cache hits | 16 | 196,499 | more threads mostly add GIL and barrier cost; hits are ~3-5 us |
| device-latency reads | 4 | 42-52k | p50 82 us -- real NVMe latency, so the syscall path is where the time goes |

The device-latency row is the one that sizes the implementation: at ~82 us per read,
116k pages/s needs roughly ten requests in flight, and a synchronous per-thread read
delivers exactly one. `burst`'s qd1 figure of 19,859 IOPS per thread is the same fact from
the fio side. **Stage B therefore needs either many threads or an actual submission queue**
(`io_uring` via C/Rust), which is very likely why sglang's reader is Rust with persistent
`io_uring` rather than a thread pool. Whether Python can reach the target at 8-16 threads is
open; the cold probe is measuring it.

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

# 7. can a "recovery" arm even be measured here?  (burst /nvme1: no -- EPERM)
cat /sys/class/nvme/nvme1/model
cat /sys/block/nvme1n1/queue/discard_max_bytes /sys/block/nvme1n1/queue/discard_granularity
findmnt -no OPTIONS /nvme1 | grep -c discard        # 0 = online trim off
fstrim -v /nvme1 2>&1 | tail -1                     # Operation not permitted unprivileged
systemctl is-active fstrim.timer                    # weekly only

# 8. is a fill-state drop actually fill, or preceding write volume?  print both
grep -E "^### |WRITE: bw" /nvme1/bench/fillstate.log

# 9. the API asymmetry that OOM-killed the engine (section 1c): register faults
#    pages in unrefusably; alloc refuses cleanly.  Watch residency, not RSS,
#    and never do this with the engine resident.
watch -n1 'awk "/^MemAvailable|^Shmem|^SwapFree/{printf \"%-11s %.2f GiB\n\",\$1,\$2/1048576}" /proc/meminfo'
```

## Incident, 2026-10-12 ~01:56: my benchmark tool destroyed a production checkpoint shard, and the verification habit caught it

**What happened.** The placement probe I staged tonight was adapted from the SLC-decay script,
which was written against a scratch file and therefore passed `--size=20g`. Against the 8.1 GiB
checkpoint blob, **fio extended the file to 20 GiB to satisfy the requested size, overwriting the
shard's contents** -- not appending padding, as I first assumed. `mtime` moved to 01:56:05, the
file became exactly 21,474,836,480 bytes, and the md5 recorded at 01:41 no longer matched.

**How it was caught, by accident rather than design.** The next step copied the blob to build a
control and reported a *different* md5 than the one printed twenty minutes earlier. The
block-by-block comparison the repair script ran then showed **0 of 33 256 MiB blocks matching**,
with only 0.6% zero bytes -- so the payload had been replaced, not extended. Had I trusted the
"obviously it just padded the tail" story, this would have been discovered at the next engine
restart instead.

**Why recovery was possible at all.** The `chattr +C` control copy made at 01:41 -- built to
verify that `chattr +C` actually took, as the filesystem plan's fixture gate demands -- was a
byte-identical snapshot of the pre-damage blob, md5 `05d83cc9…`. The fixture check existed to
stop me reporting a lie about layout; it doubled as the only good copy. Recovery was therefore:
verify the reference, stage a copy, verify the staged copy's md5, `mv` it into place atomically.

**Verification after restore, not before it was declared fixed:**

```
  size 8,594,481,976   md5 05d83cc951f8b7b6acefac0b862c92e4   (matches the 01:41 record)
  all 22 blobs: file size agrees with its own safetensors header   (0 mismatches)
  all 22 shards open via safetensors.safe_open the way the loader does  (0 failures)
  hardened probe re-run: size and md5 identical before and after
  sha256 baseline captured for all 50 blobs on the volume, all 22 checkpoint shards covered;
      the restored blob: sha256 64db96ef9410180f...  md5 05d83cc951f8b7b6...  size 8,594,481,976
  engine restarted from the restored checkpoint:
      SERVING after 120s   Model loading took 72.24 GiB and 191.96 s
      max-model-len 524288   checkpoint_mapped false   num_speculative_tokens 3
      Shmem 66.18 GiB   pins 64.000 + 2.000 GiB   MemAvailable 49.17 GiB
      greedy output identical to the pre-incident reference strings
      blob md5 after the load: unchanged
```

**The engine never noticed** -- its weights were already loaded and pinned, which is exactly why
this class of damage is invisible until a restart. A restart would have failed on a corrupt
safetensors header.

**Consequences recorded as rules:**

* **`--readonly` and `--size=100%` are mandatory** in any fio invocation aimed at a file this
  project does not own. `--readonly` makes fio refuse to create or extend rather than silently
  reshaping the target. The probe script is fixed and was re-run to prove the fix.
* **Never adapt a benchmark script from a scratch-file context to a production file without
  re-reading its creation flags.** `--size=20g` is harmless on a file you made and destructive
  on one you did not.
* **Record an md5 of any artifact before measuring against it.** The recovery worked because one
  existed for an unrelated reason; that is luck, not process.
* **A verification script is itself a script, and needs the same care as the thing it checks.**
  **Seven** of them failed tonight, and all seven were in the tooling I wrote to check my own
  mistake: `fio --size=20g` extended the file it was meant to observe; `podman inspect -q -f`
  suppressed format output so a readiness guard could only ever report "not running"; a
  nested-quote syntax error killed the determinism check *after* the script had printed a
  confident-looking config block; `podman events` streams by default and hung a diagnostic; a
  redirect pointed at a local-only path that does not exist on the remote; `find -name "23224da14*"`
  matched `*.lock` and then `*.refs` so two consecutive "verify the blob" steps hashed a 0-byte
  and a 266-byte file instead of an 8 GiB one; and `date -ud "2026-10-12 01:41:29"` silently
  treated a host-local timestamp as UTC, which would have told me the host runs UTC when it runs
  CDT.
  The pattern across all seven: **each one produced output that looked like an answer.** A guard
  that can only read "not running", a hash of an empty file, and a timezone conversion that
  returns the input digits are all indistinguishable from success unless the check asserts
  something about its own inputs -- expected size, expected hash, expected offset. Every check in
  this document that survived the night had a written-down expectation; every one that failed had
  a plausible-looking output instead. When the tool is the only thing between you and a corrupt
  checkpoint, a broken check is not cosmetic -- it is the failure.
* **Verify a fixture's identity at every step that consumes it.** Two of tonight's three runs are
  valid only because the copies were md5-checked as they were built.

**Effect on tonight's findings.** The encoded-vs-NOCOW comparison stands: RUN 1 (01:21-01:40)
and RUN 2 against the `chattr +C` copy both predate or are independent of the damage, and the
mechanism result -- `O_DIRECT` growing the page cache 0.51 GiB on encoded extents versus 0.00 on
plain -- cannot be produced by placement or by file size. **The encoded-but-freshly-written
control is void**: it measured a 20 GiB file that was mostly not checkpoint data. Layout versus
placement is therefore still separated only by the cache-growth evidence, not by a control, and
the redo must copy from a scratch file, not from the production blob.

**Also measured while probing, on the restored file with the hardened tool:** `gg` reads at
**20.1k qd1 / 131k qd8**, against its historical 19.9k / 121.2k baseline and `burst`'s
12.7k / 96.4k slow-placement state. So `gg` is in a healthy placement and its drive is not a
suspect in any of these numbers. **Closed out.** The engine restarted from the restored
checkpoint, reached serving in 120 s, pinned 64.000 + 2.000 GiB, and produced greedy output
identical to the pre-incident reference strings -- which is the check that actually matters,
because a wrong byte anywhere in a shard changes tokens. Output determinism is the oracle for
checkpoint integrity, the same one that made arm B credible.

One operational side effect of the night, unrelated to the damage: this restart loaded in
**191.96 s** rather than the 170.2 s measured earlier, because every benchmark cell ran
`--evict-first` and the last load was a cold 72 GiB read of the weights. Nothing is wrong; the
first start after this may simply be slower until the page cache refills.

The `chattr +C` reference copy is **kept deliberately** -- it is a verified, byte-identical,
NOCOW-laid-out 8 GiB fixture, which is precisely what the layout-vs-placement redo needs, and it
costs 8 GiB on a volume with 3.3 TB free.

## Trust table: which of tonight's measurements survived the file damage

**All times in this section are host-local `CDT` (`America/Chicago`, UTC-5), the clock `stat` and
`podman inspect` report.** In UTC: the damage ran **06:56 -> 07:15 UTC**, and the 01:41 integrity
anchor is **06:41 UTC**. This is stated because vLLM's own container logs run **UTC** -- the same
instant appears as `07:18:03` in a log line and `02:18` on the host -- so cross-referencing this
table against engine logs without the offset makes the incident look five and a half hours away
from the measurements it invalidated.
Everything below is classified against that timestamp, so the boundary is checkable rather than
something to take on faith.

| measurement | when | status | why |
|---|---|---|---|
| pinned vs exact-size pair (A1/B1/A2/B2) | 23:19-01:00 | **stands** | completed long before; read-only access |
| 24-cell read-path matrix | 00:05-01:05 | **stands** | pre-damage; `preadv`/`O_DIRECT`/`fadvise` only |
| 3.2-pass steady-state run | 01:21-01:40 (RUN 1 of that job) | **stands** | pre-damage |
| eager / CUDA-graph cost | 00:49 | **stands** | pre-damage; engine-side, no file writes |
| 10-pass encoded run (RUN 1) | 01:21-01:40 | **stands** | pre-damage |
| 10-pass NOCOW run (RUN 2) | 01:41-01:53 | **stands** | reads the separate `chattr +C` copy, made 01:41 and md5-verified against the pristine original |
| first placement probe (20.6k / 154k) | 01:56 | **void** | measured the file *as it was being extended* |
| `fresh-encoded` control, 6 cells | 01:57-02:0x | **void** | the 20 GiB file was mostly not checkpoint data |
| fragmentation hypothesis | derived from the above | **withdrawn** | an interpretation of garbage, and the more damaging loss: a confident paragraph built on invalid cells |
| hardened placement probe (20.1k / 131k) | 02:2x | **stands** | post-restore, `--readonly`, file md5 unchanged before and after |

**That limit is now closed, and closed by someone else's numbers rather than mine.** Every shard
was checked against the LFS `sha256` oids published by the Hub for revision
`888306bd3996d6317758c07df50622829259ad17`: **22/22 match**, including
`model-00019-of-00022.safetensors`, the shard I destroyed -- published `64db96ef9410180f...`,
recomputed on disk `64db96ef9410180f...`, published size 8,594,481,976, restored size identical.
Those oids were fixed before this machine was involved, so the whole checkpoint -- not just the
restored shard -- is provably the published bytes. The "inferred, not recorded" caveat attached to
runs earlier than 01:41 is withdrawn, and with it the last reason to re-run any engine measurement
on corruption grounds.

Getting there required matching through the cache's actual layout: 16 of 22 blobs are stored under
their oid name, 6 are stored in the xet CAS under their `xetHash` name with an oid-named symlink
pointing at them, so a naive name-keyed comparison reports 6 false failures. My first pass made
exactly that error and printed "22 mismatches" when 16 hashes already agreed.

Earlier still, that comparison was worse than wrong: I hand-transcribed the API listing into the
script and corrupted the `xetHash` column (one value appeared twice), which manufactured the
mismatches I then had to chase. **Fetch reference data; do not retype it.**

The old limit read: the earliest integrity anchor for the blob is an md5 taken at **01:41**. For runs before that, intactness is *inferred* -- no tool in this toolchain writes
except the one that did this, and engine output is bit-identical across the incident -- rather
than recorded. A sha256 baseline for all 22 blobs has now been captured so the next claim rests
on a hash and not an inference.

**A mechanism I claimed and then disproved.** I wrote that the engine had this blob mmapped, so
writes through the page cache could have silently replaced weight pages it had not faulted in --
"wrong tokens, not an outage", complete with a plausible-sounding mechanism. Checked properly, it is
**false for a loaded engine**. `/proc/<worker>/maps` contains exactly two paths matching
"safetensors", and both are shared libraries (`_safetensors_rust.abi3.so`,
`fastsafetensors/...so`); zero mappings reference the checkpoint by either hash. vLLM streams
weights into GPU memory at load and holds no file mapping afterwards. So the damage window had no
path to silent weight corruption at all, and the failure mode was always the honest kind: the next
load would fail outright.

The residual truth is narrower: the risk exists **during** a load, between `safe_open` and the
transfer to device memory, not for a model that has finished loading. Worth keeping as a caution,
worth not repeating as a discovery. The general lesson is the one I keep failing to apply -- the
confident mechanism story is exactly when to go look.


## Two checks I praised tonight that were checking the wrong thing

**"The blob's link count is 1, so there is no shared-hardlink hazard."** In a xet-backed HF cache
nothing is hardlinked, so link count is one for every blob and the check passes vacuously. The
sharing is done by **symlink indirection**: `models--<repo>/blobs/<sha256-oid>` is a symlink to
`hub/blobs/<xx>/<xetHash>`, and a sidecar `<xetHash>.refs` lists who depends on it. Reading that
file for the blob I damaged shows **two** dependent models:

```
  models--...--Qwen3.8-Flash-Next-EXL3-K4.25-v1/blobs/64db96ef9410...
  models--...--Qwen3.8-Flash-Next-EXL3-K4.25-PLE-FP8-v1/blobs/64db96ef9410...
```

So the blast radius of the `fio --size` damage was **two checkpoints, not one** -- the BF16 variant
shares the same CAS blob -- and my reassurance to the contrary was produced by a check that could
not have failed. A check that cannot fail is not a check.

**"Read the file to see what it is."** `cat` on a cache path follows the symlink, and the thing I
believed was a 79-byte pointer file was a 79-character *symlink target* into 8.6 GB of weight
bytes, which I duly streamed through a text filter into my own context. Check `stat -Lc %s` or
`file` before printing anything from a cache directory; the 79 bytes that looked like a file size
was a path length.

## The access pattern is not uniform, and that matters more than everything else in this document

Everything above this section reasons with `C/N` -- cache fraction equals hit rate -- which is exact
for uniform random access and is what every read-path cell I measured used, because the instrument
picks rows uniformly. The real pattern was never measured until now.

Port of the id math to numpy, **validated byte-identical against the engine's own CPU path** across
24 randomised batches (single- and multi-request, eos segmentation, long prompts). Config-derived
geometry reproduces the artifact from first principles: `ngram_size=3`, `heads_per_ngram=8` -> 16
heads, per-head primes 20,000,003..20,000,171, `padded_vocab_size = 320,001,536` = 128 shards x
2,500,012 rows exactly, `head_dim = 160`, **12,500,060 pages** (an earlier note of mine said
12,500,074). Tokenised with the served tokenizer (`tokenizer.json`, sha256 matching the published
oid) so the ids are real model tokens, not synthetic ones.

Hit rates below are **exact LRU from reuse distances**, computed by a C tool whose stack-distance
method was checked against an independent brute-force LRU (python, 30 cases, agreement exact; the C
tool agrees within 0.4-0.6%, the residual being its geometric capacity buckets).

| corpus | tokens | working set | LRU @ 6.18 GiB | LRU @ 17.2 GiB | uniform `C/N` prediction |
|---|---|---|---|---|---|
| python source | 9.95M | 32.1 GiB | 88.7% | 93.2% | 36.0% |
| JSON payloads | 3.45M | 7.6 GiB | 96.4% | 96.4% | 36.0% |
| docstrings/comments | 1.79M | 6.2 GiB | 94.3% | 94.3% | 36.0% |
| **serving-shaped session** | 2.28M | 14.7 GiB | **86.4%** | **89.4%** | 36.0% |

**The uniform model understated the achievable hit rate by a factor of about 2.5.** That is not a
tuning difference; it is the difference between a design that scrapes past its requirement and one
that has roughly an order of magnitude of margin. At the 170,126 pages/s hard end, ~86-91% hits
means the device carries 15-24k pages/s against a measured capability of 177-227k, and the binding
constraint moves entirely onto the submit path -- 16 pages per token issued by the gather, whatever
the hit rate.

### The miss rate decomposes almost entirely into first touches

Warming an empty cache is not the same question as caching well, and the numbers separate cleanly.
Second half of the session trace, cold process start into an empty cache:

| cache | cold-start LRU | warmed from the first half's hot set | warm-up value |
|---|---|---|---|
| 6.18 GiB | 85.2% | 86.6% | +1.4pp |
| 17.2 GiB | 86.1% | 91.1% | +5.0pp |
| 19.4 GiB (a 32 GiB host) | 86.1% | 91.5% | +5.4pp |

Caching the *entire* table from a cold start yields 86.1% -- because 13.9% of that half's accesses
are its own first touches. So there is an irreducible cold component, LRU is within a few points of
what any cache of that size can do, and the only lever left is warm-up, worth about 5 points at
best.

### Two features I planned that should not be built

**The hot list is dead.** My pre-registered gate asked whether the hottest 41% of pages carry
>= 55% of accesses; the answer is 97%, so by the letter of the rule I should build it. The rule was
measuring the wrong quantity -- it detects skew, and skew is enormous, but a hot list only earns
its keep by beating what a page cache already does at the *same capacity*:

| corpus | capacity | LRU | hot-list ceiling | gain |
|---|---|---|---|---|
| code | 6.18 GiB | 88.7% | 85.7% | **-3.0pp** |
| code | 17.2 GiB | 93.2% | 95.9% | +2.7pp |
| session | 6.18 GiB | 86.4% | 93.0% | +6.6pp |
| session | 17.2 GiB | 89.4% | 100.0% | +10.6pp |

Those "100%" ceilings are not a hot list working; they mean the working set is smaller than the
capacity, so *any* scheme that knew the set in advance would hit everything. LRU gets there without
knowing anything, and at 6.18 GiB on code a static list is actively worse than LRU. The remaining
gap is cold share, which is a warm-up question, not a list question. **Not building this.**

**Warm-up is mostly unnecessary, for a reason that has nothing to do with the trace.** The page
cache is system-wide and survives a process exit; restarting the engine does not empty it. The cold
numbers above describe a *host* reboot or real memory pressure, both of which the kernel then
refills on its own as traffic arrives. The one ordering constraint that genuinely matters is
upstream's: weight loading evicts the table, so the first prefill after a start runs cold -- and
the fix, if it is needed at all after the prototype measures it, is a bounded `WILLNEED` pass over
the previously hot *regions* after weights load, not a persisted id list.

### Retractions attached to this section

* **"prose is the hard case" (63.6% LRU).** Artifact of a 254,824-token corpus: 36.4% of its
  accesses are first touches simply because the trace is short. The same content type with 1.79M
  tokens reaches 94.3%. My first read of that number was as a property of English rather than of
  sample size.
* **Every hit rate quoted earlier in this document** (70.0%, 42.9%, 31.1%, the `C/N` agreement
  itself) describes the uniform probe, not the workload. Those cells remain valid as device and
  filesystem characterisation -- throughput ceilings, amplification, `O_DIRECT` behaviour, layout
  effects -- and those conclusions are unchanged. The hit-rate rows are now labelled as the null
  model they always were.
* **RAM sizing table.** If ~6.2 GiB of cache yields 86% and ~19.4 GiB yields 89-91%, the jump from
  32 GiB to 64 GiB is worth a few points, and ~20 GiB of RAM -- about 6 GiB of cache after the
  12.6 GiB engine floor -- is a plausible streaming target. That needs the prototype to confirm
  before it goes in a recommendation, but it is the opposite direction from where `C/N` pointed.

Honest limits: these are one-shot corpora of available text, not logged traffic. Model-generated
decode tokens are more repetitive than inputs, so output-side traffic should be *better*; multiple
concurrent users on diverse topics should be worse than a single session. The trace also credits
every speculative token MTP drafts, rejected ones included, which matches what the gather actually
fetches. Real prompt traffic from `gg` would replace the guesswork, and the prototype's own
instrumented runs can supply it.

## Layout as the cause now has a proper control, and `st_blocks` turns out to be a liar

The control that the destroyed file made impossible has been rebuilt from bytes I own: identical
FP8 content, written in the same minute, differing only in extent type.

| fixture | encoded extents | pcache cap4 pages/s | hits from device | `O_DIRECT` cache growth | dev/req |
|---|---|---|---|---|---|
| D-encoded | 4,642 | 160,045 | 2.7% | **+0.58 GiB** | 0.95 |
| E-nocow | 0 | **176,462** | **42.9%** | **+0.00 GiB** | 1.00 |

Same bytes, same recency, so content and placement are both held constant and layout is the only
variable. E-nocow landed at 42.9% against the pre-registered 40-43% band; `O_DIRECT` polluted the
cache by 0.58 GiB on the encoded file and not at all on the plain one; throughput differs by 10.3%.
The pre-registered falsifier -- "if D-encoded matches E-nocow, retract the finding" -- did not
fire. **The layout conclusion from earlier stands, now with the control it lacked.**

### `st_blocks` cannot see compression on this mount, so my disk-cost pricing is void

The compressibility control settled it: **1 GiB of zeros** -- the most compressible data that
exists -- reports `st_blocks` ratio **100.0%** while carrying 8,189 encoded extents (1 GiB / 8,189
= 128 KiB, exactly btrfs's compression block size, so compression is definitely being applied).
Therefore:

* `st_blocks` on this kernel/mount reflects logical size for compressed extents, and the
  "encoded extents save nothing" reading I built on it was an artifact of the metric, not a
  property of the data.
* So is the price I attached to `compress=no` -- "forfeits about 6.7 GiB" -- **unmeasured**. It may
  be roughly right (standalone zstd-1 does reach 86.0% at 128 KiB blocks, which matches the extent
  size) or it may be nothing; this mount's accounting cannot tell us either way.
* The tool that would answer it is `compsize` (fiemap-based, reports RAM size versus on-disk size
  per algorithm). Not installed, and installing needs root. Until then the honest statement in the
  deployment note is: *compression is on, it is costing retention and throughput as measured above,
  and its disk benefit on this filesystem is unverified by the instruments available here.*

### The disk question settled without root, and my price for it was five times too big

`compsize` is installed on both hosts but its `SEARCH_V2` ioctl needs privilege (`Operation not
permitted`), so I said it was unavailable -- asserting an instrument was missing without testing
the obvious alternative. The alternative is `df`: it reports bytes the filesystem actually
allocated, and it cannot inherit whatever `st_blocks` is doing. Allocation delta for 1 GiB written,
sampled 33 s apart so a btrfs transaction commit has landed, with `chattr +C` as the control:

| payload | compressed dir | NOCOW dir |
|---|---|---|
| 1 GiB of zeros | **3.4%** | 100.0% |
| 1 GiB of real FP8 weight bytes | **97.1%** | 100.0% |

Three conclusions. The method detects real savings (zeros -> 3.4%), so `st_blocks` is confirmed to
report logical size for compressed extents on this mount and every space claim built on it was
void. btrfs *is* compressing the encoded extents. And on the actual payload it saves **2.9%**, so
my "NOCOW forfeits about 6.7 GiB" figure overstates the cost by roughly five times: the real price
on a 47.684 GiB table is about **1.4 GiB**.

That leaves a genuine unexplained gap -- standalone zstd-1 at btrfs's own 128 KiB block size said
86.0% on a 256 MiB sample, while allocation says 97.1% over 1 GiB. The likeliest cause is that the
sample and the written region are not the same material: this shard holds no n-gram tensors at all,
and that is exactly what happened -- measured per window: 86.0% at offset 0, then **100.0% at 256,
512 and 768 MiB**. Whole-window zstd-1 over that 1 GiB returns 96.5%, and btrfs allocated 97.1%.
The instruments were both correct; my sample was not representative, because this shard opens with
one compressible quarter (it contains no n-gram tensors, so probably a scale or embedding tensor)
and the rest is genuinely incompressible.

The consequence is sharper than a pricing correction. `filefrag` reports 4,642 of 4,668 extents in
this file as encoded -- 99.4% -- so btrfs routed essentially the whole shard through the compression
path and gained 2.9% of disk, while the encoded-extent retention penalty applies across the whole
file: 42.9% hit rate plain versus 2.7% encoded, and 10.3% throughput. That is paying the cost of
compression on 99.4% of extents to collect a benefit on about a quarter of them. `compress=no` or
`chattr +C` on the checkpoint tree is not a close call, and it is now measured on both sides.

This is the third time tonight the sample was wrong while the instrument was right -- the 254k-token
prose corpus, the 256 MiB compression sample, and a uniform-random access model standing in for real
traffic -- which is a pattern worth remembering louder than any of the individual corrections.

That is the third time tonight a number I quoted confidently turned out to be an artifact of the
measuring stick rather than the thing measured -- after `diskstats` field indices and a
`du`-derived compression claim. The pattern is consistent enough to be a rule: when two of my
instruments disagree, one of them is lying, and the one I notice is not necessarily the culprit.

## The module's first GPU run: correct bytes, wrong timing, and the timing was the test's fault

`patches/qwen_stream_gather.py` driven by the real session trace against a verified 8 GiB FP8
fixture on `gg`, alongside the serving engine:

    gate: 0 of 4000 randomly sampled rows mismatch a direct read -> PASS
    fill:  p50  11.31 ms  p99  32.72 ms
    exposed against a 205 ms compute window: p50  11.74 ms  p99  33.13 ms   (5.73% of prefill)
    decode (16 rows): p50 298.9 us  p99 440.8 us

Prediction had been 0.2% of prefill and ~17 us decode, from the `burst` prototype. The correctness
half held immediately; the timing half failed for two reasons, one in the module and one in the
harness, and only one of them is about the design.

* **The pipeline never ran.** `submit()` stored into a single `_pending` slot, so submitting batch
  k+1 overwrote the handle for batch k and `wait()` dutifully waited for the batch just submitted.
  That is why `exposed ~= fill`: the measurement was correct and the thing it measured was a
  synchronous design. A one-slot queue cannot express "filling k+1 while the device still needs k",
  which is the entire premise of stage B. Pending fills are a FIFO now.
* **Decode paid thread-pool dispatch.** Fanning 16 rows across 24 threads to move 2.5 KiB measured
  299 us; small batches copy inline now.
* **What is left is environmental and is now instrumented.** Each 32,768-row batch touches on the
  order of 17,000 distinct pages to deliver 5.24 MiB of payload, so fill cost is dominated by page
  residency, and `gg` currently holds ~66 GiB of pinned host memory for the serving engine plus its
  own page cache. The self-test reports per-batch minor/major fault deltas and host/cgroup memory so
  the next run attributes the gap instead of arguing about it.

Method note. The inline small-batch path was first written with `bounds` rebound *after* the copy
call, which would have copied a `1/threads` slice into a stale arena. The GPU gate would have caught
that as a mysterious byte mismatch, so there is now a GPU-free test,
`scripts/test-ple-rowcopy.py`, that byte-compares **every** row rather than sampling, for the inline
path, the boundary, the fan-out path, the whole table, and scattered non-monotonic ids. Sampling is
what let the earlier 24x-underfilled buffer look plausible: `0 of 4000 sampled` passes on a build
that copies 1/24 of a batch if the sample lands in the copied part.

## The "real trace" is synthetic, and I have been calling it real all evening

Asked where `session.rows.bin` came from, the honest answer turned out to be a provenance I had never
checked. It is not logged traffic, not `burst`, not this session. `build2.py` produced it at 07:32:

* `text.code` -- 40 MB of Python source from the container image's `dist-packages`
* `text.prose2` -- docstrings and comments scraped from those same files with `tokenize`
* `text.json` -- 12 MB of JSON from the same tree
* `text.session` -- **2,600 pseudo-turns** assembled by slicing random windows (`random.seed(11)`) out
  of those three and interleaving prose/code/json, 8.4 MB total

That 8.4 MB was tokenized with the checkpoint's own tokenizer (2,279,111 tokens) and pushed through the
byte-identical numpy port of `compute_ngram_ids`, giving the 36,465,776 (token, head) row ids that every
"trace-driven" number in this file rests on. The instrument's own field says `access_model:
real-trace`, which meant only "driven from a file", and I read it as "driven by real workload".

Two specific ways this biases the result, pulling in opposite directions, which is why I am not
guessing a sign:

* **Optimistic.** The corpus contains **2,487 copies of the Apache licence header** and is drawn from a
  fixed ~80 MB pool, so late windows revisit pages touched early. Real traffic from different files,
  different repositories and different users has far more cold first touch -- and first touches are
  exactly what my miss decomposition showed the residual misses to be.
* **Pessimistic.** An agentic client resends its entire prior context on every request, so the same
  prefix -- and therefore the same n-gram ids -- is re-touched once per turn. A synthetic stream of
  mostly-novel windows cannot express that, and page caches love it. This is plausibly the largest
  single effect in coding-agent traffic and it is entirely absent here.

What this does and does not invalidate. The uniform-traffic results are untouched, because they never
used a trace: the CoW exoneration, the 15x BF16 encoded-extent cost, `O_DIRECT`'s amplification and
buffered fallback, the 25.6x page-versus-row copy amplification, the mapper/`fadvise` semantics, and the
streaming module's correctness gate. What is provisional is every **hit rate and every capacity number**
derived from the corpora -- 86.4% at 6.2 GiB, 0.797 at 16 GiB, and therefore the 32 GiB target floor
that sizing decision rests on. Those are properties of a synthetic mixture, not measurements of the
workload.

Mitigations now in place: the corpora are renamed `synth-*` on both hosts, the instrument records
`trace_file`, `trace_rows` and a `--trace-provenance` string in every receipt and flags undeclared
provenance, so a synthetic curve can no longer be mistaken for a measured one six hours later.

The fix is a capture, not an argument. The cheapest one that does not touch the serving engine is a
loopback packet capture on the inference port for a bounded window, parsed offline against the same
tokenizer -- no proxy in front of the thing doing this reasoning, no restart, no client reconfiguration.
It records conversation content, so it is the user's call, and it should be time-boxed because agentic
traffic re-sends whole contexts and fills disks quickly.

### With the synthetic trace the ratio holds at 15.6x, and only the amplification changed

Same blob pair, driven by `synth-session.rows.bin` with 320-byte rows, cold-started per cell.
`dedup_saving_frac` 0.444 confirms locality was actually present, so the cells are meaningful; device
volume fell in both arms, which was the pre-registered falsification check.

| 4 GiB cache | encoded original | plain NOCOW copy |
|---|---|---|
| device reads | **149.53 GiB** | **9.61 GiB** |
| amplification | 3.44x | 0.22x |
| pages/s | 117,964 -- **11% below** the 132,734 prefill target | **187,537 -- 41% above** target |
| hit rate | not meaningful (amplification > 1) | 0.779 |

Three of four predictions held: amplification fell from 8.13x toward 3.44x as locality reduced misses;
the plain arm's hit rate rose from 0.458 under uniform to 0.779; and the encoded arm remained far worse
per byte served. The fourth failed and it was the load-bearing one -- I predicted the 15x device-volume
ratio would narrow to 4-8x. It became 15.6x. The reasoning error was treating amplification and miss
rate as independent: locality shrinks the miss count in *both* arms by roughly the same factor, so the
per-miss cluster cost survives the change in traffic pattern. Amplification is a property of the extent
encoding, not of the access model.

The deployment statement is now sharper than the uniform result. Under a realistic reuse pattern at a
4 GiB cache, the plain-extent BF16 table **meets** the prefill read rate with 41% headroom and the
encoded one **misses it by 11%** -- the first cells all night where layout decides whether the
requirement is met at all rather than by how much. `O_DIRECT` over the encoded file was still charged
2.48x amplification, and over plain extents it was exactly 1.00x with zero cache growth, both below
target as before.

Caveats unchanged and inherited from the corpora: synthetic traffic, ids folded 12.8x into the blob's
row space, uniform in the ratio sense but not from a real workload. The synthetic basis inflates
locality, which if anything understates the encoded arm's disadvantage, since fewer real hits means more
misses at 3.44x each.

## The BF16 table on encoded extents costs 15x the device traffic, and my prediction was wrong in kind

Same bytes (`cmp`-verified, 8,013,268,456 both), same host, cold start enforced by `drop_caches` plus
`fadvise` plus the mapper check, uniform traffic, 20,480,000 row requests per cell over a 7.46 GiB blob,
8 threads, `--memory 4g` standing in for a small-RAM host. `manifest.txt` confirms each arm read the
file it was aimed at.

| cell | original (39,257 encoded extents) | NOCOW copy (0 encoded) | ratio |
|---|---|---|---|
| pcache, 4 GiB cap | **649.6 GiB** device, amp **8.13x**, 61,293 pages/s | **43.3 GiB** device, amp 0.54x, **149,687 pages/s** | **15.0x** traffic |
| pcache, uncapped | 18.3 GiB device, amp 0.23x, hits 0.771 | 7.5 GiB (one pass), amp 0.09x, hits **0.906** | 2.4x |
| odirect, 4 GiB cap | 425.1 GiB device, amp 5.32x, 83,906 pages/s | 79.9 GiB, amp 1.00x, 99,029 pages/s | 5.3x |
| odirect, uncapped | 43.3 GiB, amp 0.54x, **page cache grew 4.81 GiB** | 79.9 GiB, amp 1.00x, cache grew 0.00 | -- |

**The prediction was directionally right and conceptually wrong.** I predicted the encoded arm would
show ~2-3% hits -- a retention collapse -- and the plain arm ~42.7%. The plain arm came in at 45.8%,
within three points. The encoded arm returned a hit rate of **-7.13**, which is not a bad retention
number but a metric that has stopped meaning anything, because the device moved **8.13 pages for every
page requested**. The penalty is not only that compressed pages are poorly retained; it is that every
miss costs a whole compressed cluster, and under a cache cap misses are constant. Retention and
amplification multiply: 15x the device traffic, and 2.4x *slower* in aggregate (61.3k against 149.7k
pages/s) because the extra traffic is not free. The plain arm met the 132,734 pages/s prefill target at
a 4 GiB cache; the encoded arm delivered 46% of it.

Three secondary findings, each from an instrument finally asking the right question:

* **O_DIRECT over compressed extents is charged for amplification too** (5.32x, 425 GiB) and
  simultaneously grew the page cache by 4.81 GiB at 0.54x amplification uncapped -- direct I/O being
  partly served through the buffered path, exactly the substitution the `!direct-populated-cache` flag
  exists to catch. Over plain extents it honoured the flag perfectly: 1.00x, zero cache growth.
* **Uncapped, the encoded file is merely untidy** (18.3 GiB read for a 7.46 GiB file, hits 0.771 against
  0.906). The catastrophe needs the cache cap. That is worth stating precisely because the cap is the
  entire premise of the low-RAM work -- the penalty is a cache-pressure effect, not a constant tax.
* Run self-consistency held: 649.6 GiB over 342 s is 1.9 GiB/s of device reads, which the drive does
  sustain for cluster-sized requests, and the plain arm's uncapped cell read exactly one pass.

**Deployment consequence, and it is the first one tonight that changes an action.** For FP8, nothing:
its table bytes already sit in plain contiguous extents and the matched-region A/B differed by 0.2-0.8%.
For BF16, the plain layout is not optimisation but a requirement -- 650 GiB versus 43 GiB of device
reads for identical bytes is the difference between meeting the prefill read rate and delivering 46% of
it. So the `/home/rtx/ckpt-nocow` tree is the only place BF16 PLE should be tested or served on this
host, and my earlier "delete the copy" advice is withdrawn for the second time, now on evidence rather
than on the discovery that the data was already there.

## An A/B that compared a file against itself, and the two real numbers it left behind

The first BF16 layout A/B was void. The resolution script that was supposed to name the blob failed on
a path bug, so `BLOB` reached the matrix runner empty -- and the runner, rather than refusing, fell
back to `MODEL`, which defaults to the **serving container's own snapshot**, and picked the largest
shard in it. Both arms therefore measured `model-00019` and the comparison tool reported
"retention indistinguishable (<1%)" about a file against itself. The pre-existing guard only fires when
nothing resolves at all; the case that actually misleads is resolving successfully to the wrong thing.
The runner now warns loudly whenever `BLOB` is defaulted and writes `$OUT/manifest.txt` recording
`blob_requested` against `blob_used`, model, trace, caps, threads, steps, target, dev, precell and blob
size, so every receipt can be checked for what it actually describes.

Two measurements survive the void run, and both are worth more than the invalid conclusion:

* **Independent reproduction of the encoded-extent retention penalty.** `model-00019` at a 4 GiB cap
  measured **2.2% hits** here against **2.7%** in the original controlled pair, with amplification
  0.98x. Different session, cold-started by `drop_caches` rather than `fadvise` alone, same answer to
  within 0.5 points.
* **A third sample of the noise floor, and it separates by configuration.** Capped cells reproduced to
  0.034% on device pages and 0.18% on rate; the *uncapped* `pcache` cell's rate differed 5.29%, where
  the earlier self-pair gave 3.43%. So uncapped page-cache throughput on this host is a +-3-5%
  instrument while capped cells are tight to well under 1%. Any future claim of a few-percent
  throughput difference must come from capped cells or be repeated, and uncapped `pcache` rates should
  be treated as indicative only.

## The BF16 checkpoint is already on disk, and it is the pathological case in its entirety

Answering "does the NOCOW tree contain BF16 as well as FP8" turned up something I had been treating as
hypothetical for hours. Both cache trees hold two checkpoints:

| repo | n-gram table | where the bytes live |
|---|---|---|
| `EXL3-K4.25-PLE-FP8-v1` | 128 shards, `F8_E4M3`, 47.68 GiB | inside `models--…-PLE-FP8-v1/blobs/`, 74 GiB |
| `EXL3-K4.25-v1` | 128 shards, **`BF16`, 95.37 GiB** | shared `hub/blobs/`, 174 GiB -- the model directory is 10 MiB of symlinks |

The 174 + 74 = 247 GiB identity is what confirms nothing is double-counted, and it explains why the
BF16 directory looked empty at a glance. Checking extent layout on the blobs that hold the BF16 table:

| blob | original tree | NOCOW copy |
|---|---|---|
| `4abae2f51a5f45…` | 39,290 extents, 39,257 encoded | 2 extents, 0 encoded |
| `32e7cfaa8ade48…` | 61,043 extents, 61,039 encoded | 1 extent, 0 encoded |
| `bc2fe48ffe5df4…` | 61,043 extents, 61,035 encoded | 1 extent, 0 encoded |

So where the FP8 table's own byte ranges measured 0.0% encoded, the BF16 table is ~100% encoded and
shattered into sixty thousand fragments per 7.45 GiB file. Everything I had been arguing for BF16 by
inference -- from `embed_tokens` and `lm_head` measuring 100% inside encoded extents -- is directly
visible in the payload that would actually be served.

Scope note that keeps this from becoming an argument for restarting something tonight: nothing on `gg`
serves the BF16 variant. `gg` serves the FP8 PLE model, and the `K4.25-v1` weights in this cache are
what the `burst` engine runs -- its own tree, which is not in scope because that engine is this
session's reasoning backend. The NOCOW subvolume therefore already repairs 105 GiB of data that exists
and would be served badly, but it still does not justify touching the running service.

## The checkpoint tree is 707,486 extents; the NOCOW copy is 132 -- and the reason is one mechanism, not two

A second copy of the cache was made into a `chattr +C` subvolume on the same volume, content-verified
against the baseline (50/50 sha256, plus a full byte comparison of a 6.35 GiB blob). Measuring the
layout with `filefrag` field accounting rather than a regex:

| tree | extents across shard blobs | of which encoded |
|---|---|---|
| original | 707,486 | 706,770 |
| NOCOW copy | **132** | **0** |

`model-00001`, which holds n-gram shards 0, 1 and 10, goes from 21,777 extents (21,745 encoded) to 4.
The blobs that never compressed were already at 4-6 extents. I initially called this two independent
benefits -- encoded extents plus fragmentation -- and that was wrong: 99.9% of the extents were
encoded, so the fragmentation is compression's symptom rather than a second cost. The 256 MiB test
said so at the time: the compressing file shattered into 2,048 extents, the NOCOW file wrote in two.

Extent count versus bytes is also worth pinning, because both statements are true and they sound like
contradictions. In `model-00001` 21,745 of 21,777 extents are encoded, but those encoded extents carry
2.65 GiB of the file's 6.35 GiB; the n-gram shard ranges specifically measure 0.0% encoded because
FP8 bytes do not compress. So "almost every extent is encoded" and "the table's bytes are not in
encoded extents" describe different units of the same file.

**Retraction, instrument kind:** the "1000 extents" I reported earlier was not GNU grep lacking `\s`
-- `\s` works fine -- but `filefrag` right-aligning its extent index and emitting *no leading space*
from extent 1000 onward, so `^\s+[0-9]+:` silently matched only the first thousand. A round number
that equals a suspiciously familiar constant is a formatting boundary until proven otherwise.

## O_DIRECT over compressed extents is not O_DIRECT

The same comparison produced this in the original (compressed) arm:

    odirect-t8-host   117,775 pages/s  device 8,300,611  fs amp 0.75x  cache grew 2.66 GiB  !direct-populated-cache
    odirect-t8-host   (NOCOW copy) 97,736 pages/s  device 11,072,382  fs amp 1.00x  cache grew 0.00 GiB

The compressed file's "O_DIRECT" run *grew the page cache* and read fewer pages from the device than
were requested -- btrfs cannot service direct I/O over encoded extents and quietly routes those reads
through the buffered path. So yesterday's flat statement that O_DIRECT tops out at 85.6k pages/s needs
a qualifier: over a compressed file it looked *faster* (117.8k against 97.7k) precisely because it was
not doing what it was asked. Any O_DIRECT measurement on btrfs has to assert zero cache growth, which
is why the instrument flags it rather than reporting a rate.

**Retraction, conclusion kind:** "O_DIRECT cannot sustain the prefill page rate" survives only for
uncompressed data. Over compressed extents the number is not an O_DIRECT measurement at all.

## Levelling the page cache changes nothing, and that result costs me a claim I made yesterday

The `drop_caches` grant exists now (`/usr/local/bin/ple-drop-caches`, granted NOPASSWD to `rtx`,
wrapped so it refuses while the engine has requests and prints `Shmem` before and after so the pinned
table is visibly untouched: 66.18 -> 66.18 GiB while 16.11 GiB of file cache was released). It went
straight onto the assumption behind every hit-rate receipt in this file: cells start cold via
`posix_fadvise(DONTNEED)`, which provably fails to evict pages some process has mapped.

Same blob (incompressible, 8 GiB, CoW arm), same eight threads, same cap-4 and uncapped
configurations, run twice -- once cold-started by `fadvise` alone (how every earlier cell was run),
once with a host-wide `drop_caches` before each cell:

| cell | device pages, fadvise | device pages, drop_caches | difference | hit A | hit B | rate A | rate B | rate diff |
|---|---|---|---|---|---|---|---|---|
| pcache cap 4 | 12,005,164 | 12,008,313 | 0.026% | **0.427** | **0.427** | 174,490 | 174,551 | 0.04% |
| pcache uncapped | 2,099,281 | 2,099,344 | 0.003% | **0.900** | **0.900** | 220,149 | 227,703 | **3.43%** |
| odirect cap 4 | 20,951,519 | 20,955,016 | 0.017% | 0.000 | 0.000 | 139,665 | 139,711 | 0.03% |
| odirect uncapped | 20,951,655 | 20,954,633 | 0.014% | 0.000 | 0.000 | 140,918 | 140,701 | 0.15% |

Two conclusions, one of them a retraction.

**The prediction held: there is no cold-start bias.** Retention is indistinguishable, worst case
0.026% of device pages, and hit rates agree to the last reported digit. Nothing mapped these blobs,
so `fadvise` was already doing complete eviction -- which is exactly the condition the mapper guard
now enforces instead of trusting. Every `--require-cold` receipt in this file stands on its own
measurement, and the grant turns out to be for metadata tidiness rather than validity.

**Retraction: the 2-5% throughput differences I reported between the CoW and NOCOW arms are not
established.** I wrote that run-to-run reproducibility was excellent and therefore those deltas were
signal. The second column of this table says otherwise: two *identical* uncapped `pcache` arms
differed by **3.43%** in achieved pages/s. Any rate difference below roughly that is indistinguishable
from noise on this host, so the CoW-versus-NOCOW rate deltas (2.9-4.8%) sit inside the noise band and
must be reported as "no measurable difference" rather than as a small effect favouring NOCOW.

What survives intact is the part the layout argument actually rests on. Device-page counts reproduce
to 0.026% between arms, so the retention claims -- 42.7% versus 2.7% for plain versus encoded extents,
and the matched retention of CoW against NOCOW that exonerated copy-on-write -- are orders of
magnitude above the noise floor. Method lesson: a noise floor has to be measured by repeating one
arm, not by observing that two arms agree.

## The exclusion test: copy-on-write is innocent, so the layout advice collapses to nothing

The controlled D-versus-E pair varied three things at once -- CoW flag, extent count, and whether
extents were encoded -- which is why its 42.9%-versus-2.7% result proved nothing about cause. The
exclusion arms fix that by holding the payload incompressible, so btrfs declines to compress either
arm (verified: both `compsize 100%`, both zero encoded extents) and copy-on-write is the only
variable left. Uniform access, matched 8 GiB regions, 8 threads, 20,480,000 row requests per cell.

| cell | CoW arm pages/s | CoW device pages | NOCOW pages/s | NOCOW device pages | rate delta |
|---|---|---|---|---|---|
| pcache host | 208,731 | 2,097,075 | 216,862 | 2,097,030 | +3.9% |
| pcache cap 3 GiB | 163,630 | 14,498,906 | 168,396 | 14,497,132 | +2.9% |
| pcache cap 4 GiB | 181,426 | 12,001,706 | 178,540 | 12,002,263 | -1.6% |
| odirect host | 140,542 | 20,951,797 | 147,176 | 20,952,369 | +4.7% |
| odirect cap 3 GiB | 139,124 | 20,951,519 | 145,410 | 20,953,806 | +4.5% |
| odirect cap 4 GiB | 139,627 | 20,951,511 | 146,282 | 20,952,388 | +4.8% |

Device pages agree to within 0.005% in every paired cell -- page-cache *retention* is untouched by
the CoW flag -- and rate differences are 2-5% with mixed sign, largest on `O_DIRECT` where there is
no page cache involved at all. So:

* Copy-on-write costs a few percent of submit-path rate and nothing in retention. The pre-registered
  branch taken is "cow ~= nocow, therefore the D-versus-E gap was the encoded extents", which
  rehabilitates the original attribution by elimination rather than by direct measurement.
* Hit rates reproduce the earlier plain-arm numbers: 89.8% uncapped, 41.4% at 4 GiB, 29.2% at 3 GiB
  (region 8 GiB, ten passes). The encoded arm measured 2.7%. That gap is real, and it belongs to
  compression alone.
* **The practical conclusion is that none of this applies to the FP8 table.** It is stored
  uncompressed in contiguous extents, CoW costs ~0-5% and the only path where CoW showed a
  consistent penalty is `O_DIRECT`, which cannot sustain the prefill rate anyway (85.6k pages/s
  against a 132.7k requirement). So `compress=no` on the mount, and `chattr +C` on a checkpoint
  subvolume, both buy the FP8 table nothing measurable today.

Recommendation to the user's mount-options question, narrowed by evidence: do not remount and do not
re-lay-out for the FP8 checkpoint. Build the NOCOW subvolume when a **BF16** checkpoint is actually
going onto a target host -- BF16 tensors demonstrably compress (embed_tokens and lm_head measured at
100% inside encoded extents), so that payload inherits the 2.7%-versus-42% retention penalty for
real, and there the subvolume is worth the copy.

## One pass of a real trace, at full geometry, inside a capped container: the sizing model holds

A 47.684 GiB exact-geometry fixture, the session's real 36,465,776 (token, head) row ids,
`--evict-first --require-cold`, 8 threads, one full pass per cell, page granularity. Predictions
were written into the log before the first cell ran.

| cell | hit predicted | hit measured | device GiB predicted | device GiB measured | pages/s |
|---|---|---|---|---|---|
| pcache cap 8 | 0.75 +-0.04 | **0.744** | 18-22 | **19.02** | 155,345 |
| pcache cap 16 | 0.80 +-0.03 | **0.797** | 15-17 | **15.08** | 166,659 |
| odirect cap 8 | ~74 (all misses) | 0.000 | ~74 | **74.22** | 85,624 |
| odirect cap 16 | ~74 | 0.000 | ~74 | **74.22** | 85,583 |
| pcache, uncapped host | 0.80 +-0.03 | **0.797** | ~14.7 | **15.07** | 168,484 |
| odirect, uncapped host | 0.000 | **0.000** | ~74 | **74.22** | 85,834 |

All six cells landed inside their pre-registered bands. The comparison that matters for the design is
the first and fourth rows: **a 16 GiB cgroup cap measures the same hit rate as a 995 GiB host**
(0.797 both), and an 8 GiB cap is 5.3 points lower (0.744). Working-set size, not memory capacity,
is what governs this table -- which is the entire premise of serving a 47.684 GiB n-gram table on a
small-RAM machine. `O_DIRECT` reproduced its 74.22 GiB and 85.8k pages/s uncapped, confirming the
capped `O_DIRECT` numbers were the device and submit path, not the cgroup.

Every capped cell landed inside its predicted band, and `dedup_saving_frac` read 0.467 against the
0.475 measured independently by brute-force reuse counting. This is the first end-to-end
confirmation of the chain that the whole low-RAM design rests on: the offline exact-reuse-distance
hit-rate curve, derived from logged n-gram ids, predicts what the kernel page cache actually does
under a cgroup bound. A 47.684 GiB table behaves like a ~15 GiB table at these capacities.

Two results that change design choices rather than just confirming them:

* **`O_DIRECT` cannot sustain the prefill read rate.** 85.6k pages/s against a 132.7k target, in
  both capped configurations, with page-cache amplification 1.00x as expected -- it is not a
  misconfiguration, it is the device and submit path. Converted to workload terms at 8.56
  pages/token, that is roughly the prefill requirement with no headroom. Any streaming design that
  deliberately bypasses the page cache therefore gives up the one thing that makes a small-RAM host
  work: the ~80% of reads that cost nothing.
* **The page-cache path clears the target with margin at both caps** (155-167k pages/s, ~1.2-1.3x
  requirement), which is why stage B reads through the cache with row-granular copies instead of
  trying to be clever about bypassing it.

One incidental validation: the runner's `wait_idle` gate reported "engine busy (1 requests)" and
deferred a cell -- the requests were this session's own inference against the idle-looking `burst`
engine. The gate protected a measurement from the very workload I did not know was there.

## What the instruments got wrong while I was using them

Five defects in the read-path instrument, found in one hour by making each one capable of failing.
They are recorded together because they share a cause: an instrument that cannot disagree with me
reports whatever I expect.

1. **Trace mode had never run.** `TraceSource.take` used a bare `np` while numpy is imported lazily
   inside `__init__` -- a `NameError` before the first read. The smoke test existed precisely to
   catch this and did.
2. **A failed cell looked like a running one.** Worker threads were not daemons, so an exception in
   the step loop left them parked on `cv.wait()` and the process never exited. The container sat
   "Up 21 minutes" reporting nothing while its python had already printed a traceback. Any runner
   waiting on it would have waited forever, and I read that as a slow measurement.
3. **`--dev` was an unchecked string.** I passed `nvme1n1` for a file on `nvme0n1p3`. Device
   counters are the only thing separating a cache hit from a disk read, and a wrong device does not
   error -- it produces a confident wrong hit rate. The instrument now resolves the blob's backing
   device and refuses the cell on disagreement. Resolution goes through `/proc/self/mountinfo`,
   not `st_dev`: btrfs reports an anonymous device number with no `/sys/dev/block` entry, so the
   principled-looking implementation returned junk and refused *every* legitimate cell on a btrfs
   host until cross-checked against `df`. Two further off-by-ones in the same function (mount point
   versus root-within-device, source versus super-options) were caught the same way.
4. **The dedup saving was inflated 16x.** `rows_total` already counts token x head, and
   `dedup_saving_frac` divided by `rows_total * rows_per_token` -- reporting 96.7% where brute-force
   reuse counting says 47.5%. Corrected denominator gives 0.465, which agrees with the independent
   measurement. `measured_hit_rate` used distinct pages all along, so published hit rates stand.
5. **`--require-cold` was a claim about a state it could not always reach**, and I had asked for
   root to fix it on a premise that turned out to be false. Measured directly: with a live mapping,
   `posix_fadvise(DONTNEED)` leaves 65,536 resident pages at 65,536; with the mapping closed, the
   same call takes them to 0. `drop_caches` skips in-use pages too, so it would not have helped in
   the one case that mattered. The real fix needs no privilege -- scan `/proc/*/maps` for processes
   mapping the blob and refuse the cell. Matching there is by inode plus resolved path, not the
   device field: `maps` printed `00:23` for a file whose `st_dev` says minor 37, because two mounts
   cover the same tree, and a key that cannot match builds a guard that never fires.

Retraction: I reported a 96.7% page-dedup saving from the instrument before checking it against the
47.5% measured by brute force. The instrument was wrong; the earlier number was right.

## Two things about `burst` that change what can be measured there

* A **live vLLM engine has been running on `burst` for 3 days 16 hours** (container `qwen38-exl3`,
  model `EXL3-K4.25-v1`, port 8001), holding 91.7 GiB of 97.9 GiB device memory while reporting
  `num_requests_running = 0` and 0.4% CPU. GPU utilisation reads 100% with no requests in flight.
  Consequences: (i) the CUDA-side half of the streaming module's self-test cannot be timed there
  without contention, and (ii) any GPU-involving number already taken on `burst` was taken beside
  this process, so absolute latencies there carry that caveat even though the CPU- and disk-side
  comparisons were run identically in both arms.
* `burst` has **995 GiB of RAM**, so an uncapped cell there is effectively an unbounded-cache cell;
  the low-RAM question can only be answered through cgroup caps, not by the host default.

## The table is not compressed at all, so the layout thread was aimed at the wrong file

With working controls, the question the last three sections circle resolves simply. Extent coverage
measured against named tensor ranges inside the shards that hold the table:

| file | range | dtype | size | in encoded extents |
|---|---|---|---|---|
| model-00001 | `ngram_embedding.shard_0.weight` | F8_E4M3 | 381.5 MiB | **0.0%** |
| model-00001 | `ngram_embedding.shard_1.weight` | F8_E4M3 | 381.5 MiB | **0.0%** |
| model-00001 | `embed_tokens.weight` | BF16 | 1,212.5 MiB | **100.0%** |
| model-00001 | `lm_head.weight` | BF16 | 1,212.5 MiB | **100.0%** |
| model-00002 | `ngram_embedding.shard_100/101.weight` | F8_E4M3 | 381.5 MiB each | **0.0%** |

The BF16 rows are the positive control -- they prove the method detects compression -- and they sit
entirely inside encoded extents. The FP8 table rows sit entirely outside them. Combined with
compsize showing 13 of the 14 table-bearing files at `TOTAL 100%`, and with `model-00002` holding its
3.73 GiB in a **single contiguous extent**, the conclusion is that btrfs declines to compress FP8
weight bytes and the PLE table is already stored in the ideal layout: plain, uncompressed,
contiguous.

Three consequences, each of which undoes something I argued earlier.

* **`compress=no` and `chattr +C` buy the FP8 table nothing today.** There are no encoded extents
  over it to penalise. The controlled D-versus-E result (42.9% against 2.7% hits) is real, but it was
  measured on `model-00019`, which contains no n-gram tensors at all, and whose own layout is 7%
  encoded. The whole layout investigation has been about a penalty this payload does not carry.
* **The BF16 case is where the advice actually applies, and it is now predicted rather than
  guessed.** BF16 tensors demonstrably compress (embed_tokens and lm_head to ~79-86%) and land fully
  inside encoded extents. A BF16 PLE table would therefore hit the retention penalty for real, which
  is the opposite of what I wrote when I called the BF16 trade "flagged rather than guessed".
* **Extent geometry is now the leading hypothesis for the D-versus-E gap**, not compression, because
  the arms differed in CoW, extent count (5,337 against 65) and encoded-ness simultaneously. The
  incompressible-payload pair isolates exactly that, and since the table files are already one or a
  handful of contiguous extents, the practical exposure is small either way.

Method note, because this is how the contradiction got published in the first place: my earlier
"positive control" printed the *smallest* tensor -- the list was sorted ascending and I labelled
element zero "largest" -- so it read "largest n-gram tensor: 0 MiB" from a 2-byte `weight_scale`
entry and I concluded the mapping was broken when the data was fine. A control that is not checked
for being capable of passing is not a control.

## The streaming gather prototype: the design I was building was 143x more expensive than it needed to be

Built on `burst` (RTX PRO 6000 Blackwell, 96 GiB VRAM, 995 GiB RAM) against a 47.684 GiB fixture
with the exact real geometry, driven by the **actual session access trace** rather than a uniform
probe, inside capped containers.

**Page-granular staging is the wrong shape.** The first prototype fetched whole 4 KiB pages into
pinned staging, which copies 4,256 bytes to deliver a 160-byte row. For a 2048-token prefill batch
that is 53 MiB of copying to produce 5 MiB of rows:

| shape | page-granular `pread` | row-granular `memcpy` from a mapping | ratio |
|---|---|---|---|
| 1 token (16 rows) | 31.3 us | **2.7 us** | 11.6x |
| 16 tokens (256 rows) | 512.9 us | **3.5 us** | 146.7x |
| 2048 tokens (32,768 rows) | 34,685 us | **242.9 us** | 142.8x |

Arm C died because the *GPU* cannot dereference pageable host pages. Nothing prevents the *CPU*
from reading them, and a `memcpy` of 160 bytes from the mapping into packed pinned staging has no
page amplification and no per-row syscall. That is the shape the copy-based design should have.

**Row straddling is a real constraint, not a corner case.** The first version failed its own
correctness gate on 28 sampled rows because a 160-byte row starting above offset 3,936 inside a
page is split across two page slots and is therefore not contiguous in staging. Slots are now
`PAGE + ROW_BYTES` with overlapping reads (a 3.9% byte overhead) rather than special-casing the
~4% of rows that straddle. I had measured `pages_per_row` as 1.01-1.05 weeks ago; the geometry was
telling me this all along.

**Within-batch deduplication is load-bearing under real access.** It removes **47.5%** of requests
at 2048 tokens per batch (31.1% at 256, 57.5% at 8192). My earlier note that dedup "is worth 0.3%
on the real table" was measured under uniform access and is withdrawn: under the real trace it
halves the request count, which is a design-level effect, not a micro-optimisation.

**Cost by phase, steady state, 2048-token batch, 20 reader threads, 20 GiB cap:**

```
  copy phase (memcpy/pread with faults)   50.7 ms p50   <-- all of it
  host-to-device                           0.04 ms
  device gather + sync                     0.30 ms
  reader CPU                            0.56 cores
  device traffic                    15.2 GiB for 5.5 GiB of rows (2.8x)
  DECODE, one token (16 rows), same cache: 16.6 us p50, 25.4 us p99, zero device reads
```

Decode is a non-issue: 16.6 us against a step budget of tens of milliseconds, and 60,000
steps/s of headroom before any model compute. **Prefill is different in kind, but the number is not
what it looks like.** 50 ms of exposed copy time per 2048-token batch is +25% against the ~205 ms
of compute such a batch takes -- except that it runs at 0.56 cores, which means the machine is
waiting on page-fault latency with 20 outstanding requests, not short of CPU. The gather for batch
k+1 can run inside batch k's compute window, where it costs nothing measurable; upstream already
has the hook that makes this possible (`start_prefetch` runs the lookup while the previous decoder
layer executes). So the open question is no longer "is the copy too slow" but "is the pipeline deep
enough", which is a much smaller piece of engineering.

Predictions written before the run, scored after: at a 20 GiB cache I predicted ~89-91% hits and
about 12 GiB of device traffic; measured 85.8% and 15.2 GiB. At 8 GiB I predicted 86.4% and
15.4 GiB; measured 80.5% and 20.9 GiB. Right direction and right order of magnitude, consistently
optimistic by 4-8 points of hit rate -- the miss model deserves that correction before it goes into
a sizing recommendation.

**One measurement discarded rather than reported.** The first capped-vs-uncapped comparison showed
106 ms against 168 ms and I was about to attribute the difference to the cap. Both arms had moved
exactly 6,016 MiB of device traffic and charged exactly 5.97 GiB -- the 8 GiB cap was never binding,
because 250 batches only touch 5.97 GiB of distinct pages, and the difference was device variance.
The same run also initially reported uncapped numbers identical to capped ones because the host had
already cached the file from the earlier arm: the cross-cgroup free-hit effect measured that
morning, appearing as a result.

## The pipelined gather: 0.2% of prefill, even while reading cold from disk

The un-pipelined numbers above are an upper bound on the wrong quantity. In the engine the lookup
for batch k+1 runs while batch k is being computed -- upstream already exposes that hook as
`start_prefetch`, which exists precisely so the pinned lookup overlaps a decoder layer. Filled with
a persistent pool of 24 threads doing C-side `memcpy` from a read-only mapping into double-buffered
pinned staging, one batch ahead, on a 47.7 GiB fixture under a 20 GiB cgroup cap:

| condition | fill per 2048-token batch | **exposed cost** (p50) | exposed (p99) | device traffic |
|---|---|---|---|---|
| cold file, 24 threads | 34.75 ms | **0.46 ms = 0.22% of prefill** | 0.79 ms = 0.39% | 9.9 GiB |
| warm cache, 24 threads | 1.28 ms | 0.40 ms = 0.20% | 0.81 ms = 0.39% | 0.0 GiB |
| cold file, 8 threads | 133.40 ms | 0.40 ms = 0.20% | 54.76 ms = 26.7% | 6.6 GiB |
| per-row `pread` from Python, 24 threads | 223.97 ms | 19.78 ms = 9.7% | 23.89 ms | 0.0 GiB |

The fill rate does not have to be fast; it has to fit inside the compute window, and 35 ms fits
inside ~205 ms. Thread count matters only for the tail: at 8 threads a cold start produces a 27%
p99, at 24 threads 0.39%. And **the implementation language matters more than the mechanism** --
calling `pread` per row from Python is GIL-bound and gets *slower* with more threads (156 ms at 8,
224 ms at 24), while one `ctypes` call per slice releases the GIL and scales to 3,894 MiB/s. If the
engine's staging fill is written in Python it will be the bottleneck; if it is one C call per
thread slice it is free.

Two caveats on these numbers. The 205 ms compute window is `gg`'s measured prefill rate applied to
this batch shape, not a `burst` measurement -- this table's exposed cost is a ratio against a real
number from another machine. And the gate that validates all of it samples 2,000 random rows against
direct positional reads; the version before it checked only row 0, passed unconditionally, and
produced a confident 1.9% figure from code that was reading the wrong bytes.

### Where this leaves the design

Everything now has a measurement attached. Decode: 16.6 us p50 with a warm cache, and a 1.4 ms p99
when the cache is smaller than the working set, against a step budget of tens of milliseconds.
Prefill: 0.2% exposed with a one-batch pipeline and 24 threads. Working set: 14.7 GiB for a
serving-shaped session, so a 32 GiB host's ~19.4 GiB of cache clears it and 8 GiB does not.
Hit rate: 86-94% from exact reuse distances, versus the 36-41% the uniform model predicted.
Requests: 47.5% removed by within-batch dedup. Layout: must be non-encoded -- 42.9% versus 2.7%
hits in the controlled comparison. What is left is wiring, not feasibility: `PLE_STREAM=1` in the
engine, the cgroup-aware gate, and a byte-identical-output check against the pinned path.

## Stage B met the engine, and the engine changed the design

The wiring was right. The failure traceback ran `ple_layer.start_prefetch` →
`ngram_embedding.start_prefetch` → `_lookup_on_current_stream` → my `_lookup` → `stream_lookup`, which
is exactly the intended path, on a real engine start with the real checkpoint. The error was not a
plumbing bug:

```
RuntimeError: Cannot copy between CPU and CUDA tensors during CUDA graph capture unless the CPU
tensor is pinned. Please use tensor.pin_memory() or allocate the tensor...
```

and the frame that raised it was **the D2H of n-gram ids**, not the H2D of rows I had been worrying
about. The deeper line was `breakable_cudagraph.py:105 return fn(*args, **kwargs)` -- the
`@eager_break_during_capture` decorator on `start_prefetch` had passed straight through, because
`is_breakable_cudagraph_enabled()` reads `VLLM_USE_BREAKABLE_CUDAGRAPH`, which was unset, in which case
the decorator returns the *undecorated* function. So the mapped backend's host-prefetch design already
assumed breakable graphs; my streaming backend needs them for a stronger reason.

**Following that error message would have produced silently wrong output.** It suggests pinning the CPU
tensor, which makes the *copy* capturable. The row gather around it cannot be capturable in any form:
it is `ctypes.memmove` out of the page cache, and CUDA cannot see a ctypes call, so a capture reaching
this function would record the graph *around* the host work, replay it with no host participation, and
serve the same frozen rows every step -- whatever ids the profiling batch happened to use. Not an error,
a wrong answer. Hence a runtime invariant instead of a config assumption:

```python
if torch.cuda.is_current_stream_capturing():
    raise RuntimeError("PLE streaming gather was reached inside a CUDA graph capture. ...")
```

which is necessary because the decorator has a branch that deliberately does *not* break --
`if mode == CUDAGraphMode.FULL: return fn(...)`. What makes breakable graphs safe is stated in that
module's own docstring: the captured artifact is "the bound `CUDAGraph.replay` for graph segments, **or
the user fn for eager segments** -- replayed in order at inference time", and "eager segments must
operate on the same static buffers used during capture". The prefetch buffer is static and passed in by
the caller, so the streaming lookup satisfies the second half already.

`PLE_STREAM=1` therefore implies `VLLM_USE_BREAKABLE_CUDAGRAPH=1`, asserted against the real file body
(unset → 1, explicit 0 respected, explicit 1 kept, `PLE_STREAM=0` leaves it alone).

**And it splits the throughput question in two.** Enabling breakable graphs sets
`compilation_config.mode = CompilationMode.NONE`. So a streaming-vs-pinned throughput delta measured
across different graph modes is measuring both things at once. The comparison has to be
{pinned, streaming} × {default graphs, breakable graphs}, and any single-difference number is not
attributable to streaming. This is the trap I set for myself immediately after fixing the last one, and
the only reason I did not fall into it is that reading *why* the gate exists turned up the side effect.

### Corrections to my own process this round

6. **The restore trap destroyed the evidence it was supposed to preserve.** `restore()` began with
   `podman rm -f ple-stream-test`; `podman logs` had already come back empty at the point of failure
   detection, so by the time I wanted the traceback the container was gone and I had to re-run a full
   engine stop/start to get 470 lines of log. Archive (`logs` + `inspect` for exit code and OOM flag)
   *before* any cleanup. Both restore attempts did land the host back in its serving state -- health
   200, greedy sha `f91132436fb09e45`, Shmem 66.18 GiB, VRAM 91,992 MiB -- so the failure was purely
   epistemic, which is the more expensive kind.

7. **A commit message claimed work that had not been written** (fourth instance of this pattern). The
   python block editing `start.sh` died on an unterminated triple-quoted string; the commit still ran
   because the failing block was the second of two in one shell invocation, and the message described
   the `start.sh` change as implemented. Fixed in a follow-up commit that states the message was wrong,
   rather than amended, so the wrong claim stays visible. `bash -n` also caught a stray `fi` that the
   replacement introduced -- the anchor already ended with one.

8. **My test harness for the implication logic was wrong before the code was.** "FAIL start=unset →
   got unset" looked like a bug in `start.sh`; in fact the extracted `if` block was being run with
   `PLE_STREAM` unset, so the branch correctly did nothing. An assertion that cannot distinguish "code
   is wrong" from "precondition missing" is not a test. The same call also printed an if/fi "imbalance"
   (13 vs 9) from an `awk` heuristic that counts `]; then` inside `[[ ]]` -- noise; `bash -n` is the
   authority and it passed.

9. **Nested heredocs inside `ssh '…'` keep breaking**, three times in one session, always the same way:
   `\\'` or `"` terminating a string early. The fix that actually works is writing the script to a file
   locally, `scp`, then `bash`; the fix I keep attempting is more escaping.

### Two facts about the deployed baseline that changed my arithmetic

The serving container is `localhost/qwen38-rtx:local`, in which **`qwen_pinned_host` does not exist** --
`find_spec` returns None. So its `PLE_PIN_STRATEGY=round2` is not a preference that image honours: it
torch-rounds the 47.684 GiB FP8 table to a **64.000 GiB** pin. The exact-size work is in
`localhost/qwen38-exactpin:local`, which is not what is serving. That is a ~16.3 GiB host-RAM saving
available today by switching image, with none of the streaming work involved.

It also invalidated my own pre-registered acceptance number. I had written "Shmem should fall to
~18.5 GiB", derived by subtracting 47.684 from the observed 66.18. But 66.18 decomposes two ways and
the total cannot tell them apart: 64.000 + 2.18 (round2) or 47.684 + 18.50 (exact). The container env
settles it -- round2 -- so under the actual deployed baseline, streaming should take Shmem to **~2.2
GiB**. The check's threshold would have reached the right verdict either way, which is exactly why the
printed expectation needed correcting rather than leaving a check whose reasoning I could not reproduce.

## Two numbers I had wrong while writing the streaming gate

**`ple_embed_dim` is 2560, not 160.** 160 is one head's row bytes. The table is 16 heads
(`(ngram_size-1) x heads_per_ngram = (3-1) x 8`) of ~20,000,000 rows apiece, so the checkpoint reads as
320,001,536 rows x 160 B -- and 16 x 160 = 2560, which means the deployed gate's `base x dim x bytes`
formula and the checkpoint's `rows x row_bytes` are the *same number*, not a 16x discrepancy. I read the
gate as buggy, went to check, and found the two forms agree exactly at 47.68 GiB (FP8). The lesson is the
same one as the `--dev` resolver: a plausible arithmetic objection is worth five minutes of checking
before it becomes a refactor.

**The head count cancels out of the staging arena.** rows = tokens x heads, row_bytes = dim/heads x
bytes, so arena bytes = tokens x dim x bytes regardless of how the table is split -- 5 MiB at 2048
tokens, FP8. Printing it as GiB rendered "0.0 GiB", which reads like a bug in the sizing rather than a
rounding artifact; it is in MiB now.

## What the streaming preflight is for, and how it was tested

`PLE_STREAM=1` deletes a table-sized pin; it does not delete the memory requirement. The requirement
moves into page cache, so `qwen38_ple_stream_check` refuses below a 2 GiB cache floor and warns below the
16 GiB point where the measured hit rate plateaus -- with the warning naming its own provenance as
synthetic, and a test case asserting that it does. `qwen38_cgroup_avail_kib` clips `MemAvailable` by
cgroup headroom because `/proc/meminfo` is not namespaced; that number is an upper bound on reclaimable
space, not a promise, since committed anon memory is not reclaimable and a page-cache overrun causes
reclaim rather than an OOM kill.

`scripts/test-ple-stream-gate.sh` runs 10 cases and was mutation-tested four ways -- refusal disabled,
cgroup clip ignored, warn band widened, table size divided by 16 -- each of which produced failures, so
the suite is capable of disagreeing. Then the live host disagreed with *the message*: refusing a 3 GiB
cgroup, it explained that 3.0 GiB was "less than the 2.0 GiB floor". The comparison is pin + floor; the
text printed only the floor. A refusal whose arithmetic the reader cannot reconcile is a refusal people
work around with `QWEN38_SKIP_MEMCHECK`, so it now prints both terms and their sum, and the three live
cases (3 GiB cgroup refuses, 14 GiB warns, 60 GiB accepts) were re-run against the real checkpoint config
with `rc` captured directly -- not through a pipe, which is what reported `rc=0` for a refusal a moment
earlier.

## Breakable graphs are necessary and not sufficient — decode captures FULL

Attempt two failed on my own guard, which is the guard doing its job: the log said
`Breakable CUDA graph enabled` and the host gather *still* reached a capture. I had written the guard
message claiming `VLLM_USE_BREAKABLE_CUDAGRAPH=1` was the remedy, and the engine disproved it in about
three minutes.

There are two capture paths, and only one of them breaks:

```python
self.use_breakable_cg = (is_breakable_cudagraph_enabled()
                         and self.cudagraph_mode.has_piecewise_cudagraphs())
```

with the default mode `FULL_AND_PIECEWISE`. In `cudagraph_utils.capture()`, a `PIECEWISE` descriptor
either runs uncaptured or `continue`s before the graph block; every other descriptor reaches

```python
with torch.cuda.graph(graph, self.pool, stream=self._capture_stream(desc)):
    forward_fn(CUDAGraphMode.NONE)          # line 478 — this is the FULL path
```

and inside a FULL capture `eager_break_during_capture` *deliberately* does not break:
`if mode == CUDAGraphMode.FULL: return fn(...)`. So prefill was already safe and **decode was not**.
That is the precise shape of bug a single successful startup would have missed, and it is the second
time tonight that "the host-side gather is fine under graphs" needed to be checked per graph mode rather
than per flag.

Fix: `PLE_STREAM=1` also implies `--compilation-config '{"cudagraph_mode": "PIECEWISE"}'`, unless the
caller supplied their own compilation config, in which case `start.sh` says so and lets the guard refuse.
`PIECEWISE` survives the engine's own normalisation only because breakable is on —
`_normalize_piecewise_cudagraph_mode` rewrites `PIECEWISE → NONE` when compilation is off *and* breakable
is disabled, and streaming forces compilation mode to `NONE` as a side effect of enabling breakable. The
two flags are load-bearing for each other.

The cost is real and belongs to the feature, not to a footnote: dropping FULL graphs means decode replays
piecewise segments instead of one whole-step graph. That is part of what streaming costs on this engine,
and it is one of the reasons the comparison has to be a 2×2 rather than a pair.

Both prior attempts restored the serving engine cleanly — health 200, greedy `f91132436fb09e45`, Shmem
66.18 GiB, VRAM 91,992 MiB — and both failures were archived before cleanup, unlike the first one.

# Stage B works: the FP8 table served by CPU row copy with 62 GiB less pinned memory

Third attempt started, became healthy at 170 s, and passed the bar.

| | pinned (deployed) | `PLE_STREAM=1` |
|---|---|---|
| `Shmem` | 66.18 GiB | **4.43 GiB** |
| `MemAvailable` | 50.14 GiB | **109.88 GiB** |
| `Cached` | 69.24 GiB | 82.74 GiB |
| VRAM used | 91,992 MiB | 91,512 MiB |
| greedy output | `f91132436fb09e45` | `f91132436fb09e45` |
| device sectors, warm greedy burst | — | **392 (=196 KiB)** |
| capture-copy errors | 0 | 0 |

The memory prediction held to the arithmetic rather than to luck. The deployed engine pins
`PowerOf2Ceil(47.684 GiB) = 64.000 GiB` for the table; streaming pins only the token embedding,
`PowerOf2Ceil(248320 x 2560 x 2 B) = 2.000 GiB`, plus ~2.18 GiB of unrelated shmem → expected ~4.2 GiB,
observed **4.43 GiB**. Freed: ~61.7 GiB of pinned host memory, and 59.7 GiB more `MemAvailable`, which is
what the page cache then spends: 82.74 GiB cached against a 47.7 GiB table, and a warm burst touching the
device for 196 KiB. That is the entire point of the feature, measured.

The 0.25 GiB gap between predicted and observed is not chased; it is inside the resolution of "rounded
embedding pin plus unrelated shmem", and claiming it away would be dressing the model to fit.

## Greedy output on prompts over 2048 tokens is not reproducible on this engine

Found while trying to *strengthen* the correctness bar, and independent of streaming. Two runs of the same
six-prompt probe on the unchanged pinned engine produced different aggregate hashes. Per-prompt hashing
over three runs isolated it to exactly one prompt — the 2,222-token one, the only prompt above
`--max-num-batched-tokens 2048`. Then:

- Five back-to-back identical calls of one 2,110-token prompt → **three distinct completions**, with
  nothing interleaved. So it is not scheduling or interleaving.
- `vllm:prefix_cache_queries_total` 103,379 with `hits_total` 0 → **prefix caching is not the mechanism**.
- 1,510 / 1,610 / 1,710 / 1,860 / 2,010 tokens: stable. 2,310 / 2,610 (needing chunked prefill): flip.
- 4,010 tokens looked stable over three runs, so the "small final chunk" hypothesis is **not**
  established. The boundary the data supports is "prompt requires chunked prefill".

Consequences, both acted on. The strict gate hashes only single-chunk prompts (five of six here); the
long one is hashed into a separate `all` value and reported without gating — a prompt that cannot
reproduce itself can produce a false failure *or* a false pass. And it puts an earlier conclusion in
doubt: a previous session attributed non-reproducible greedy output to the PLE side stream ("identical
prompts matched in 2 of 8"). If those prompts exceeded 2048 tokens, this effect is a candidate cause and
that attribution needs re-testing before it is relied on again.

`scripts/ple-probe.py` records all of this in its own docstring, because the next person to see a
mismatched hash needs to know which half is meaningful.

## A stray `bisect.py` in `/home/rtx` silently breaks python there

The throughput probe died with `ModuleNotFoundError: No module named 'torch'` while importing
`urllib.request`. Python puts the cwd on `sys.path`, `urllib.request` imports `bisect`, and
`/home/rtx/bisect.py` shadows the stdlib module — and that file imports torch. Nothing about the engine
was involved. Probes now run `cd /tmp && python3 -I`, which is stated in the driver comment so it does
not get "simplified" back later.

# The plan already told me not to do this, and I did it anyway

`docs/gg-variant-benchmark-plan.md`, written at 00:49 CDT today — hours before I designed the streaming
lookup — contains a section "CUDA graph cost, and why it did not gate streaming" whose conclusion is:

> **It does not gate the design, because the design should not split the graph.** ... the gather belongs
> before replay, writing into a fixed-capacity pinned staging buffer backed by a persistent device
> buffer, exactly the pattern `PagePrefetcher` already uses for its own inputs ... The graph stays whole
> and the gather overlaps the GPU executing the previous step.
>
> Piecewise graphs ... may not be necessary here at all — which removes the largest implementation risk
> identified earlier.

I built the opposite: the gather runs *inside* `start_prefetch`, which forced me into breakable graphs,
which forced `cudagraph_mode=PIECEWISE`, which gives up whole-step FULL graphs for decode. The
`ENFORCE_EAGER` numbers in that same section show how expensive throwing graphs away can be on this stack
(−85.8% at c1, −66.4% at c16), which is why the size of the PIECEWISE penalty cannot be guessed at. Two
things follow, and only one is technical.

The process one: I had a written conclusion in the repo that named the implementation risk I then spent
tonight paying for, and I did not read it before designing. The reason the section is right is that it
reasons from where the *inputs* live — `input_ids`, `query_start_loc`, `ngram_context` are all known on
the host before the step replays, so the gather has no business being inside the captured region at all.

The technical one: that architecture is available and my pieces fit it. The host row copy already exists
and is proven; a persistent pinned arena plus a persistent device staging buffer, filled with one
`copy_(non_blocking=True)` issued *between* steps, makes the in-graph work pure index arithmetic over
DMA-capable device memory — capturable in a FULL graph, no breakable mode, no `PLE_STREAM` implication of
`VLLM_USE_BREAKABLE_CUDAGRAPH` or `PIECEWISE`, and it also deletes the per-step ids D2H that is v1's
known cost. The device-side gather kernel already exists for the mapped backend; what changes is which
addresses it is handed.

## What the 2×2 decides, stated before it runs

The `pinned-breakable-pw` cell is the important one and it has nothing to do with streaming: it is the
deployed backend with streaming's graph regime, so its delta against `pinned-default` *is* the price of
v1's graph requirement. Decision rule, registered now:

- pinned under PIECEWISE within the noise of pinned-default → v1's graph cost is not real; ship v1,
  the pre-replay redesign is an optimisation for the ids D2H only.
- pinned under PIECEWISE loses materially (the eager measurements suggest it might) → v1 is the wrong
  architecture regardless of how well it works; build the pre-replay staging path and keep FULL graphs.

Either way the streaming row copy is reusable. What is not reusable is the assumption that a per-step host
gather has to live inside the model forward.

The `stream-eager` cell is expected to be bad and is kept only as a documented fallback; the eager
measurement above is already its third data point.

# The matrix that booted nothing, and the five defects it surfaced first

All eight cells were rejected in seconds. Nothing was measured, and the host came back healthy — but the
cause was not the engine, and the fix chain turned into a small catalogue of ways a harness can be wrong.

**1. My run-line generator corrupted container arguments.** `create_command()` asked podman to
`{{join .Config.CreateCommand " "}}` and then `shlex.split()`ed the result. `CreateCommand` is an argv
*list* with no quoting metadata; `shlex.split` *parses* quoting. So
`--hf-overrides '{"text_config":{"rope_type":"yarn",...}}'` came back with every double quote removed, and
the container died on its own command line: `vllm serve: error: argument --hf-overrides: Value {...}
cannot be converted to <function loads>`. That reads like "podman rejected my flags" and is nothing of the
kind. Fixed by reading `{{json .Config.CreateCommand}}`.

**2. The dry run passed all four cells anyway.** It asserted flag positions — nothing podman-level after
the image, env before the image — and every one of those assertions was true while the arguments were
semantically wrecked. Structure was never the problem. The check that catches this class compares *content*:
the generated container argv must be byte-identical to the base container's plus the requested additions,
and each JSON-valued argument must still parse as JSON.

**3. `-d` was stripped and never re-added.** `build()` removes the base line's `--detach` so the driver
controls run mode, and nothing put it back — so cells would have run in the foreground and hung the health
loop, which is a much worse failure than a fast rejection. Adding it also exposed a second bug of the classic
kind: inserting a token invalidates an index computed before the insertion.

**4. Two locators for one grammar.** The new content check carried its own "find the image" logic and found
the *model path* instead, so a correct line failed the check with "container argv token 0 changed". Same
failure family as the duplicated `--dev` resolver: two implementations of one inference, disagreeing. Now
one function, `find_image_index()`, used by both paths. And the podman-flag scan walked past the image into
container argv, reporting `--port`'s value and the model path as "lost arguments" — a check that refuses
correct input teaches you to delete it.

**5. A cold cell measures ~2x low.** The first real smoke cell booted, became healthy at 290 s, reproduced
the strict hash exactly — and reported 8,205 tok/s prefill against 17,000-ish for the same pinned config,
while decode agreed to 0.5%. Kernel selection and autotuning are cold. Every timed shape now discards two
requests first; without that, the matrix measures how recently each cell booted.

## The instrument claim I had to take back within one message

I wrote that prefill had "a 2.2× spread on one configuration" and was therefore unusable. Wrong: the swing
was 8.2k → 11k → 17k because during a rewrite I dropped a `[:14000]` truncation on the prefill prompt,
changing it from ~3,500 to ~7,900 tokens. I was comparing two measurements of two different things and
reaching for "noise" to explain it. With the definition held constant, spread over four back-to-back runs is
16.2k–18.3k, about 13%, and driving the engine from a second client did not lower it.

So the resolution limits for this matrix, stated before reading it: **decode** is a real instrument
(163.4–164.6 tok/s across every run tonight, under 1%), and decode is precisely where FULL-vs-PIECEWISE
graphs should bite. **Prefill** at median-of-3 resolves something like 7–10% and nothing finer. A prefill
difference under that is not a finding. The timed lines now print their own shape (prompt tokens, chars,
reps, warmup) so a silent redefinition shows up in the data rather than in my memory.

Smaller notes: an argparse help string containing a bare `%` raises at `add_argument` time, which
`py_compile` cannot see; and my habit of piping runs through `grep '^prefill'` hid that traceback entirely,
making a crashed probe look like a silent one — the same shape as the "failed cell looked like a running
one" defect from the read-path instrument. Both now checked by importing the module and reading `tail`.

# The 2×2 result: v1 costs 22% of decode, and most of it is the graphs, not the streaming

Eight cells, two repeats, every cell correctness-clean — `strict a6cc8fed286b7071` in all eight, including
both streaming cells and both graph regimes, so five single-chunk greedy completions are byte-identical
between the pinned backend and the streaming backend. Medians, with each cell's own spread:

| cell | prefill tok/s (spread) | decode tok/s (spread) | Shmem | device reads, ~16 reqs |
|---|---|---|---|---|
| `pinned-default` | 18,211 (0.1%) | **162.8** (1.1%) | 66.18 GiB | 3.5 MiB |
| `pinned-breakable-pw` | 17,517 (0.1%) | **133.9** (1.6%) | 68.43 GiB | 3.3 MiB |
| `stream-breakable-pw` | 15,382 (0.7%) | 126.7 (3.5%) | **4.44 GiB** | **46.7 MiB** |
| `stream-eager` | 15,694 (0.9%) | **26.0** (2.7%) | 4.44 GiB | 3.4 MiB |

**Pair 1 — the graph regime alone, with streaming OFF.** Decode 162.8 → 133.9 = **−17.8%**, against
spreads of 1.1% and 1.6%. Prefill −3.8% against spreads of 0.1%. This cell was the pre-registered decision
test and it is unambiguous: giving up FULL cudagraphs costs ~18% of decode by itself, before any streaming
work happens at all. The rule I wrote before the run — *material delta means v1 is the wrong architecture
however well it passes* — therefore fires. The plan document said so at 00:49 and I did not listen.

**Pair 2 — streaming's own cost at matched graph mode.** Decode −5.4% (1.6%/3.5% spreads: borderline, the
only number in this table I would not quote alone) and prefill **−12.2%** (0.1%/0.7%: solid). That −12.2% is
far more than the prototype's ~0.5% exposed fill, and the reason is v1's shape rather than the I/O: ids go
to the host synchronously, the gather runs inline in the model forward, and the H2D is `non_blocking=False`.
Serialized, not overlapped. Both problems disappear in the pre-replay design.

**Total as built**: decode −22.2%, prefill −15.5%, in exchange for 61.7 GiB of pinned host memory. Note the
prefill comparison is streaming's *best case*: page cache held 82–84 GiB here, so nearly every row was a
cache hit and the device saw 46.7 MiB across the probe. On a 32 GiB host the same table gets 0.744–0.797
hits, so this −12.2% is a floor, not an estimate.

## A three-cell coincidence that explains an anomaly I had filed as unexplained

Decomposing `Shmem` against known pins (table `PowerOf2Ceil(47.684) = 64.000`, token embedding `2.000`):

| cell | Shmem | table | embed | other |
|---|---|---|---|---|
| pinned-default (FULL graphs) | 66.18 | 64.00 | 2.00 | **0.18** |
| pinned-breakable-pw | 68.43 | 64.00 | 2.00 | **2.43** |
| stream-breakable-pw | 4.44 | 0.00 | 2.00 | **2.44** |
| stream-eager | 4.44 | 0.00 | 2.00 | **2.44** |

`other` is 0.18 GiB with FULL graphs and 2.43–2.44 GiB in *both* non-FULL modes, across two different
backends. That is the +2.25 GiB I saw under `ENFORCE_EAGER` earlier and could not account for — now
reproduced independently, in breakable mode, on the pinned backend too. So the streaming Shmem of 4.44 is
not mysterious: 2.00 embed + ~2.25 non-FULL-graph surcharge + 0.18 ordinary. It also yields a falsifiable
prediction for the pre-replay design, which keeps FULL graphs: **streaming should land at ~2.18 GiB, not
4.44**. Correlation across three cells and two backends is not a mechanism, so I am not claiming one.

## Ordering effect I should have expected

`stream-eager` did 3.4 MiB of device reads while `stream-breakable-pw` did 46.7 MiB, both streaming, both
with the table unpinned. The eager cell ran after the breakable cell, so the table was already resident from
its predecessor's faults. Cells are not I/O-independent when they share a page cache; the first streaming
cell pays for everyone. Reading those two numbers as a pair would have produced a 14x "eager reads less"
result that is pure sequencing.

# v2 as built: staging in `prepare_inputs`, and why that place in particular

`patches/qwen_ple_stream2.py`, wired by `port-ple-stream.py` into three installed files. One sentence of
justification carries the whole design: **`prepare_inputs` is where `input_ids` themselves become fresh for
the step**, so staging rows there inherits the freshness property the engine already guarantees, instead of
needing a new argument for it.

Why the page prefetcher — which runs one line earlier and already has host-side ids — is *not* the right
hook for the data path: it treats a skipped step as harmless by design (`self.skipped += 1  # a hint only:
the lookup stays correct`). For warming a cache that is exactly right. For producing features, a skipped
step would mean the graph reads the previous step's rows and nothing would ever say so. v2 therefore keeps
its own path, and additionally tags every staged row with a device-side sequence number written in the same
stream order as the rows, so `_lookup` copies and then `masked_fill_`s anything not tagged for the current
step: a missed fill degrades to zeros and a counted skip, never to stale features.

Sizing, checked rather than guessed: `max_num_tokens` is `scheduler_config.max_num_batched_tokens` (2048
here), so staging is `2048 x 16` rows x 160 B = **5.24 MB** of device memory plus four pinned arenas of the
same size. That row count is identical to the engine's own `_prefetch_buffer` (`max_total_tokens *
etp_data_parallel_size x num_ngram_heads x embedding_dim`) at ETP=1; at ETP>1 mine would be short, and
`stage()` raises on capacity overflow rather than staging a partial step.

Two bugs found in my own wiring while testing it:

* The capture invariant in `stream_lookup` sat **above** the new v2 dispatch, so v2 — whose entire purpose
  is to be capturable — would have refused to start. Reordered, and the ordering is now asserted
  structurally by byte offset (v2 return < guard < v1 host copy), because that is precisely the kind of
  thing a later tidy-up silently breaks.
* Staging is gated on `PLE_STREAM_V2`, not `PLE_STREAM`, because building a stager *binds* it onto the
  embedding and `_lookup` dispatches on the bound object. Running the hook under a v1 deployment would
  have quietly converted it into v2 mid-flight.

The Dockerfile's assertions moved into `patches/check-ple-stream.py` after the inline `python3 -c` version
ran into the same quoting wall as before (a `\"` inside a single-quoted shell string reaches Python as a
literal backslash). The file now prints 11 PASS lines at build time, including that the staging hook
landed **exactly once** in `model_state.py` and that the streaming selection sits ahead of
`checkpoint_mapped`.

Both staging modes are implemented and named (`sync` waits on the step's D2H on the CPU; `async` hands it
to a worker thread) because the cost of the D2H is the one open question in this design and I would rather
measure both than argue about one.

## A typo I made twice is a missing check, not bad luck

v2's first real engine start died at `prepare_inputs → stage_step_rows → _build_stagers` with
`ModuleNotFoundError: No module named 'vllm.model_executor.models.qwen4_exp'`. The module is
`vllm.models.qwen4_exp`. I had already written this exact wrong prefix once earlier in the session, in
`skip_pageable_gate`, and had already been told the correct path by an import I ran to check it. Repeating
it means the class of error was unguarded, so now it is: `check-ple-stream.py` greps every overlay for the
literal bad path and fails the build. It could not be caught by importing the module, because the bad
import sits inside a function body and never executes at import time — which is also why the eleven
existing build assertions all passed on a broken image.

The import was also dead code: `_build_stagers` matches the backend by `type(emb).__name__`, so the fix was
deletion, not correction. And the same traceback confirmed the part I was actually testing: the staging hook
fires in `prepare_inputs`, eagerly, before any capture, exactly where designed.

## The staleness mask, tested where it can be tested

`apply_staged_rows` decides inside the captured graph whether a staged row is used. Its failure mode is
"silently wrong n-gram features", so it was factored out of the class as device-agnostic torch ops and put
under `scripts/test-ple-stream-v2.py`, which runs on CPU: all-fresh copies every row, all-stale zeroes the
output, mixed tags resolve **per row** (not per buffer), stale rows carry no staged bytes, and a short
`n_rows` neither reads nor writes past its bound. Seven assertions, all passing inside the image against
the real torch.

The first draft of the seventh check was `not out.any() or True`, which cannot fail. I have written
variants of that mistake often enough this session that the file now carries a static guard for it — a
check whose result is a constant is worse than no check, because it reads like coverage.

The case that check documents is genuinely load-bearing and genuinely not covered by this function: a
never-filled buffer has zero tags and `cur_seq == 0`, so it *matches* and gets copied. It is unreachable in
the engine only because `_lookup` runs after `prepare_inputs` has staged the step and the first fill sets
`cur_seq >= 1`. That is a property of hook placement, not of the mask, and the test says so rather than
implying the function is self-defending.

## Procedural

A `scp` of that test file failed with "No such file or directory" while the file was sitting committed in
the repo: the command ran without a `cd`, so the relative path resolved against the session's workspace
root instead of the repo. Absolute paths in remote-copy commands, or `cd` in the same statement.

# v2 works: 2.21 GiB of Shmem, FULL graphs intact, and two of my explanations were wrong

First successful v2 boot: healthy at 170 s, `strict a6cc8fed286b7071` unchanged, zero staging failures, and
the log confirms the geometry that was sized from config rather than guessed — `32768 rows x 160 B
(5.0 MiB device + 4 pinned arenas), max 16 reqs x 2 ctx, mode=sync`.

| | pinned (deployed) | v1 | **v2 sync** |
|---|---|---|---|
| `cudagraph_mode` | FULL_AND_PIECEWISE | PIECEWISE | **FULL_AND_PIECEWISE** |
| decode tok/s | 162.8 | 126.7 | **150.7** (−7.4%) |
| prefill tok/s | 18,211 | 15,382 | **15,462** (−15.1%) |
| Shmem | 66.18 GiB | 4.44 GiB | **2.21 GiB** |
| VRAM | 91,992 MiB | 92,032 MiB | 91,508 MiB |

Both predictions were checked before the run, and both held to within the explanation:

* **Shmem ~2.18 predicted, 2.21 observed.** The 0.03 GiB difference is the 21 MB of pinned staging arenas
  (4 × 5.24 MB). The decomposition — 2.00 GiB embed pin + ~0.2 GiB ordinary, with no table pin and no
  non-FULL-graph surcharge — is confirmed.
* **Decode ~160 predicted, 150.7 observed.** Direction right, magnitude not: about two-thirds of v1's
  decode loss was the graph regime, and roughly 7% is genuinely the streaming work. That 7% is measured
  against a *baseline in the same graph mode*, which the earlier v1 numbers were not.

## Retraction: breakable graphs were never the variable

I wrote earlier that the first v1 failure happened because `VLLM_USE_BREAKABLE_CUDAGRAPH` was unset and the
decorator therefore "returned the undecorated function", and that `PLE_STREAM=1` implies setting it. The v2
run proves that wrong: it sets **no** such env and the log says

```
[vllm.py:843] Auto-enabling VLLM_USE_BREAKABLE_CUDAGRAPH=1. Set VLLM_USE_BREAKABLE_CUDAGRAPH=0 to opt out.
```

This architecture is in `default_breakable_cudagraph_architectures()`, so breakable has been on in every run
tonight, including the first failure. The wrapper frame in that first traceback (`breakable_cudagraph.py:105
in wrapper`) is itself the evidence I misread — a stripped decorator leaves no wrapper frame. It passed the
call through because the capture in progress was a **FULL** `torch.cuda.graph` with no breakable context
around it, which is the branch `if mode == CUDAGraphMode.FULL: return fn(...)`.

Two consequences. The operative variable in all of this is `compilation_config.cudagraph_mode`, not the env
flag, and `start.sh`'s implication of that env is redundant on this host — kept for now because it is
harmless and is what makes the regime explicit on architectures where it is *not* the default, but it is
not load-bearing here and no conclusion should be drawn from its presence. And my claim that "enabling
breakable forces `CompilationMode.NONE`, so the comparison has two variables" is vacuous in this
deployment: mode was already NONE. The 2×2 was still the right experiment — it isolated `cudagraph_mode`
empirically, which is the thing that turned out to matter — but it succeeded for a reason I had stated
incorrectly. That distinction is the point: a design can be validated while its explanation is wrong, and
only the re-measurement catches the difference.

The remaining prefill loss (~15%) is most likely `sync` mode: the CPU blocks on a D2H event inside
`prepare_inputs`, which serialises host and device instead of overlapping them. `mode=async` is the test,
and it comes with a detector rather than a hope — if a fill misses its step the sequence tags mismatch, the
mask zeroes those rows, and the strict hash changes. A race in v2 is visible as a wrong answer, which is
the behaviour the tagging was built to provide.

# Async staging failed twice over, and the second failure was the dangerous one

`mode=async` booted, went healthy, and then produced **wrong output**: the probe's `[multilingual]`
completion came back empty and the strict hash diverged. Two independent causes, only one of which I
expected.

**The mundane one.** 526 × `RuntimeError: Inplace update to inference tensor outside InferenceMode is not
allowed.` The engine creates its tensors under `torch.inference_mode()`; the worker thread is not in that
mode, so every `copy_` into the staging buffers raised. Nothing was ever staged.

**The dangerous one, which the first one exposed.** With every fill failing, `cur_seq` never advanced from
its initial value. `dev_seq` was initialised to the same value. The mask compared them, found them equal,
and **copied uninitialized device memory into the model as n-gram features.** My CPU test had described
exactly this case as *"safe-by-accident … unreachable in the engine because the first fill sets
`cur_seq>=1`"* — and it took one failing mode to make it reachable. The assumption was about hook ordering,
and hook ordering is not a safety property.

The fix is in the data, not the placement: row tags start at `STALE_NO_FILL = -1` while the step counter
starts at `0`, so the two sides **disagree initially** and an unfilled buffer can never match. My first
attempt at this fix used `-1` for both — which still matches itself — and the test I had written for
precisely that case failed it, which is the only reason I know. The suite now asserts both halves: the
initial state is stale, *and* a shared sentinel would match.

## Why async is not worth fixing

Even with inference mode handled, async staging cannot be made correct at this hook. A captured CUDA graph
cannot wait on an event recorded outside capture, so a fill enqueued by a worker thread after
`prepare_inputs` returns cannot be ordered before the replay that consumes it. Making it ordered means the
CPU must wait until the worker has at least *enqueued* its copy — and since the expensive part (the gather)
happens before that enqueue, the wait is the gather. Async collapses into sync. Recorded so the next reader
does not spend an evening rediscovering it.

Sync mode is therefore the design, and it is now instrumented per phase — D2H wait, CPU n-gram ids,
gather+H2D enqueue — because "prefill is 15% slower" is not an explanation, and the prototype's 0.5%
exposed-fill measurement is not a prediction I'm allowed to reuse for a different shape.

The saving grace in all of this is that the failure was **visible**. Garbage features changed greedy output
and the hash caught it in seconds. That is the property the sequence tagging exists to provide, and it is
worth more than the throughput either mode would have bought.

# Where v2's cost actually is: the CPU is waiting for the GPU, and the gather is slower than the prototype said

Second sync run reproduced the first tightly — decode 150.5 vs 150.7 tok/s across independent boots (0.13%,
which is a better repeatability number than anything else measured on this host tonight), prefill 15,885 vs
15,462 (+2.7%), Shmem 2.21 GiB both times, strict hash unchanged, `skips=0`.

Then the per-phase timers, which I added specifically so this claim would not be a guess. Cumulative at 400
steps: `d2h=11.8 ms/step, ids=0.24 ms/step, gather=11.4 ms/step` — about **23 ms of host time per step**.

Read the first number correctly: a 16 KB D2H is not 11.8 ms. That interval is `event.synchronize()` on the
compute stream, so what it measures is **the CPU blocked until the GPU drains the previously launched
step**. Sync mode therefore serialises host and device: the GPU finishes, the CPU gathers, the GPU starts.
That is the mechanism behind the −15.1% prefill, and it means the thing to fix is the stall, not the memcpy.

Two follow-ups, in the order I would do them, with the reason each is suspicious:

1. **The stall has no cheap source-side fix.** The obvious move is to stop round-tripping ids and take them
   from the host. That path does not exist here: `SchedulerOutput.new_token_ids` is documented as
   *"only used for pipeline parallelism. When PP is not used, new_token_ids will be empty"*, and this
   `InputBatch` has no `token_ids_cpu` — it stages ids into persistent buffers (`input_ids[:num_tokens]
   .zero_()` then a scatter). So host-side ids would have to come from `req_states`, which
   `prepare_inputs` already receives, meaning a host reimplementation of the input gather. Real, but it is
   a bigger change than "read the buffer you already have".

2. **The gather is implausibly slow and that is more tractable.** Average 11.4 ms/step over ~2,400
   rows/step. The prototype measured ~1.3 ms warm for 32,768 rows with 24 threads, so the per-row rate here
   is roughly 70x off — which says fixed per-step overhead (numpy allocations, three device ops, thread-pool
   dispatch) rather than memory bandwidth. The cumulative counters cannot separate a 32,768-row prefill step
   from a 16-row decode step; bucketing gather time by row count would, and that is the next instrument
   change, not an engine change.

Also noted so it isn't believed: my "build assertions passed: 0" line was a bad anchor. The check prints a
two-space indent; I grepped for four. 12 assertions pass. The new module was in the image all along — its
log line moved from `:302` to `:341`, which is how I knew to look at my grep instead of the build.

# Rebase onto vLLM v0.32: v1 was not broken -- the graph regime it forces changes output

Rebasing the 160-commit `ssd-stream-exp1` onto a `prod` that had meanwhile taken the vLLM v0.32.0 rebase
produced five cells on `burst` (GPU 1; GPU 0 sampled every 10s and never above 2 MiB). The build applied
all eight port hunks and twelve assertions, `stream-v2` reproduced the pinned baseline hash across boots,
and `pinned-exact` confirmed the 16.3 GiB rounding saving empirically (50.06 vs 66.38 GiB Shmem).

One result looked like a v1 defect and was not. `stream-v1` hashed `1ce6cb9be5b93b92` where the baseline
was `7c1421daade83d79` -- self-consistent within the boot, divergent from the reference. The control that
decided it: **pinned backend, no streaming, `VLLM_USE_BREAKABLE_CUDAGRAPH=1` plus
`cudagraph_mode=PIECEWISE` reproduces `1ce6cb9b…` exactly.** A second control, `PIECEWISE` *without* that
env, reproduces the baseline. So on v0.32 the regime v1 requires alters greedy output by itself.

| config | env | cudagraph | strict hash | decode tok/s | Shmem |
|---|---|---|---|---|---|
| pinned, defaults | — | FULL_AND_PIECEWISE | `7c1421da…` | 144.0 | 66.38 GiB |
| pinned + PIECEWISE | — | PIECEWISE | `7c1421da…` | 20.8 | — |
| pinned + PIECEWISE (JSON form) | — | PIECEWISE | `7c1421da…` | 20.8 | 72.05 GiB |
| pinned + PIECEWISE | breakable | PIECEWISE | `1ce6cb9b…` | 125.5 | 68.50 GiB |
| stream-v1 | breakable | PIECEWISE | `1ce6cb9b…` | 108.7 | 4.51 GiB |
| stream-v2 | — | FULL_AND_PIECEWISE | `7c1421da…` | 129.4 | 2.41 GiB |

That fifth row is the one that closes the attribution. The first two PIECEWISE cells differed in *two* ways
(the dotted `-cc.cudagraph_mode=` spelling plus no env, versus the JSON `--compilation-config` form plus the
env), so neither alone isolated the variable. Holding the JSON form fixed and dropping only the env
reproduces the baseline hash at the same 20.8 tok/s decode: **the env is the discriminator, and the CLI
spelling is irrelevant.** Two independent boots of the no-env PIECEWISE config agreed on both numbers.

Shmem is the column that varies most between boots of the *same* configuration -- 66.38, 68.50 and 72.05 GiB
for three pinned cells -- so treat single-cell Shmem differences below a few GiB as noise. The streaming
cells' gaps (2.41 and 4.51 against ~66-72) are far outside that spread and stand.

Two things to take from this, one methodological and one numerical.

**Methodological.** On `gg` (v0.31) the same regime had been output-neutral, so I carried an assumption
across a major version boundary and then nearly diagnosed a bug in my own code because of it. The
generalisable version: when a feature forces a configuration change, the configuration change needs its
own control cell *re-run on the new version*, not inherited from the old one. My first control attempt was
also the wrong control -- `PIECEWISE` without the env, which decoded at 20.8 tok/s, an obvious tell that
it was not the regime under test. And `stream-v1`'s hash being self-consistent across repeats is precisely
what makes regime-driven divergence dangerous: it looks like determinism.

**Numerical, and it corrects my earlier split.** Against properly matched regimes the streaming cost is
roughly the same in both designs, and v2's advantage is entirely the graph regime:

| | prefill vs matched baseline | decode vs matched baseline |
|---|---|---|
| v1 streaming (regime-matched to 16,310 / 125.5) | −13.1% | −13.4% |
| v2 streaming (regime-matched to defaults) | −13.8% | −10.1% |

Earlier I described v2 as recovering "two thirds of the decode loss" against v1's total; against the
correct controls that total was never all streaming's. The gather/staging cost is ~13% of prefill either
way -- which also means the sync-mode stall is still the thing to attack, and it is common to both
designs.

How far apart the two hashes actually are is worth knowing, because "differs" covers both a single early
token riding through 64 greedy steps and a genuinely different contribution. Measured with the probe's
`--dump`/`--diff` pair (per-prompt gated text as labelled JSONL, compared offline by label), regime B
against the shipping default over the same five gated prompts on the same host: `code` parts after 49
shared characters ("size of a table…" against "total size of a table…"), `verbatim` after 19, and
`long-context`, `multilingual` and `multi-step` identical character for character. Two of five prompts
differ, both in the wording of the first clause rather than in substance -- the cascade signature of a
near-tie flipping early and carried forward by greedy decoding, not of wrong n-gram features. A hash alone
cannot make that distinction, which is the reason the distance mode exists.

# TP2 on this host: throughput, power envelope, and what changes in the output

Two boots per cell on `burst`, both cards capped at 300 W, pinned backend, `cudagraph_mode` left at the
default `FULL_AND_PIECEWISE` (tensor parallelism does not force the breakable regime).

| cell | devices | health | Shmem Δ | prefill tok/s | decode tok/s | strict hash |
|---|---|---|---|---|---|---|
| `tp1-pinned` | 1 × `9C:00.0` | 180 / 180 s | +66.18 GiB | 11,038 / 11,068 | 136.5 / 136.6 | `7c1421da…` |
| `tp2-pinned` | `51:00.0` + `9C:00.0` | 260 / 170 s | +66.36 GiB | 15,768 / 15,762 | 197.8 / 197.2 | `6fb33c30…` |

**TP2 is a large decode win at matched per-card power**: prefill +42.9%, decode +44.8%. Against equal *total*
wattage -- one card at its 600 W ceiling, measured earlier at 16,608 / 144.3 -- TP2 on 2 × 300 W gives decode
+37.1% and prefill −5.0%. So width buys decode and is roughly neutral on big prefills, which is what a
latency-bound decode step versus a bandwidth-bound prefill should do.

**The power cap is a first-order variable for prefill and a second-order one for decode.** Dropping a single
card from 600 W to 300 W cost prefill −33% (16,608 → 11,038) and decode −5.2% (144.3 → 136.5). Corollary, and
the reason the driver applies the cap itself: prefill numbers measured under different power envelopes are not
comparable, decode numbers roughly are. Earlier single-card figures in this file were taken at the 600 W
ceiling and form a matched set only with each other.

**TP width changes greedy output, reproducibly.** `6fb33c3054c7562d` reproduced across two independent TP2
boots and differs from TP1's `7c1421daade83d79`. Consequences: the strict-hash reference is per-topology, so
`REF` must be taken from a cell of the same TP width, and moving a serving host between TP1 and TP2 is a
re-baseline event rather than a tuning knob. Sized with `--dump`/`--diff`: one of five gated prompts differs,
after 133 shared characters (~23 words), and the divergence is in whether the reply re-echoes the instruction
before answering ("…, then explain the integer arithmetic line by line.\n\nThis is" versus ". This is a
classic modular arithmetic…"). `arithmetic`, `verbatim`, `multilingual` and `multi-step` are identical
character for character. A formatting-of-the-first-sentence difference at 23 words in, not a substantive
disagreement -- but it is a real output change and should be adopted deliberately.

# Why streaming is now permitted at TP2, and where it still is not

The launcher used to exit 2 on `PLE_STREAM=1` with `TP_SIZE>1`. That guard conflated two different things,
and reading the engine made the difference explicit:

* `nvidia/ngram_embedding.py:120` sizes the prefetch buffer as `max_total_tokens * etp_data_parallel_size`.
* `common/ngram_embedding.py:94` defines `etp_data_parallel_size = etp_group_world_size // tp_group_world_size`.
* `common/ngram_embedding.py:114-144`: when that value is above 1, `_get_dp_gather_slot` reads the per-rank
  token counts, `_gather_dp_ids` zero-pads to `slot_size = max(counts)` and `all_gather`s across the ETP
  group, and `_select_embeddings` slices this rank's rows back out afterwards.

So the hazard is not tensor width, it is **ids arriving at `_lookup` that this rank never computed**. Both
backends lay rows out positionally, and v2's freshness check is a per-row sequence tag that answers "was row
*i* staged for step *N*?" -- never "is row *i* the right row". A rank staging the wrong content keeps every
tag matching: wrong features, full speed, no error.

The two designs are not equally blocked there. v1 reads the ids tensor the engine hands to `_lookup`, which
is *after* the gather, so it structurally sees the correct rows whatever the ETP width -- at TP>1 it was
unmeasured, not impossible. v2 computes ids on the host, and a host can only see its own rank's tokens;
making v2 correct under ETP-across-DP would mean adding a host-side exchange to the staging path, which is a
design change, not a sizing fix.

Plain TP2 with no data parallelism is the unchanged-geometry case: `etp_data_parallel_size` is provably 1
(the constructor raises unless the ETP group size is divisible by the TP group size, and the ETP group cannot
exceed a 2-GPU world with DP=1 -- the TP2 cells booted, so the ratio is 1), `_gather_dp_ids` is the identity,
and the only per-rank difference is the vocab ownership range from `shard_indices`, which both backends
already read from the engine rather than assuming.

The guard therefore moved rather than disappeared. `require_unreplicated_rows()` in `qwen_stream_gather.py`
runs at stager construction in both designs and refuses to serve when the engine reports
`etp_data_parallel_size != 1`, naming the silent-failure mechanism in the message. Same principle as the tag
initialisation: put the safety property in the data, not in the ordering of hooks or the wording of a
launcher -- a launcher that forgets the check still cannot stage the wrong geometry. Verified on CPU: values
2 and 4 refuse, 1 and absent proceed. The build checker gained assertions for exactly that, and they were
confirmed to fail against the pre-change image before the image was rebuilt.

Cross-boot repeatability was also confirmed for the throughput itself: 0.27% spread on prefill and 0.07% on
decode between boots of the same cell, which is far tighter than the 13% prefill spread noted elsewhere in
this file for uncapped runs -- pinning the power envelope appears to have removed most of the prefill noise.

# v2 phase split on burst, and what the gather cost actually is

`PLE_STREAM_V2_LOG_EVERY=25` on the fixed image, one v2 cell, GPU=1 at 300 W, 1,125 staged steps, 972,240
rows (864 rows/step), `skips=0`, hash identical to the pinned baseline:

| phase | us/step | what it measures |
|---|---|---|
| `d2h` | 12,794 | `ev.synchronize()` on this step's input D2H |
| `ids` | 707 | host n-gram id computation |
| `gather` | 1,787 | row copy into pinned staging + H2D enqueue |

The first column is the one to read carefully, because it looks like the smoking gun and is not. 12.8 ms per
step in a ~28 ms step would be half the runtime, yet decode lost only 7.8% against the pinned control. The
wait is for the GPU to drain to the point where `input_ids` were written, which is work the CPU would have
waited for anyway: it overlaps the replay instead of delaying it. The exposed host work is `ids + gather`
= 2.5 ms/step, which against a 28 ms step is ~9% -- and the measured decode loss is 7.8%. That agreement is
the evidence that the sync-mode stall is not the problem; the host work per step is.

`scripts/bench-ple-gather-latency.py` then shows what that host work is made of. CPU-only, real checkpoint,
`gather_to` timed at different batch sizes, with the number of shard groups per batch reported alongside:

| rows | ms/call | us/row | shard groups |
|---|---|---|---|
| 16 | 0.348 | 21.77 | 16 |
| 64 | 1.004 | 15.69 | 51 |
| 256 | 2.161 | 8.44 | 112 |
| 864 | 2.487 | 2.88 | 128 |
| 2,048 | 2.542 | 1.24 | 128 |
| 8,192 | 2.894 | 0.35 | 128 |
| 32,768 | 4.686 | 0.14 | 128 |

The absolute cost barely moves from 256 to 8,192 rows: 2.16 ms to 2.89 ms for a 32x larger batch. Fitting the
two ends, marginal cost is ~3.5 ns/row -- the memcpy is exactly as fast as the prototype measured -- and the
fixed part is ~19 us **per shard group**, not per call. 16 groups cost 0.35 ms, 51 cost 1.0, 128 cost 2.5.
Every batch bigger than a couple hundred rows touches all 128 shards of a 47.7 GiB table, so decode-size
steps pay 128 groups' worth of Python bookkeeping to move 138 KB.

That is the "fixed per-step overhead" suspicion, quantified, and it names the fix: `gather_to` groups ids by
shard in Python and issues one ctypes call per group, re-deriving `argtypes` on each call and making two
`ascontiguousarray` copies per group. One C call over a shard-sorted index array should take the fixed term
from ~2.4 ms to something proportional to the work, which by the arithmetic above is worth most of the 7.8%
decode loss. Prefill barely sees it, because 2.4 ms against a ~200 ms compute window is noise -- consistent
with prefill and decode costs being dominated by different things all along.


# Removing the per-group overhead: 2.49 ms -> 0.056 ms for a decode-size batch

`ple_copy_rows_multi` takes the shard index per row and walks the whole batch in one C call; `gather_to`
sorts by shard once and hands over three contiguous offset arrays. `argtypes` are declared at load time
instead of inside the per-group worker. Measured with `scripts/bench-ple-gather-latency.py`, same host, same
checkpoint, CPU-only:

| rows | before ms/call | after ms/call | speedup |
|---|---|---|---|
| 16 | 0.348 | 0.028 | 12.4x |
| 256 | 2.161 | 0.035 | 61.7x |
| 864 | 2.487 | 0.056 | 44.4x |
| 2,048 | 2.542 | 0.102 | 24.9x |
| 32,768 | 4.686 | 2.744 | 1.7x |

The 864-row point is the decode step the in-engine phase split measured at 1.79 ms, so exposed host work per
step should fall from ~2.5 ms (ids + gather) to ~0.8 ms, leaving the host-side id computation as the largest
remaining piece. Large batches improve less because there the memcpy is the real work -- which is the same
split the before-table showed from the other side.

Correctness gate: `scripts/test-ple-rowcopy.py` all-pass, 0 wrong rows across inline, boundary, fan-out,
whole-table, scattered, sharded and ownership-zeroing cases. The first version of this change segfaulted in
that suite for a reason worth recording: `StreamTable.from_paths` builds the object with placeholder zero
bases and fills the real mapping addresses *afterwards*, so a pointer array cached in `__init__` handed the
C loop NULL bases. Fixed by `_refresh_bases_ptr()`, called from both places -- and a reminder that caching a
derived view of mutable state needs the mutation path to invalidate it.


# In-engine effect of the single-call gather: decode loss 7.8% -> 3.4%, output still byte-identical

Same cell as the phase split above -- v2, TP1, GPU 1, 300 W, 1,125 staged steps, 972,240 rows -- rebuilt image:

| metric | before | after | pinned control |
|---|---|---|---|
| `gather` us/step | 1,787 | 364 | -- |
| `ids` us/step | 707 | 727 | -- |
| exposed host work (ids+gather) | 2.49 ms | 1.09 ms | -- |
| decode tok/s | 126.0 | 131.9 | 136.6 |
| decode vs control | -7.8% | **-3.4%** | -- |
| prefill tok/s | 10,515 | 10,566 | 11,034 |
| strict hash | `7c1421da…` | `7c1421da…` | `7c1421da…` |

The cost model that motivated the change holds: exposed host work fell from ~2.5 ms to ~1.1 ms against a
~28 ms decode step, which predicts a loss of roughly 3.9% where 3.4% was measured. The in-engine gather
improved 4.9x rather than the 44x the CPU-only benchmark showed, because the in-engine number also includes
the H2D enqueue into the pinned arena, which the benchmark does not measure.

`ids` is now the larger half of what remains: 727 us of host-side n-gram id computation per step, produced by
the engine's own host ids function rather than by anything in the streaming overlay. Attacking it is a
different kind of change -- it means porting engine code, not tuning a copy loop -- and the whole remaining
budget is ~1.1 ms/step, so even eliminating it entirely is worth roughly 3 points of decode. That is the
honest stopping point unless a 3% decode gain justifies touching engine-side id computation.

# What the remaining `ids` cost is made of

`scripts/bench-ple-ids-latency.py` calls the engine's own CPU `compute_ngram_ids` directly, on a stub carrying
the checkpoint's n-gram geometry (`ngram_size=3`, `heads_per_ngram=8` -> 16 heads, `vocab_size=248320`), with
no engine and no GPU:

| tokens | ms/call | us/token | rows |
|---|---|---|---|
| 16 | 0.190 | 11.88 | 256 |
| 54 | 0.195 | 3.62 | 864 |
| 128 | 0.203 | 1.58 | 2,048 |
| 512 | 1.916 | 3.74 | 8,192 |
| 2,048 | 2.050 | 1.00 | 32,768 |
| 8,192 | 2.940 | 0.36 | 131,072 |

Two things fall out. First, the decode-time shape is the flat part of the curve: ~0.20 ms per call whether the
step carries 16 tokens or 128, so at decode sizes the engine's hashing is mostly per-call cost, not arithmetic.
Second, the real decode step is 54 tokens -- `rows/steps = 972,240/1,125 = 864`, and 864/16 heads is exactly 54
tokens with a fixed spec-decode width -- and the in-engine `ids` phase for that step measured 727 us against
these ~195 us. So roughly half of the remaining `ids` cost is not the hashing itself: it is the environment --
the wrapper around it (pinned-memory slices, the tensor-to-numpy handoff) and whatever the live worker's thread
contention adds.

Request count was the obvious candidate for that gap, and it is not the explanation. Re-running the same call
at fixed tokens with 1 to 16 requests (a second axis in the same script) moves the decode-size number from
0.198 ms to only 0.239 ms, and at 512 tokens it stays inside 1.4-3.0 ms with no monotonic trend. The one thing
that table does show is a first-call effect: the first point of each group reads high (0.419 ms where the
steady state is ~0.20), so a single-sample measurement of this function overstates its cost -- which is worth
remembering when comparing these numbers against the in-engine average, itself taken over more than a thousand
steps.

That matters for how to spend effort. The cheap lever is inside our overlay: stop making a second contiguous
copy of the ids and stop re-slicing pinned arenas per step. The structural lever is to stop hashing on the host
at all -- the engine already has a CUDA path (`ple_ngram_ids`, taken when `input_ids.is_cuda`) that could write
the 864 ids on device, after which the host only needs a 7 KB D2H before the row gather. Both are bounded by the
same ceiling: about 1.1 ms of exposed host work per ~28 ms step, which is the ~3 points of decode the phase
split said was left after the gather fix.

# Streaming on genuinely small hosts: measured cache budget on gg

`scripts/test-gg-cache-pressure.sh`, FP8 checkpoint, `PLE_STREAM_V2=1`, TP1, one GPU, `sudo ple-drop-caches`
before every boot so each cell starts cold, swap pinned equal to the cap so anon cannot spill and hide the
effect. Cgroup counters from inside the container (`memory.stat`, `memory.events`), MiB, before -> after the
drive:

| cap | anon | file cache | refault_file | limit hits | prefill | decode | strict hash |
|---|---|---|---|---|---|---|---|
| none | 7,063 -> 7,084 | 80,815 -> 80,864 | 0 -> 0 | 0 | 15,521.6 | 139.9 | `7c1421da…` |
| 32 GiB | 7,142 -> 7,162 | 24,578 -> 24,627 | 349,903 -> 349,903 | 17,729 | 15,404.6 | 139.9 | identical |
| 16 GiB | 6,996 -> 7,017 | 8,409 -> 8,462 | 663,348 -> 664,258 | 21,605 | 15,137.4 | 139.0 | identical |
| 8 GiB | -- | -- | -- | -- | never healthy (1,208 s) | | |

What the numbers say:

* **Host-side anon is a fixed ~7 GiB**, independent of the cap, and it is the thing that decides whether a
  small host works at all: at an 8 GiB cap only ~1 GiB is left for cache, and the cgroup OOM-killed the
  container's own runtime (`crun invoked oom-killer … Out of memory and no killable processes` in dmesg). That
  is a hard stop, not a slow boot, and it is why the launcher's floor sits at 2 GiB of cache rather than being
  a style preference.
* **Steady-state serving barely notices the pressure.** From uncached to a 16 GiB cap, prefill falls 2.5% and
  decode 0.6%. Almost all refaulting happens during boot (refaults climb to 349,903 by health at the 32 GiB cap
  and 663,348 at 16 GiB); during the whole driven probe at the 16 GiB cap only 910 more pages refault. The
  working set for this probe traffic is a few GiB, not tens.
* **The cgroup limit is hammered during boot** -- `max` reaches 17,729 and 21,605 -- while `high` stays 0.
  Reclaim is doing the work; nothing is being refused outright until the cap approaches the anon footprint.
* **Greedy output is unaffected by memory pressure.** Same strict hash at every cap that booted, as expected:
  eviction changes which pages are resident, not what the model computes. If this ever differs, suspect the
  staging path rather than the cache.

Two earlier claims tighten up against this. The synthetic read-path numbers in `platform-config.sh` (hit rate
0.744 at 8 GiB of cache, 0.797 at 16 GiB, working set 14.7 GiB) pointed the right way but understated how well
serving tolerates a small cache -- measured, 8.4 GiB of cache costs 2.5% of prefill. And the warning threshold
of 16 GiB is now a measurement rather than a guess: at that size the engine boots in under 300 s cold, matches
the pinned hash exactly, and gives up a couple of percent.

Operating rule for streaming hosts, from the table: **16 GiB of total RAM is the effective minimum, 32 GiB the
recommended size.** At 16 the engine boots cold in under 300 s, matches the pinned hash, and gives up 2.5% of
prefill and 0.6% of decode; at 32 it gives up under 1% of prefill and nothing measurable of decode. Below that,
the cliff is the engine's fixed ~7 GiB anon footprint plus ~2 GiB of usable cache -- 8 GiB caps OOM the runtime
itself.

For comparison at the same host and envelope, the pinned arms: `round2` FP8 needs 66.0 GiB and was refused by
the preflight at every cap tried (206%/413%/825% of installed), `exact` needs 48.9 GiB and served with identical
output. So below ~50 GiB of RAM the ordering is exact-pin, then stream; below ~9 GiB neither fits because of the
engine's own anon footprint.

# BF16 profile qualified: `exact` returns ~33 GiB at both TP widths, output unchanged

burst, LONGCTX=1 (524288 + YaRN), QUANT=exl3 (BF16 PLE table), both cards capped at 300 W, image
`localhost/qwen38-rtx:exp3-stream`. `Shmem` is read as a delta over a pre-boot sample because it is host-wide.

| cell | strategy | pinned demand | predicted | strict hash | prefill | decode |
|---|---|---|---|---|---|---|
| tp1-round2 | round2 | 130 GiB (+131 observed range 133.9 abs) | 130.00 | `7c1421daade83d79` | 11,105.9 | 131.5 |
| tp1-exact  | exact  | +97.54 | 98.00 | `7c1421daade83d79` IDENTICAL | 11,081.1 | 130.9 |
| tp2-round2 | round2 | +130.36 | 130.00 | `dae7b3a806972f90` | 15,821.6 | 195.3 |
| tp2-exact  | exact  | +97.73 | 98.00 | `dae7b3a806972f90` IDENTICAL | 15,722.9 | 195.1 |

Four things worth keeping.

**The saving is real and matches the arithmetic at both widths.** Predicted 130 against 98; measured deltas
130.36 -> 97.73 and ~131 -> ~97.5, i.e. 32.6 GiB returned. The rounding waste does not shrink at TP2, which is
the point of computing demand as `tp x round(size/tp)` rather than `round(size)`.

**Nothing observable changes.** Greedy output is identical within each width, twice per cell, and throughput
moves by at most 0.6% -- inside gg's own cross-boot spread of about 1%, so the honest reading is "no measurable
cost". The loader confirms the strategy from inside: `PLE weight pinned at exact size 95.368 GiB (no
power-of-two rounding)` at TP1, and one such line per rank at TP2, each `47.684 GiB`, which also shows the table
is split per rank rather than replicated. Graph regime stayed `FULL_AND_PIECEWISE` in all four cells, with zero
capture-copy errors and zero load errors.

**Two independent stability checks agreed.** The TP2 hash `dae7b3a806972f90` is the same one recorded from an
engine started from a different image (`localhost/qwen38-rtx:local`) before this run, so the exp3 overlays do not
change BF16 TP2 output. And BF16 at TP1 hashes to `7c1421daade83d79`, the value the FP8 checkpoint produces at
TP1 -- on the gated prompt set the PLE dtype difference is not visible at TP1, while TP2 gives a different value
for each checkpoint. Observed, with no stronger claim attached: hash identity is per topology.

**TP width is worth far more here than dtype.** TP2 over TP1 on the same BF16 profile: prefill +42.5%, decode
+48.5% at matched per-card watts -- close to the FP8 measurements (+42.9% / +44.8%), so the width gain is a
property of the shape rather than of the table dtype. BF16 itself costs a little decode against FP8 at TP1
(131.5 vs ~136.5) while prefill matches.

### Harness note

The first cell of the burst run aborted for an unrelated reason, and the fix is worth recording because it made
a cell report nothing at all: `podman rm -f` returns while the old container is still in its grace-period
shutdown, and `start.sh` refuses a name that is still held (`qwen38-exl3 is already stopping. Run ./stop.sh
first`). With a fixed 3-second sleep, the first cell of every run lost its boot to that race and only the later
cells measured anything. All three drivers now wait for the name to be released and pass `REPLACE=1`, so the
launcher does the stop-and-replace deliberately instead of refusing. The `tp1-round2` numbers above come from a
re-run after that change, in which the cell booted in ~240 s and produced the hash shown.

### gg reproduction (same pair, second host)

With the fixed driver, gg runs the same TP1 pair: `round2` is refused by the preflight (needs 130.0 GiB against
125.7 GiB installed, with alternatives printed), `exact` boots healthy in 360 s and pins `Shmem` +97.54 GiB --
predictions confirmed on a host whose memory is actually tight, which is where the rounding decision matters.
Its strict hash is `7c1421daade83d79`, matching burst exactly at the same topology, and throughput is prefill
11,287.7 / decode 133.1 against burst's 11,081.1 / 130.9 (+1.9% / +1.7%). The wattages are not matched across
hosts: burst's header shows both cards at 300.00 W (`-pl` applies there), gg's shows its card at its default
600.00 W ceiling (sudoers there covers only `ple-drop-caches`, so the cap attempt is a no-op). Within gg the
pair shares that envelope, which is what makes its numbers a fair same-host comparison; cross-host deltas of
~2% should be read with the watt difference in mind. Same profile, two
hosts: identical bytes out, pinned demand within 0.2 GiB of prediction.

# `PLE_PIN_STRATEGY=exact` is the launcher default

Both EXL3 profiles (`exl3` BF16 and `exl3-ple8` FP8) share one launcher, so the default lives in one place:
`start.sh` and the preflight in `platform-config.sh` now fall back to `exact` when the variable is unset.
`round2` remains accepted and is still what an image without the `qwen_pinned_host` port does regardless, which
is why the preflight keeps printing the NOTE telling the operator to confirm the loader's exact-size line.
Verified after the change by restarting burst's serving engine with no strategy set: preflight approves on
"strategy exact" (97.4 GiB against 96.6 GiB of checkpoint), the loader logs `pinned at exact size 95.368 GiB`,
`Shmem` sits at 101.30 GiB absolute, hash `7c1421daade83d79`, decode 131.6 -- all matching the qualified cells.
Consequence worth knowing: on a ~126 GiB host like gg, BF16 is now approved by default (97.4 fits) where
`round2` would refuse it (130.0 against 125.7), and FP8's default reservation drops from 66.4 GiB to 48.9.

# Streaming gets one knob: `PLE_STREAM=1` is the shipping design; `piecewise` is the diagnostic

One variable, three values, resolved in `start.sh` into the pair both old and new images read:

| operator writes | forwards | graphs | what it measures |
|---|---|---|---|
| `PLE_STREAM=1` | `PLE_STREAM=1 PLE_STREAM_V2=1` | `FULL_AND_PIECEWISE` | streaming as shipped |
| `PLE_STREAM=piecewise` | `PLE_STREAM=1 PLE_STREAM_V2=0` + forced PIECEWISE + breakable | `PIECEWISE` | the graph-regime cost alone |
| unset (`0`) | `PLE_STREAM=0 PLE_STREAM_V2=0` | default | pinned reads |
An explicitly supplied pair is honoured verbatim, so historical cells keep reproducing. Inside the container the pair stays the contract for the same reason: images built before this change read only the pair, and `v2_enabled()` falls back to `PLE_STREAM` when `PLE_STREAM_V2` is absent — so a bare `PLE_STREAM=1` on a fresh container gets the staged design too.

Why `piecewise` for the diagnostic arm instead of a version tag: it is what the loader prints for that arm (`graph mode: CUDAGraphMode.PIECEWISE`), so the value names the observable regime rather than the revision it came from. Reading a container's env tells you which regime is in force without looking anything up. "v1" would have been accurate history and useless guidance.

What the old `PLE_STREAM=1` meant — staging inside the replay via `RowStager` — remains reachable by the explicit pair, because it is still the fallback gather when no stager is bound: the first steps, the capture passes, and `--enforce-eager`. Removing it would leave streaming with no path outside graphed replays. Its measurements anchor the regime decomposition and do not need a re-run to stay true: the v1-vs-v2 gap was 17.8 points of graph regime against ~4.4 points of staging position.

Gate for the whole table: `scripts/test-ple-knob-resolution.py`, offline, against the fake-docker lifecycle harness — it asserts the exact `-e` pair and graph flags reaching the container for every spelling.

# gg cache ladder re-run under one recorded power envelope (supersedes the tables above)

Same driver, rewritten to pair every arm in one job: `scripts/test-gg-cache-pressure.sh`, FP8 checkpoint, TP1,
one card. The envelope is recorded before the first cell: `sudo -n nvidia-smi -pl 300` applies on gg -- sudo
works there, which corrects the earlier note claiming its sudoers made the attempt a no-op -- and the header
then shows `0, 00000000:06:00.0, 300.00 W, 29`. Cold cache via `ple-drop-caches` before every boot; swap
pinned equal to the cap so anon cannot spill. Cells in order: `pinned-exact` (sets the strict-hash reference),
uncapped streaming, streaming at 32 GiB, streaming at 16 GiB.

| cell | prefill tok/s | decode tok/s | strict hash | Shmem | healthy after |
|---|---:|---:|---|---|---|
| pinned-exact | 11,347.3 | 139.0 | `7c1421daade83d79` | 49.86 GiB | 244 s |
| uncapped-stream | 10,963.3 | 134.7 | identical | 2.21 GiB | 171 s |
| stream-32g | 11,084.4 | 135.3 | identical | 2.21 GiB | 271 s |
| stream-16g | 11,073.1 | 135.1 | identical | 2.21 GiB | 281 s |

What changes against the earlier reading, and what does not:

* **Streaming costs about 3%, not about 15%.** The earlier pair compared a pinned anchor taken at the card's
  higher default ceiling against capped streaming cells: most of that −15% was wattage, not memory strategy.
  With all four arms inside one recorded envelope, the deltas are −3.4%/−3.1% uncapped, −2.3%/−2.7% at 32 GiB,
  −2.4%/−2.8% at 16 GiB. Direction unchanged: pinned is faster when it fits; streaming's penalty is small.
* **Caps are invisible in steady-state serving.** The capped arms match the uncapped arm within gg's own noise
  floor (~1% prefill, ~0.3% decode); the refault counters say why: ~489k refaults by health at the 16 GiB cap,
  then only **+22 pages across the entire driven probe**. Cache pressure is a boot-time phenomenon for this
  working set, exactly as claimed before -- now with a same-envelope control.
* **Boot time is the real cost of a small cap**: ~171 s uncapped against ~271-281 s capped. So the sizing rule
  survives with a corrected justification: minimum ~16 GiB total RAM and comfortable ~32 GiB buy cold-boot
  margin; throughput barely notices the difference. Below ~8 GiB the fixed ~7 GiB anon footprint still OOMs.
* **Greedy output: identical across all four arms**, pinned and streaming, capped and not. The hash gate is now
  recorded with an explicit envelope, so future disagreements between records can be attributed to something
  other than clock state.
* Anonymity of modes is confirmed again from inside the container: `Shmem` 2.21 GiB for streaming vs 49.86 GiB
  for pinned-exact, matching the prediction arithmetic exactly as before.

The older gg figures elsewhere in this file and in `docs/gg-variant-benchmark-plan.md` predate envelope
recording; their hash-era values (`a6cc8fed286b7071` generation) are probe-generation-specific and consistent
within their own runs. Cross-generation hash differences are prompt-set changes, not behavior changes.

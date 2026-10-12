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
(`discard_max_bytes` = 2 TiB, granularity 512). So the FTL was never told those
LBAs were free -- from the device's point of view it was still ~90% full during
the "5.5% recovery" measurement. `fstrim.timer` is active but weekly, and could
not have covered a file created and deleted inside the window. A real recovery
arm needs root, which makes this the one question in the whole pre-work where
root would actually buy something (unlike section 2's page-cache case, where it
would not).

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

So the earlier conditional -- "if qd1 degrades the way qd8 did, a per-token
synchronous read lands near 1.3x budget" -- is now measured, at 1.3x, and it is
the thinnest margin anywhere in this study. **A per-token synchronous read path
is not viable on a drive that has ever been written heavily**: one 30% slip and
the chunk deadline is missed with no reserve. Submit at depth instead (io_uring
or libaio, >=8 outstanding, 32 comfortably), where the same drive delivers
21.3x.

Twenty-five minutes of idle produced no recovery whatsoever (qd8 96,448 vs
96,572 at ten minutes, 0.1% apart), which is consistent with the untrimmed-
garbage explanation above but still does not separate it from a persistent GC
state -- that separation needs `fstrim`, and `fstrim` needs root.

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

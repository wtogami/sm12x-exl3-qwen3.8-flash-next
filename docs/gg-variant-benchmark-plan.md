# Benchmark plan: pinned round2 vs exact-size vs streaming, all on `gg`

Plan only. Nothing here is measured yet except the baseline arm.

## Why three arms and why all on `gg`

A decode rate is a property of the machine producing it, and every receipt in
`benchmarks/` before this session came from `burst` or another host. `burst` also has
8x the RAM, so it cannot answer any question about pinning. One host, three
configurations, same everything else.

| arm | what it is | what it needs |
| --- | --- | --- |
| **A** `round2` | today's default: `pin_memory`, table pins 64.000 GiB for 47.684 GiB of FP8 | already running; receipt exists |
| **B** `exact` | `PLE_PIN_STRATEGY=exact`: table pins 47.684 GiB, aliased, no kernel change | one build, then an env var |
| **C** `stream` | CPU-side gather into pinned staging + async H2D (**stage B; not written**) | code. `PLE_MMAP=1` **cannot work here** -- see below |

Two consequences of that table that change the schedule:

* **A and B share one image.** The fair baseline is therefore *not* the existing
  receipt -- that was taken on the current image, which carries neither the port nor
  the `reject_exact_repin` import in `model_loader/utils.py`. Re-measure A on the
  B-image with the flag off (`A'`) and interleave `A'`/`B`. The build is the expensive
  part; the arm is one env var and a restart.
* **C is blocked, and needs no build either -- it needs code.** The mmap ports *are* in
  the installed image (labels `io.tpurtell.ple-mmap.pr = 58439+58835`, commit
  `47b9933db82d`; `nvidia/ple_pageable.py`), so I tried running arm C directly with
  `PLE_MMAP=1`. The container died 60 s in:

  ```
  require_pageable_access: device 0 reports
  CU_DEVICE_ATTRIBUTE_PAGEABLE_MEMORY_ACCESS_USES_HOST_PAGE_TABLES=0
  ```

  That gate is legitimate rather than conservative -- `MappedTable` hands a plain
  pageable `mmap(PROT_READ)` to a Triton kernel that the GPU dereferences, which a
  discrete part cannot do. So arm C is a development item, not a measurement arm: the
  payload must be routed through **pinned** staging (which discrete GPUs *can* read, and
  which is how the engine serves today) with a CPU-side gather and async H2D copy.
  Sequenced accordingly: A'/B now, C after stage B lands.

## Grid: a staircase, not a cross product

`gg` reports `GPU KV cache size: 935,216 tokens` and `Maximum concurrency for 524,288
tokens per request: 1.78x`. Concurrency times context must fit inside that KV pool with
headroom, so most (concurrency, context) cells are unservable and the ladder has to be
staircased:

```
concurrency   context ceiling on gg        decode ladder      prefill ladder
c16           <= 58,451 tokens each        256 out            2048, 8192, 32768
c8            <= 116,902                   256 out            32768, 131072
c4            <= 233,804                   256 out            131072
c1            <= 935,216 (so 512K fits)    256, 1024 out      261632, 458752
```

At 512K only one request is servable at a time (1.78x means two do not fit), so the
long-context arm is necessarily c1 and measures a different thing from the c16 arm:
single-stream latency and prefill burst size rather than aggregate page pressure.

## Decision: all arms run at `LONGCTX=0` (native 262144)

YaRN is dropped from the core comparison. It is close to orthogonal to the storage
question (below), it removes a confound rather than adding one, and it makes the
concurrency-ceiling problem disappear. `A'` had to be re-measured anyway, so switching
modes costs no extra runs.

One correction to the reasoning that got us here: the claim that YaRN costs throughput was
**already measured and refuted in this repo**. `start.sh` documents the switch as
"RULER 97.66 at 523K vs 97.92 at native 262K, **no measured throughput cost**". So the
saving from dropping it is not engine time -- it is test-suite time, which is a property
of which arms we choose to run, not of the rope mode. Both knobs are independent, and
conflating them nearly cost us the load-bearing prefill measurement, since the biggest
prefill point (261,632) is available inside the native window.

Two consequences that are not free:

* **The existing baseline receipt is now stale by configuration**, since it was taken at
  `524288`/YaRN. It stays as a historical record but cannot be paired against any arm
  here.
* **Cross-mode determinism is not valid.** YaRN changes positional encoding, so the same
  prompt can legitimately produce different greedy tokens. The byte-identity gate only
  means something within one rope mode. This is reasoning, not measurement -- it gets
  confirmed or corrected the first time an arm is run in both modes.

## Is the 512K YaRN window relevant here? Partly, and not for the obvious reason

The checkpoint is natively `max_position_embeddings = 262144`; 512K exists only via
`LONGCTX=1`, which sets `--max-model-len 524288` plus
`--hf-overrides {"text_config":{"max_position_embeddings":524288,"rope_parameters":{"rope_type":"yarn","factor":2.0,...}}}`
in `start.sh`.

**YaRN itself is close to orthogonal to the storage question.** PLE page demand is
`pages_per_token x tokens_per_second`, and rope scaling changes neither the n-gram
lookup count nor the table size. Its effect on this comparison is indirect: it makes
per-token attention more expensive at long context, which *lowers* tokens/s and so
*loosens* the streaming budget. Attributing a long-context difference to YaRN without
holding token counts fixed would be a mistake, so measure 262144-native and 524288-YaRN
**at the same prompt token counts** and treat any gap as a compute effect.

What genuinely varies with context length, and is worth measuring:

1. **Prefill burst shape -- and this is the arm that sizes the design.** Prefill runs at
   a far higher token rate than decode, and page demand is `pages_per_token x
   tokens_per_second`, so the storage requirement tracks **prefill throughput, not decode
   throughput** -- which inverts the framing used everywhere earlier in these findings,
   where decode was treated as the binding rate. The arithmetic is unavoidable:

   ```
   pages/s required = 16.5333 x prefill tok/s   (fp8; 17.25 for bf16)

   prefill tok/s     pages/s needed     gg single worker 122k   gg 4 workers 523k
        5,000            82,700                1.48x                    6.3x
        7,000           115,700                1.06x no margin        4.5x
       10,000           165,300                0.74x FAIL              3.2x
       20,000           330,700                0.37x FAIL              1.6x

   The middle row is measured, not interpolated. `benchmarks/exl3-final/prefill.json`
   reports effective prompt throughput of 6,472-7,513 tok/s and it is essentially flat
   from 2,048 to 261,632-token prompts, so the page-rate requirement is ~107k-124k pages/s
   regardless of context size -- **0.99x to 1.15x of `gg`'s single-worker ceiling.** A
   one-thread streaming submitter sits on the limit, not under it, at every prompt length.
   Four workers move it to 4.2-4.9x. Time to first token in the same receipt: 0.3 s at
   2,048 rising to 40.4 s at 261,632.

   Caveat before this is used as a `gg` number: that receipt is `burst` running the BF16
   `exl3` profile. It settles the architectural question (one worker is insufficient)
   because the required rate is a property of pages/token and prefill tok/s, but the
   absolute pages/s must be restated from `gg`'s own prefill arm, which is scheduled.
   ```

   A 261,632-token prompt is 4.33M page reads -- **about 16.5 GiB of device traffic for
   the FP8 table** in one pass (17.2 GiB for BF16), against only ~670 MB of actually
   useful row bytes. That 25x read amplification is the price of 160-byte rows at 4 KiB
   granularity, and it is what the chunked-prefill boundary has to absorb inside compute
   time rather than across decode steps.

   Two conclusions. One worker is not enough once prefill exceeds ~7k tok/s, so the
   multi-threaded submitter is not an optimisation for arm C but a prerequisite; and the
   plan's "2048-token chunk in ~3.6 s" figure is a *decode-era* number that understates
   what a real prefill pass demands, so it should be replaced by the table above.
   Measuring `gg`'s actual prefill tok/s at each size is therefore load-bearing for arm
   C in a way it is not for arms A and B.
2. **Decode demand falls as context grows.** Longer context means slower tokens/s, so
   the worst-case page rate is at *short* context and high concurrency. If the c16/256-out
   arm passes, the long-context decode arms are comfortable by construction.
3. **KV pool pressure.** 935,216 tokens is 3.57x the native window, and pinned-vs-stream
   differ in host RAM, not GPU RAM -- but arm C also needs host memory for its readahead
   pool, which competes with everything else on a 126 GiB host. That is exactly the
   resource arm B frees up, so record host memory per arm alongside throughput.

## Fixed measurement set, identical for every arm

1. **Startup and footprint** -- wall time to `http=200`; `Shmem`, worker `/dev/zero`
   mappings, `MemAvailable`, swap after ready. This is the claim B actually makes and it
   needs no benchmark to establish: expect 66.18 -> 49.87 GiB pinned.
2. **Decode ladder** -- `benchmark-decode.py`, `--mtp-tokens 3 --mtp-policy static
   --output-tokens 256 --concurrency 1 2 4 8 16`, matching the existing baseline's
   schema (`qwen38-decode-concurrency.v3`) so receipts stay comparable.
3. **Prefill ladder** -- `benchmark-prefill.py` per the staircase above.
4. **Determinism gate first** -- a fixed greedy prompt set must be byte-identical across
   arms before any timing is compared; otherwise the arms may be benchmarking different
   token sequences and the numbers are meaningless.
5. **Arm C only:** `/proc/diskstats` delta across each run (actual pages/s at the device
   -- the one instrument that survived `libaio`'s per-task accounting blind spot),
   page-cache growth as the buffered-fallback detector, drive temperature, and the
   submitter worker count swept 1/2/4/8, since both drives were shown to be
   submit-limited rather than device-limited.

## Stage B arms: page cache versus O_DIRECT, and why the memory cap is the experiment

Decided after the pinned-versus-exact pair, in response to the observation that sglang's
streaming reader is page-cache-backed rather than `O_DIRECT`. Both access methods get
implemented and both get measured. They are not a speed race against each other, for the
reason quantified below, and the arm that matters is the one run under a memory cap.

### The cache bonus is proportional to the RAM the feature exists to free

Under independent random requests over N pages, LRU hit rate is C/N: the cache holds the C
most recently used distinct pages and any page is equally likely to be requested next. For
the 47.684 GiB FP8 table (12,500,074 pages) against the observed ~115,700 pages/s prefill
requirement, with `gg`'s measured ceilings of 122,571 IOPS on one worker and 523,000 on four:

| spare RAM | hit rate | device pages/s | vs 1 worker | vs 4 workers |
|---|---|---|---|---|
| 2 GiB | 4.2% | 110,847 | 1.11x | 4.7x |
| 8 GiB | 16.8% | 96,289 | 1.27x | 5.4x |
| 16 GiB | 33.6% | 76,878 | 1.59x | 6.8x |
| 32 GiB | 67.1% | 38,056 | 3.22x | 13.7x |

Two conclusions from that table, both of which change the design:

* **Capacity is a worker-count problem, not a cache problem.** Two workers already clear the
  requirement with a *zero-hit* cache. So the access method must be chosen on predictability
  and RAM accounting, not on throughput. Anyone tempted to pick page cache "for the speed"
  should note that four workers beat a 16 GiB cache at zero extra RAM.
* **The bonus and the purpose are in direct conflict.** A large cache benefit costs 16-32 GiB
  of the exact resource the feature exists to recover. On a host too small to pin the table,
  the model predicts a few percent hits.

The C/N result is pessimistic for real workloads because n-gram access is plausibly Zipf-ish;
a small hot set can beat C/N by a lot. That is testable and is the one measurement that could
overturn the choice of default: capture row IDs from a real prompt stream, count distinct
pages in the working set, or `mincore()` a resident cache and read the hit rate directly.

### Correcting the premise that page cache avoids the mmap penalty

It does not, not by being page cache. The penalty is **kernel readahead on random 4 KiB
reads**, and it attaches to how the file is touched. A plain buffered `read()` in a loop gets
readahead too. `MADV_RANDOM` is what defeats it, which is why both sglang's reader and this
repo's `MappedTable` call it. So the design requirement is explicit read sizes plus advice
discipline; "page-cache-backed" without them is the naive-mmap case wearing a different hat,
and on a tight host it is the worst of all three -- readahead amplification, cache-churn
overhead, and no device-level control.

### One risk I raised and then retracted

Page cache on a low-RAM host was characterised here as dangerous. For *clean file-backed
pages* that is wrong: they are the cheapest memory the kernel holds, reclaimed with no I/O and
no swap. The real costs are the extra copy (page cache to user buffer to pinned staging),
per-page LRU and PTE work at 116k pages/s, and refill latency when a wanted page was just
evicted. Every near-disaster this session came from *anonymous* memory -- the 64 GiB
`cudaHostRegister` that killed the serving engine -- and anon is what swaps. That asymmetry
argues **for** page cache, and it is why it is the default rather than the fallback:

* **default: page cache**, with `MADV_RANDOM`, an explicit 4 KiB read unit, and a bounded
  footprint via `posix_fadvise(DONTNEED)` if it has to be bounded;
* **`O_DIRECT` as the predictable option, not the worst case**: no cache pollution in either
  direction, one fewer copy when reading straight into a registered buffer
  (`IORING_REGISTER_BUFFERS`), and latency that depends on the drive rather than on the whole
  machine's memory state. For a decode-step deadline, low variance beats a good mean.

### Two non-negotiable measurement requirements

1. **Verify that `O_DIRECT` is actually direct.** Measured this session: on btrfs, `O_DIRECT`
   *silently falls back to buffered* on compressed extents. `gg`'s FP8 table happened to be
   honest -- 98% of reads hit the device, `du` ratio 1.0000 -- because zstd gives up on FP8
   bytes, but a BF16 table does compress, so an `O_DIRECT` arm on BF16 could quietly be a
   buffered arm reporting device numbers. Every direct run carries a `/proc/diskstats` delta.
   Without it the flag is a label, which is how the original plan ended up with fake numbers.
2. **Run the streaming arms under a memory cap.** `gg` sits at 49-65 GiB spare, so a
   page-cache-backed prototype on this box flatters itself into meaninglessness for the hosts
   the feature is for. Rootless `podman --memory` was proven to enforce (rc=137 inside the
   cgroup, host `MemAvailable` unmoved), so the simulation is honest. Report the number under
   the cap, not beside it.

That makes the streaming matrix: **pinned, stream-pcache, stream-odirect, each at full and
capped memory** -- six configurations. The cap is the only way the answer transfers off this
machine.

### Sequencing: measure the read path before integrating it into the engine

Full-size streaming needs ids on the CPU, which means the n-gram hashing moves off the GPU or
the ids come across a synchronisation point -- that is engine work with its own hazards. The
access-method question does not depend on any of it. So it splits:

* **B-i, read-path prototype.** Standalone: replay row-id streams (uniform, and Zipf-sampled
  to bracket real skew) through a CPU gather into pinned staging at the measured ~116k pages/s
  target, for both access methods, at 1/2/4/8 workers, under both memory settings. Measures
  pages/s, p50/p99 read latency, CPU%, and sustained-versus-transient. No engine restarts, no
  correctness surface, and it can run on either host.
* **B-ii, engine integration**, only once B-i says a configuration can sustain the rate.
  Behind a flag, with the same determinism check that made arm B credible -- byte-identical
  greedy output, because resource statistics cannot prove the gather is reading the right rows.

## Statistics: this is a non-inferiority test, not a speed test

B is expected to be neutral and C is allowed to be slower, so the question is "by how
much, and is it outside the noise". The measured per-point spread on the baseline receipt
is **2.3% to 10.5%** depending on concurrency, with c16 at 5.2% across 3 runs. Three runs
therefore resolve roughly +/-3% and nothing finer.

* **Paired and interleaved**, `A' B A' B ...`, never blocked, so slow drift (thermal,
  drive state) does not become an arm effect.
* **A null control is mandatory**: `A'` measured twice brackets the comparison and gives
  the noise floor directly. Without it, a 2% difference is uninterpretable.
* Report medians and spreads, not means; state the paired-difference interval; pick the
  harm threshold in advance (suggest 3% on decode, 5% on prefill) and report pass/fail
  against it rather than dressing up small deltas.
* N=9 per arm per point for decode at the flagged concurrencies; N=3 elsewhere, since
  prefill variance is lower and that arm is slower.

## Hygiene, in order of how much it would have cost us

1. **The `uncompressed` requirement is O_DIRECT-specific, and arm C does not obey it.**
   The checkpoint-mapped path reads by demand paging, so the kernel decompresses
   transparently -- compression costs that arm CPU cycles rather than silently faking its
   numbers, and `chattr +C` buys it nothing. Any streaming-doc warning about uncompressed
   tables must say which access method it applies to, or it will send the next reader to
   fix a problem that arm C does not have.
2. **Never benchmark a freshly written table file.** The PM9A1-class SLC region is
   ~236 GB, which swallows both the 47.7 GiB FP8 table and the 95.4 GiB BF16 table whole,
   so a streaming prototype measured against a just-copied checkpoint measures the drive's
   write cache. Either use the production checkpoint as-is (what the `gg` disk numbers
   did), or write and then wait hours.
2. **No heavy writes on the drive shortly before an arm.** Read latency at shallow queue
   depth is placement/cache dependent and was observed to differ by ~40%.
3. **Co-located client, always**, and a `runtime.json` per arm. Several historical result
   directories have no receipt at all, which is how the host of a number went unnoticed
   for weeks.
4. **Same session for all interleavings** -- an arm separated from its pair by a reboot is
   not paired.

## Cost

Two builds (B-image, then C-image). Per arm: restart ~4 min, decode ladder ~15 min,
prefill ladder ~25 min with the long-context points. With N=9 decode and the paired
design, roughly **half a day** including both builds and the C-only instrumentation. The
B comparison is the cheap and decision-relevant half -- one build, one env var, and a
claim about pinned bytes that is already provable from `/proc` in seconds.

## Target hosts, measured footprint, and two of my own conclusions I had to retract

The goal is stated by host size, not by throughput: **make a 16 GiB host able to serve this
model at all, make 32 GiB workable, and make 64 GiB comfortable with RAM left for other
work.** That reframes the access-method question, and it exposed two errors in how I had been
presenting my own measurements.

### What the engine costs in host RAM with the table pinned, measured

On `gg` while serving (`Shmem 66.18 GiB` = the 64.000 rounded pin plus the 2.000 embedding pin):

```
  MemTotal 125.68   MemAvailable 48.83   Shmem 66.18   swapped 1.98
  non-table host footprint = 125.68 - 48.83 - 66.18 = 10.67 GiB resident  (+1.98 swapped)
```

Of that ~12.6 GiB, only ~5.8 GiB is attributable to process RSS -- worker heap 1.81, worker
anonymous 1.03 across 815 mappings, libraries 0.24, nvidia driver 0.21, API server 1.28, engine
core 1.18 -- and **the checkpoint is not among them**: no category for checkpoint mappings
appeared, so the loader is not holding the weights mapped resident. The remaining ~5 GiB sits
in kernel and driver allocations outside process RSS.

This matters because it is the floor a streaming build cannot go below. It is a separate
problem from the table, and it is the one that decides whether 16 GiB is real.

### Retraction one: the page-cache advantage is not 11%

The matrix measured ~11.5% hit rates and I wrote that page cache beat `O_DIRECT` by 11-30%.
That number is a **single-pass artifact**: each cell issued ~1.6M requests over a 2.1M-page
region, so almost every page was cold by construction. Steady-state serving re-reads the table
across requests, and there the hit rate is the cache fraction, `C/N`, which is a completely
different figure at these host sizes:

| host | minus 12.6 GiB floor | page cache for the table | steady-state hit | device load (of 128k-170k pages/s) | threads / cores |
|---|---|---|---|---|---|
| 16 GiB | ~3.4 GiB, less the OS | 3.4 GiB of 47.68 | **~7%** | 119k-158k | ~8-10 / ~2.5 |
| 32 GiB | ~19.4 GiB | 19.4 GiB | **~41%** | 76k-100k | ~5-6 / ~1.2-1.6 |
| 64 GiB | ~51.4 GiB | whole table | **approaches 100%** | near zero once warm | warm-up only |

At 32 GiB the device carries 60% of the requirement, which the measured 163,834 pages/s at 8
threads covers with room. At 64 GiB the table simply lives in RAM -- as page cache, not pinned.
Warm-up cost is one full pass: 12.5M pages at ~130k pages/s is about 96 s of background reading.

### Retraction two: "the cache bonus costs the RAM you came to save" is wrong for page cache

I argued that a large cache benefit requires spending the memory streaming exists to free. That
is true of *pinned* memory and false of page cache, which is the whole point of the design:
clean file-backed pages are reclaimed under pressure with no I/O and no swap, so the cache
grows to fill idle RAM and shrinks the moment something else needs it. A 64 GiB host therefore
gets near-total hits **and** leaves room for other work -- the two are not in competition, which
is exactly why page cache beats a pinned table rather than merely substituting for it.

The consequence for `O_DIRECT` is decisive. On a 32 GiB host it would forfeit ~41% of traffic
that would otherwise never reach the drive, on a filesystem where it does not even stay direct
(1.16x amplification, 495 MiB of cache populated anyway). It survives as an option for jitter
control on the tightest hosts, where the cache is worth ~7% anyway, and for nothing else.

### The gating question for 16 GiB is not streaming

With a 12.6 GiB non-table floor plus the OS, a 16 GiB host has ~2 GiB of usable cache and no
headroom for the allocator slop and driver mappings that a bigger GPU may add. Streaming makes
47.684 GiB of table invisible to the memory budget; it does not touch the 12.6 GiB underneath
it. So the honest sequencing is: **32 GiB is the realistic floor for this engine today**, and
16 GiB needs a second, unrelated piece of work -- shrinking the engine's own host footprint,
starting with whatever the ~5 GiB of unattributed kernel/driver memory is.

## CUDA graph cost, and why it did not gate streaming (measured 2026-10-12 00:49 CDT)

Step 1 of the streaming path was to bound the CUDA-graph unknown: a CPU gather cannot live
inside a captured graph, so `ENFORCE_EAGER=1` measured the worst case of running without them.

| concurrency | graphs on (B2) | eager | change |
|---|---|---|---|
| 1 | 128.99 | 18.31 | −85.8% |
| 4 | 370.86 | 70.96 | −80.9% |
| 8 | 587.06 | 138.16 | −76.5% |
| 16 | 786.38 | 264.54 | −66.4% |

The cell is internally valid -- nine runs spanning 17.3-19.2 tok/s at c1, receipt records
`mtp_tokens: 3` and `kv_cache_dtype: fp8`, same image and `max-model-len 262144` as the
graphs-on comparison. **Attribution is not resolved**: eager in this stack may also change
kernel selection (the engine logs FlashInfer GDN kernel choices at boot), so −86% must not be
quoted as "the value of CUDA graphs". One unexplained detail: `Shmem` read 52.03 GiB under
eager against 49.78 for the same exact-size config, ~2.25 GiB matching the transient
page-lock normally released after load.

**It does not gate the design, because the design should not split the graph.** The gather's
inputs are `input_ids`, `query_start_loc` and `ngram_context` (`compute_ngram_ids`,
ngram_embedding.py:539-562) -- all three are known on the host *before* the step replays,
including MTP drafts, which are produced by the previous step and copied out before the next
one. So the gather belongs before replay, writing into a fixed-capacity pinned staging buffer
backed by a persistent device buffer, exactly the pattern `PagePrefetcher` already uses for
its own inputs (`self.capacity = (max_num_tokens, max_num_reqs, ngram_context_len)`,
`pin_memory()` slots at :471-474, `copy_(..., non_blocking=True)` at :485-488). The graph stays
whole and the gather overlaps the GPU executing the previous step.

Corollary on CPU budget: eager spends ~47 ms/step of host time on kernel-launch work that
graphs eliminate. With graphs on, that capacity is free, and the gather needs at most 2.7 ms
per step cold, 0.17 ms warm. Piecewise graphs, the thing vLLM #54129 needed, may not be
necessary here at all -- which removes the largest implementation risk identified earlier.

What is left, in order: measure real reuse statistics (the cold fraction is worth 16x per
page), then the prototype.

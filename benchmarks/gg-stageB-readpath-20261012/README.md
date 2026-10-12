# Stage B-i: read-path matrix on `gg` — page cache vs O_DIRECT, thread scaling, memory caps

24 cells. Standalone CPU-side gather against a real checkpoint blob, no engine in the loop.
This is the measurement that decides how a streaming PLE should read, and whether a Python
submit path can carry the rate at all.

* Host `gg`, Samsung 990 PRO 4TB, btrfs `compress=zstd:1,discard=async`, ~49-65 GiB free.
* Blob: 8.1 GiB FP8 shard file (`blobs/23224da1…`), whole-file region, 4 KiB read unit
  (`st_blksize`), `row_bytes` 160, 48 steps × 2,048 tokens × 16 rows = ~1.61M page requests
  per cell.
* Target 132,734 pages/s = `gg`'s own measured 32,768-token prefill (8,028 tok/s) × 16.5333
  pages/token. The hardest case in the set is the 2,048-token prompt at 170,126 pages/s.
* Capped cells run inside `podman --memory Ng --memory-swap Ng`, which was separately proven
  to enforce. Every cell runs `--evict-first --require-cold`, so a warm start aborts instead
  of reporting page cache as device traffic.
* `scripts/bench-ple-readpath.py`, driven by `scripts/run-stageB-readmatrix.sh`.

## Results

pages/s of *requests* (the unit the ceilings are in), page-cache hit rate, device pages per
request, and CPU cost:

| threads | setting | pcache | odirect | pcache−odirect | pcache hits | pcache fs amp | odirect fs amp | cpu ms/1k p/od |
|---|---|---|---|---|---|---|---|---|
| 4 | uncapped | 101,726 | 78,499 | **+29.6%** | 11.8% | 0.88x | 1.16x | 9.4 / 10.2 |
| 4 | 4 GiB cap | 95,170 | 77,445 | +22.9% | −3.9% | 1.04x | 1.16x | 10.2 / 10.7 |
| 4 | 8 GiB cap | 98,392 | 77,535 | +26.9% | 11.9% | 0.88x | 1.16x | 10.1 / 10.7 |
| 8 | uncapped | 163,834 | 138,264 | +18.5% | 11.5% | 0.89x | 1.16x | 12.6 / 12.5 |
| 8 | 4 GiB cap | 151,478 | 136,343 | +11.1% | −4.5% | 1.04x | 1.16x | 13.8 / 13.0 |
| 8 | 8 GiB cap | 160,030 | 135,496 | +18.1% | 11.7% | 0.88x | 1.16x | 13.2 / 12.9 |
| 12 | uncapped | **182,535** | 163,853 | +11.4% | 11.7% | 0.88x | 1.16x | 15.0 / 15.9 |
| 12 | 4 GiB cap | 164,494 | 167,900 | −2.0% | −4.3% | 1.04x | 1.16x | 17.9 / 16.5 |
| 12 | 8 GiB cap | 171,127 | 163,348 | +4.8% | 11.8% | 0.88x | 1.17x | 17.4 / 17.1 |
| 16 | uncapped | 157,155 | 140,634 | +11.7% | 11.5% | 0.89x | 1.16x | 16.5 / 18.1 |
| 16 | 4 GiB cap | 151,494 | 159,254 | −4.9% | −4.1% | 1.04x | 1.16x | 19.7 / 19.8 |
| 16 | 8 GiB cap | 156,332 | 140,735 | +11.1% | 11.9% | 0.88x | 1.16x | 18.9 / 20.8 |

## What it says

**1. The page-cache bonus is real, small, and exactly as proportional to spare RAM as the C/N
model predicted.** Uncapped it is worth 11–12% of requests as hits, which decays to *nothing*
under a 4 GiB cap — where the hit rate computes to −4%, i.e. the device served more pages than
were requested and the cache contributed zero. Under a 8 GiB cap on an 8 GiB region it survives
(11.7%), because the region fits. The advantage over `O_DIRECT` shrinks monotonically with the
submit path's own scarcity: +29.6% at 4 threads, +18.5% at 8, +11.4% at 12, and it *reverses*
(−2.0%, −4.9%) at 12–16 threads under the tight cap, where both methods are device-bound and
`O_DIRECT`'s extra copy stops mattering while page-cache management still costs CPU.

**2. Thread-per-synchronous-read scales to about 8–12 and then goes backwards.** Against the
`fio` qd1 figure for this drive (19,859 IOPS/thread), the measured rates are 1.28× the linear
sum at 4 threads (cache hits, so above the model is expected), 1.03× at 8, then **0.77× at 12
and 0.49× at 16**. Past ~8 threads, GIL and scheduler overhead are subtracting throughput. So
the gather is not "infinitely threadable" in Python, and the peak is ~182k pages/s — a
long way under the 318k that linear scaling would have promised. This is the quantitative
version of why sglang's reader is Rust with persistent `io_uring` rather than a thread pool:
not because threads cannot reach the rate, but because they reach a ceiling with a knee.

**3. Cost: about 2.7 cores for the peak cell** (15.0 ms CPU per 1k pages × 182.5k pages/s =
2,732 ms CPU per second). At the 12-thread capped figure it is 2.9 cores. That is a real
budget on a host whose whole purpose is serving tokens, and it is paid on the CPU that also
does tokenisation and scheduling.

**4. `O_DIRECT` pays a constant 1.16× device tax and a constant 495 MiB of page cache.**
Across all twelve `O_DIRECT` cells the device moved 1.16× the requested pages and the page
cache grew by 494–497 MiB, independent of thread count and of memory cap. A constant that
insensitive to its own variables is a property of the filesystem path, not of the workload:
btrfs is reading more than requested and is populating cache for a mode that asked not to.
The `pcache` cells by contrast show 0.88× (fewer device pages than requests — the hits) and
1.04× under the tight cap. So `O_DIRECT` here is neither strictly direct nor strictly
cache-free, and it is the *slower* option in every uncapped cell.

**5. Neither method has margin at the hard end.** Best cell overall is `pcache`/12
threads/uncapped at 182,535 pages/s = 1.38× the 32k-prompt requirement but only **1.07×** the
2,048-token one (170,126). Capped, the best is 171,127 (1.01×) and 164,494 (0.97×, short). A
streaming gather at this rate would not merely keep up with `gg`'s prefill, it would
occasionally *be* the limit, and the prefill tok/s it can feed is `pages/s ÷ 16.5333`:
182,535 → 11,040 tok/s uncapped, 164,494 → 9,949 tok/s capped, against a native 8,028–10,290.

## Caveats that bound how far these numbers travel

* **Self-overlap is inflated here.** 1.61M requests over a 2.1M-page region repeats 30% of
  pages, so part of the 11.7% hit rate is an artifact of re-reading a small region. A real
  261,632-token prefill makes 4.33M requests over 12.5M pages and repeats 15.5%; a 32,768-token
  one repeats 2.1%. So read the uncapped hit rate as an *upper bound* on self-overlap benefit
  for long prefills, not as an expectation. Cross-request reuse of hot n-grams is a real effect
  in the other direction and this instrument cannot see it — that needs a captured trace.
* **Region is one 8.1 GiB blob, not the 47.684 GiB table**, because the table is 128 shards of
  0.37253 GiB across all 22 files. Residency *fraction* is scale-free, which is why this is
  still a valid probe of the capped regime, but multi-shard fd handling is untested here.
* **Single drive, single filesystem, single host**, with the serving engine idle but present
  (`wait_idle` gates each cell on zero in-flight requests; background device traffic was
  measured at 0 p/s per cell).
* **Python, not the implementation language stage B would choose.** These are ceilings for a
  threaded synchronous design, not the ceiling of what a gather can do.
* `p50` sits at 47–63 µs and `p99` ranges 74–385 µs; the fat `p99` tail belongs to `O_DIRECT`
  at 16 threads and to the capped `pcache` cells, which is the opposite of the usual
  expectation that `O_DIRECT` is the low-variance option. Worth re-measuring before treating
  that as settled.

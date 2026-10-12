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
| **C** `stream` | checkpoint-mapped PLE table (`port-ple-mmap`, upstream PR 58439 + 58835) | a build that applies those ports |

Two consequences of that table that change the schedule:

* **A and B share one image.** The fair baseline is therefore *not* the existing
  receipt -- that was taken on the current image, which carries neither the port nor
  the `reject_exact_repin` import in `model_loader/utils.py`. Re-measure A on the
  B-image with the flag off (`A'`) and interleave `A'`/`B`. The build is the expensive
  part; the arm is one env var and a restart.
* **C needs a second build anyway.** The installed image contains **zero** mmap-related
  lines in `ngram_embedding.py`, so streaming is not a config flip. Since B's build is
  needed first, build in that order and keep the delta between images to exactly the
  mmap ports.

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

1. **Prefill burst shape.** This is where streaming is most at risk. A 261,632-token
   prompt at 16.5333 pages/token is ~4.33M page reads, about **66 GiB of table traffic
   for the FP8 table** in one pass, and it must be served inside the compute time of
   that prefill rather than spread across decode steps. The plan's whole "2048-token
   chunk in ~3.6 s" assumption is a statement about this arm and gets tested nowhere
   else.
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

1. **Never benchmark a freshly written table file.** The PM9A1-class SLC region is
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

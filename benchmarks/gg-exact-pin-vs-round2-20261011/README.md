# exact-size pin vs torch round-to-power-of-two, on `gg`

First `gg` receipt for the pinned-PLE comparison, and the first measurement of the
exact-size allocation (`PLE_PIN_STRATEGY=exact`, `patches/qwen_pinned_host.py`) inside the
serving stack rather than in a probe.

* Host: `gg`, RTX PRO 6000 Blackwell 96 GB, Samsung 990 PRO 4TB, btrfs, ~126 GiB RAM.
* Profile: `exl3-ple8`, MTP 3, FP8 PLE table (47.684 GiB of table data).
* Image: `localhost/qwen38-exactpin:local` for **both** arms, so the vendored port is present
  in the control too and the single variable is the allocation strategy.
* `LONGCTX=0` (native 262,144) on both arms; YaRN off.
* Arms: `A1/A2` = `round2` (torch `pin_memory`, rounds 47.684 -> 64.000 GiB),
  `B1/B2` = `exact` (`cudaHostAlloc` of the true size).
* Decode: `--concurrency 1 2 4 8 16 --output-tokens 256 --runs 9 --request-mode clients`.
* Prefill: `--prompt-tokens 2048 8192 32768 131072 261632 --runs 2`.
* Summarise with `scripts/analyze-pin-pair.py <dir>`.

## Memory: the actual result

| | A (`round2`) | B (`exact`) | delta |
|---|---|---|---|
| worker pin mappings | 64.000 + 2.000 = 66.000 GiB | 47.684 + 2.000 = 49.684 GiB | **-16.316 GiB** |
| `Shmem` | 66.10 / 66.10 GiB | 49.78 / 49.78 GiB | -16.32 GiB |
| `MemAvailable` | 48.93 / 49.28 GiB | 65.54 / 64.34 GiB | **+16.2 GiB** |
| loader line | (none) | `PLE weight pinned at exact size 47.684 GiB` | |
| model load, seconds | 171.3 / 170.2 | 122.3 / 126.0 | **-27%** |

The recovered 16.316 GiB is exactly `64.000 - 47.684`, i.e. the rounding model predicted the
measurement rather than merely surviving it. `MemAvailable` moves slightly less because the
allocation itself is not the only difference at load time.

Greedy output is byte-identical between arms and to the pre-experiment baseline:

```
', in a magical land far away, there lived a little a'
' Paris. The capital of Germany is Berlin. The capita'
```

That is the correctness proof, and resource statistics cannot substitute for it: a wrong
offset, a mis-typed view, or a freed-and-reused mapping would change tokens under real load.

## Throughput: no measurable difference, in either direction

No row survives as an effect once judged against the arms' own boot-to-boot spread. The
largest apparent moves are `prefill 2048` at -6.51% (B's own spread there is 12.24%) and
`decode c4` at +3.25% (B spread 3.27%). The `+8.26%` at TTFT/2048 is 20 ms of a 200 ms
measurement. See the flags the analyzer prints rather than the percentages.

**Protocol deviation, stated plainly:** `B1` reused the boot already running rather than
being a fresh boot, to save a four-minute restart. It reached serve in 80 s against 230-270 s
for the other three, and it is the source of both the `c16` high outlier (828.94) and the
`2048` low one (9031). `B`'s spread is 2.9-12.2% against `A`'s 0.1-6.2%, and part of that gap
is this asymmetry rather than the allocation strategy. `A2` vs `B2` is the clean fresh-vs-fresh
pair; it agrees with the null verdict. A future run should boot all four cells identically.

What can be claimed: **exact-size is throughput-neutral within this resolution (+/-3-6% at
these repeat counts) while returning 16.316 GiB of host RAM.** What cannot: any speed *gain*.
The 45 s load-time reduction has a plausible mechanism (16 GiB fewer pages to allocate and
page-lock) and reproduced on both boots, but three of four cells differing in warm-up history
is not the design this comparison was built to establish.

## The streaming requirement, restated from `gg`'s own numbers

16.5333 pages/token (FP8) against `A`-arm measured prefill, versus the measured ceilings of
122,571 IOPS on one submitting worker and 523,000 on four:

| prompt tokens | prefill tok/s | pages/s required | 1 worker | 4 workers |
|---|---|---|---|---|
| 2,048 | 10,290 | 170,126 | **0.72x -- short by 39%** | 3.1x |
| 8,192 | 8,626 | 142,613 | 0.86x -- short | 3.7x |
| 32,768 | 8,028 | 132,734 | 0.92x -- short | 3.9x |
| 131,072 | 7,962 | 131,632 | 0.93x -- short | 4.0x |
| 261,632 | 7,768 | 128,432 | 0.95x -- short | 4.1x |

`gg` runs prefill ~25-45% faster than the `burst` receipts the earlier estimate came from, so
the borrowed figure **understated** the requirement: a single submitting worker is short at
every prompt length, not marginally adequate at long ones. Two workers are necessary
(1.5-1.9x headroom); four are what the design should assume. Decode is a different regime
entirely -- 784.51 tok/s at c16 is 12,971 pages/s, 9.4x under one worker -- which is why the
sizing case is prefill and not decode.

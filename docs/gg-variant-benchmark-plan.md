# Benchmark plan: pinned round2 vs exact-size vs streaming, all on `gg`

This was the planning document for the gg pin/stream campaign. The pre-execution speculation has been
trimmed; the plan as executed, every measurement, and every retraction live in
`docs/ssd-stream-findings.md`. What remains here is the verdict record, kept because the ladder table in
it is the paired, envelope-quoted summary of the campaign.

# Verdict update after v2 landed (2026-10-13, early)

Streaming is servable, on a host with less RAM than the table, at a measured cost. What changed since the
earlier verdicts in this document is that the cost is now attributable to named things instead of to
"streaming".

**Measured on `gg` (125.68 GiB RAM, RTX PRO 6000 Blackwell), FP8 table, identical graph mode
(`FULL_AND_PIECEWISE`, breakable auto-enabled by architecture) in every cell:**

| | pinned | v2 streaming (sync) | delta |
|---|---|---|---|
| Shmem | 49.86 GiB (`exact`) | **2.21 GiB** | −47.65 GiB |
| decode | 139.0 tok/s | **135.1 tok/s** | −2.8% |
| prefill | 11,347.3 tok/s | **11,073.1 tok/s** | −2.4% |
| greedy output | `7c1421daade83d79` | `7c1421daade83d79` | identical |

Paired re-run on 2026-10-19 under one recorded envelope (`-pl 300`, header printed before the cells; see the
dated section in `docs/ssd-stream-findings.md`). The earlier same-host figures in this table (decode −7.4%,
prefill −15.1%, hash era `a6cc8fed…`) predate envelope recording and are kept as the historical reading;
the direction agrees, the magnitude does not, and the paired run is the one to quote.

v1 (gather inside the captured region, so `cudagraph_mode=PIECEWISE`) cost −22.2% decode and −15.5%
prefill, of which **17.8 points of decode were the graph mode alone**, measured with streaming off. That is
the whole argument for staging before replay, and it is now the reason streaming keeps FULL graphs. (Operator-facing surface since: one knob, `PLE_STREAM=1`; the internal pair survives for images built before the consolidation.)

**What "smaller host" means here, stated as a claim and as its limits.** The table no longer needs to be
pinned, so the RAM floor is roughly the token-embedding pin (2.0 GiB rounded) plus whatever page cache the
operator wants. The hit-rate curve that justifies a cache budget — 0.744 at 8 GiB, 0.797 at 16 GiB, which
matches a host that caches the entire table — was measured on **synthetic** corpora, and every number in
the table above was measured on a 125 GiB host where the whole 47.7 GiB table sits in cache. So:

* Measured: streaming works, is byte-identical on gated prompts, costs 7.4% decode / 15.1% prefill **with
  a fully warm cache**, and frees 64 GiB. (Superseded on 2026-10-19 by the paired re-run under one
  recorded 300 W envelope: −2.8% decode / −2.4% prefill; the pre-envelope pair mixed power ceilings,
  see the dated sections in `docs/ssd-stream-findings.md`.)
* Not measured: a host whose cache is smaller than the working set, where misses go to the device at
  ~132.7k pages/s required against 85.6k measured for O_DIRECT; a real traffic trace rather than synthetic
  reuse; and whether the 15.1% prefill loss is dominated by the D2H wait, the CPU gather, or the H2D — the
  per-phase instrumentation added tonight answers that, and its first reading is in the findings document.

A 32 GiB target therefore rests on the synthetic reuse curve and a warm-cache cost measurement, not on a
32 GiB host. The preflight says so out loud (`PLE_STREAM_CACHE_WARN_GIB` warns below 16 GiB and the
warning names its own provenance as synthetic), which is the honest form of a provisional floor.

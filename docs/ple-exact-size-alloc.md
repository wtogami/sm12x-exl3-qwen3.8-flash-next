# Implementation assessment: exact-size pinned PLE allocation

Assessment only. Nothing here is implemented; the branches carrying it are
documentation. Evidence for every measured claim is in
`docs/ssd-stream-findings.md` sections 1, 1b, 1c.

## What it replaces

`torch.empty(..., pin_memory=True)` sends every pinned request through
`PowerOf2Ceil()`, so the PLE n-gram table costs 128.000 GiB of pin for 95.368 GiB
of data on BF16, and 64.000 GiB for 47.684 GiB on the FP8 profile that serves
today. `cudaHostAlloc` does not round, and a `cudaHostAlloc` buffer wrapped so
that torch reports it as pinned is aliased by
`get_cuda_view_from_cpu_tensor` with no copy. Measured at full scale on `gg`:
`same_addr=True`, `MemAvailable` delta `-0.00 GiB`, write-through sentinel
correct.

Recovered RAM: **32.632 GiB** (BF16), **16.316 GiB** (FP8, the profile serving
now). Recovered startup time, if page-locking scales with bytes as it did
(1.49-2.27 GiB/s measured): today locks 128.000 GiB where this locks 95.368, so
roughly **25-30% of a 56-85 s page-lock** -- an estimate from two samples with
1.5x spread, not a measurement.

This supersedes pow2 tiling. The two are within 0.632 GiB of each other on RAM,
but tiling requires the gather kernel to resolve a row against multiple base
pointers, and exact-size requires no kernel change at all.

## The primary patch site

`vllm/models/qwen4_exp/common/ngram_embedding.py`, `allocate_embedding_weight`
(line 462 in the installed build) is what creates the table:

```python
return torch.empty(num_embeddings, embedding_dim, dtype=dtype,
                   device="cpu", pin_memory=True)          # rounds to pow2
```

Replacing that one return with an owning `cudaHostAlloc` wrapper is the whole
allocation change:

```python
_OwnedArr = {}          # subclass cache, one entry per element type

def _pinned_exact(shape, dtype):
    """A CPU tensor of exactly prod(shape)*itemsize bytes, page-locked by the
    CUDA driver rather than by torch's caching host allocator, that reports
    is_pinned()==True so get_cuda_view_from_cpu_tensor aliases it."""
    nbytes = math.prod(shape) * dtype.itemsize
    p = ctypes.c_void_p()
    if rt.cudaHostAlloc(ctypes.byref(p), ctypes.c_size_t(nbytes), 0) != 0:
        raise RuntimeError(f"cudaHostAlloc({nbytes}) failed")
    arr = _owned_ctypes_array(nbytes)(p)        # __del__ -> cudaFree
    return torch.frombuffer(arr, dtype=torch.uint8).view(dtype).view(shape)
```

The returned tensor becomes the parameter's data, so `self.weight` is already the
strong anchor the owner needs -- the same idiom the token-embedding offload uses
explicitly with `layer._qwen_host_embedding_storage = host`. Nothing new has to
hold it up, and the class already keeps `self._uva_weight` alive beside it.

Two preconditions to assert rather than assume:

* **The CUDA context must already exist.** `is_pinned()` is False for genuinely
  pinned memory in a process that has not initialized CUDA, and the view op
  dispatches `aten::is_pinned`, so a missing context silently selects the copy
  branch -- which asks for a *second* full-size buffer. The worker initializes
  CUDA before building the model, so this holds today; it is cheap to assert and
  expensive to discover later.
* **The tensor must be contiguous.** `frombuffer` output is contiguous by
  construction, and the fallback branch calls `.contiguous()`, so this is free --
  but it is why an exact-size tensor must not be produced by slicing a larger one.

## Two sites that would re-round the table (accepted risk, kept as a tripwire)

`model_executor/model_loader/utils.py` re-pins CPU-resident parameters in the
`finally` of the same context manager that hands out the module:

```python
p.data = torch.empty_like(p.data, device="cpu", pin_memory=use_pin_memory).copy_(p.data)
...
cpu_data = torch.empty_like(p.data, device="cpu", pin_memory=use_pin_memory).copy_(p.data)
p.data = get_accelerator_view_from_cpu_tensor(cpu_data)
```

`torch.empty_like(..., pin_memory=True)` rounds again. Both paths are guarded --
the first by `name in cpu_params`, the second by "was UVA-offloaded but got
replaced with a new device tensor" -- so neither runs in a normal load. But if one
ever fires on the PLE table, the pin silently becomes 128.000 GiB again, and it
would also *copy* 95 GiB. An implementation has to either route these through the
same exact-size allocator or assert the table never reaches them. `model_executor/
offloader/uva.py:121` has the same idiom and the same caveat.

**Owner judgement, recorded so the guard is not over-read.** The code paths that reach
those branches are a new model release or an experimental quant, and each of those
already requires recipe validation against specific resource limits -- so a re-rounded
128 GiB pin surfaces during validation, not silently in production. `reject_exact_repin`
is therefore a convenience that converts a validation-day OOM into a named error, not
the mitigation for a live hazard. It stays because it costs one set lookup per re-pin.

## The lifetime trap

Verified, not inferred:

* an **owning** ctypes-array subclass handed to `torch.frombuffer` is anchored by
  the tensor. Deleting every other reference does not free it; the `cudaFree` in
  `__del__` fires only once both the CPU tensor and the CUDA view are dropped.
* a **PEP 688** exporter (`__buffer__` / `__release_buffer__`) is released *inside*
  `frombuffer`, so the owner dies immediately while the tensor still hands out the
  address. `is_pinned()` then returns True and the view is created without error,
  both about freed memory; the first read raises `CUDA error: invalid argument`.

So the buffer must be a plain buffer-protocol object with an observable
destructor. Use `__buffer__` and the failure is silent until first touch, in a
95 GiB allocation, at 3 a.m., in a process that looks healthy.

Risk if the ownership chain is wrong is a device-side use-after-free. It is
bounded by deployment, though: the serving launch has no `--enable-sleep-mode`,
so parameters are never released at runtime and a conservative implementation may
simply never `cudaFree` -- a bounded 95 GiB leak at process exit beats any chance
of a dangling device pointer.

## The coupled gate change

`platform-config.sh::qwen38_ple_memcheck` predicts demand with
`qwen38_next_pow2 "$table_kib"`. That is correct for the code as it exists and
wrong the moment the allocation changes, in both directions:

* gate unchanged, exact-size shipped: predicts 128.000 GiB for BF16 on `gg` and
  **refuses a build that needs 95.368** -- a false refusal on the host that
  motivated the change;
* gate relaxed, code unchanged: predicts 95.368 and **accepts a build that will
  pin 128.000** -- which is the recorded 103.4% failure mode.

They must land together, and the gate needs to know which strategy the image
actually uses rather than guessing. The cheapest honest mechanism is a profile
field or exported variable, e.g. `PLE_PIN_STRATEGY=round2|exact` defaulting to
`round2`, with the table rounding made identity only under `exact`. The token
embedding must stay pow2-rounded either way -- it still uses `pin_memory`, so
1.184 GiB continues to cost 2.000 GiB, and `130 GiB` predictions that include it
must keep including it.

Thresholds do not need to move. The refuse-within-`max(8 GiB, 5% RAM)` and
warn-above-70% settings were kept deliberately after a probe reached 77.5%,
because an allocation probe is not a served run; exact-size improves the
numerator and is no reason to loosen the policy.

## Performance expectations, and the only valid comparison

Steady state should be **unchanged**, for a structural reason rather than an
optimistic one: both designs reach the device through the same aliased UVA view of the
same page-locked bytes, so the kernel, the PCIe traffic and the served tokens are
identical. What changes is how much host RAM is committed, not how it is read.

Proving "unchanged" is harder than proving "regressed", because the effect sought is
the absence of one. `fp8` c16 decode in the existing receipts spans 864 median to 993
max -- about 15% -- so a 1-2% difference is invisible below that noise floor and N has
to be raised to match. Requirements for the comparison:

* same image and config, single variable `PLE_PIN_STRATEGY`;
* **same host** -- a decode rate is a property of the machine producing it, and every
  receipt in `benchmarks/` came from `burst` or another box, never `gg`;
* co-located client, plus a `runtime.json` per arm. Several historical result
  directories carry no receipt at all, which is how the host question stayed unnoticed;
* greedy byte-identity checked across arms before timing is compared, since otherwise
  the arms may be benchmarking different token sequences;
* `Shmem`, worker pin mappings and swap recorded per arm. That is the claim actually
  being made, and it needs no benchmark to establish.

Startup cost is out of scope as a concern. Page-locking measured 42.0 s and 63.8 s for
the identical call, so it is neither well-quantified nor decision-relevant, and
exact-size can only reduce it.

The same discipline governs the streaming comparison, where some loss is an accepted
trade: streaming is a survival path for hosts whose GPU VRAM exceeds their system RAM,
not a throughput play. The deliverable there is a measured cost against the `gg` pinned
baseline -- never against a foreign host's number.

## Tests to write with the change

`scripts/test-ple-memcheck.py` currently asserts BF16-on-`gg` refuses. Under
`exact` that verdict flips, so the matrix needs the strategy as an input, with a
mutation for "strategy says exact but the table is still rounded" and the
converse -- the failure worth catching is the gate and the allocator disagreeing.

A lifetime regression test is worth having in the repo because the trap is
invisible: allocate a small exact-size tensor, alias it, drop every reference
except the CUDA view, read through it, then drop the view and assert the free is
observed. That is the shape of the test that failed to fail when it was written
against a non-owning `from_address` buffer -- the owning destructor is what makes
the assertion mean anything.

## Suggested staging

1. A helper plus the single `allocate_embedding_weight` return, behind
   `PLE_PIN_STRATEGY`, defaulting to today's behaviour.
2. The gate and its test matrix in the same commit; they are one change.
3. Assert-not-assume for the two re-pin paths in `model_loader/utils.py`.
4. Measure `gg` pinned baseline vs `gg` exact-size on the same host (startup time is
   explicitly not a concern; steady-state non-regression is).

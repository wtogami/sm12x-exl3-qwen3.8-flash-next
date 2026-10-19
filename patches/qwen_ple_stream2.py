"""PLE streaming v2: stage each step's rows before replay so the graph stays whole.

Why a second design
-------------------
v1 does the row gather inside ``_lookup``, which runs inside the captured region. Host code cannot be
captured, so v1 needs breakable graphs, which need ``cudagraph_mode=PIECEWISE``, which gives up whole-step
FULL graphs for decode. Measured on this host: giving up FULL graphs costs **17.8% of decode with
streaming switched off entirely** (162.8 -> 133.9 tok/s, cell spreads 1.1% and 1.6%), and that is most of
v1's 22.2% decode loss. The gather itself is not the expensive part -- the graph regime it forces is.

v2 moves the gather to ``Qwen4ExpModelState.prepare_inputs``, which the engine already calls eagerly on
every step and which already hands the page prefetcher the step's ``input_ids``, ``query_start_loc`` and
``ngram_context``. Rows land in a persistent device buffer *before* replay; ``_lookup`` becomes a
device-to-device copy, which captures fine. FULL graphs stay on, and freshness rides on the same property
input_ids already rely on: prepare_inputs for step N fills the buffers that step N's replay reads.

The honest open question is the ids. ``input_ids`` lives on the device, so the host needs a D2H, and its
cost depends on whether that copy can be ordered independently of the previous step's kernels. Both paths
are implemented and counted rather than guessed at:

* ``sync``: enqueue the D2H, wait on an event, gather, H2D. Simple, stream-order-safe, but if the event
  sits behind the previous step's work the CPU stops overlapping the GPU.
* ``async``: hand the D2H to a worker thread (what the page prefetcher does) and let the fill land
  concurrently. No CPU stall, but a fill that misses its step means the graph would read the previous
  step's rows -- which is why every staged row carries a sequence tag and the lookup zeroes anything not
  tagged for the current step, and increments a visible counter. Silent staleness is the one outcome this
  design is not allowed to have.

Selection: ``PLE_STREAM_V2=1``. ``PLE_STREAM=1`` alone keeps v1 so the two can be A/B'd on one host.

STATUS: implemented but NOT wired and NOT measured. Nothing calls ``stage_step_rows`` yet, ``_lookup``
still runs v1's path, and the pinned qsl/ctx slot capacities below are guesses (16384 x 64) rather than
the model state's real ``max_num_reqs`` / ``ngram_context_len`` -- they fail loudly on overflow rather than
silently, but they must be sized from config. Remaining work, in order: (1) size buffers from the model
state, (2) add the ``prepare_inputs`` hook and the v2 ``_lookup`` to port-ple-stream.py, (3) stop
start.sh implying breakable+PIECEWISE under v2 so FULL graphs survive, (4) build and run the matrix
streaming cell, where the falsifiable predictions are decode ~= 160 tok/s (v1: 126.7) and Shmem ~= 2.18
GiB (v1: 4.44, the difference being the ~2.25 GiB non-FULL-graph surcharge).
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor

import torch

from vllm.logger import init_logger

logger = init_logger(__name__)

MODE_ENV = "PLE_STREAM_V2"
MODE_SYNC = "sync"
MODE_ASYNC = "async"
MODE_ENV_NAME = "PLE_STREAM_V2_MODE"
ARENAS_ENV = "PLE_STREAM_ARENAS"
DEFAULT_ARENAS = 4
LOG_EVERY_ENV = "PLE_STREAM_V2_LOG_EVERY"


LOG_EVERY_DEFAULT = 200


def _log_every() -> int:
    """How often the per-phase averages are logged, in staged steps.

    The phase split (D2H wait / host ids / gather+enqueue) is the only in-engine evidence of where streaming
    time goes, and a short acceptance run can finish before a fixed period of 200 steps ever prints. Reading
    the interval from the environment keeps one boot able to answer both "steady state" (default) and "did
    this shape stage at all" (set it to 1 or 10) without a rebuild. Resolved per stager, at construction,
    which is after the launcher has passed the environment in.
    """
    raw = os.getenv(LOG_EVERY_ENV, "").strip()
    if not raw:
        return LOG_EVERY_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{LOG_EVERY_ENV} must be an integer >= 1, got {raw!r}")
    if value < 1:
        raise RuntimeError(f"{LOG_EVERY_ENV} must be >= 1, got {value}")
    return value


def v2_enabled() -> bool:
    """True when the pre-replay staging design was explicitly asked for.

    Separate from stream_enabled() because the two designs have opposite graph requirements: v1 needs
    breakable graphs and PIECEWISE mode, v2 needs FULL graphs left alone. One flag per requirement set,
    so the loader and the memory preflight cannot disagree about which regime is in force.
    """
    return os.getenv(MODE_ENV, "0").strip().lower() in ("1", "true", "yes")


def v2_mode() -> str:
    """sync: the CPU waits on this step's D2H. async: a worker thread does, at the cost of a staleness path.

    Both are measured rather than assumed; the mode is explicit so a regression in either is attributable.
    """
    m = os.getenv(MODE_ENV_NAME, MODE_SYNC).strip().lower()
    if m not in (MODE_SYNC, MODE_ASYNC):
        raise RuntimeError(f"{MODE_ENV_NAME} must be {MODE_SYNC} or {MODE_ASYNC}, got {m!r}")
    return m


class StepStager:
    """One step's rows, staged into a persistent device buffer before replay.

    Layout is ``[tokens * ngram_heads, row_bytes]`` in exactly the order ``_lookup`` flattens its output,
    so the in-graph work is a positional copy with no index math and no dependence on the id values
    matching between host and device -- if the host staged a different id than the device computed, the
    copy still writes a well-defined row, which is why the sequence tag, not the layout, is what
    guarantees freshness.
    """

    STALE_NO_FILL = -1   # see the tag initialisation in __init__

    def __init__(self, embedding, wrapper, *, max_rows: int, max_reqs: int, ctx_len: int,
                 threads: int = 24, arenas: int | None = None):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from qwen_stream_gather import StreamTable

        self.torch = torch
        self.embedding = embedding
        self.device = embedding._prefetch_buffer.device
        table = embedding.table
        self.row_bytes = int(table.row_bytes)
        self.max_rows = int(max_rows)
        self.mode = v2_mode()
        self.arenas = max(2, int(arenas or os.getenv(ARENAS_ENV, DEFAULT_ARENAS)))
        self._log_every = _log_every()

        self._st = StreamTable(
            [int(v.__array_interface__["data"][0]) for v in table.views],
            rows_per_shard=int(table.rows_per_shard), row_bytes=self.row_bytes,
            threads=threads, rowcopy_so=os.getenv("PLE_ROWCOPY_SO") or None)
        self.vocab_start = int(embedding.shard_indices.org_vocab_start_index)
        self.vocab_end = int(min(embedding.shard_indices.org_vocab_end_index, table.num_rows))

        # Rotating pinned arenas: the CPU must not overwrite an arena whose H2D is still in flight. Two
        # buffers were measured to race (a warm quartile of 5.65 ms with zero faults); this ring is
        # event-guarded for exactly that reason.
        self._host = [torch.empty(self.max_rows, self.row_bytes, dtype=torch.uint8, pin_memory=True)
                      for _ in range(self.arenas)]
        self.dev = torch.zeros(self.max_rows, self.row_bytes, dtype=torch.uint8, device=self.device)
        # Per-row sequence tag, on the device, written by the same stream-ordered copies as the rows so a
        # row and its tag can never disagree.
        # STALE_NO_FILL, not 0: every tag starts as "not staged for any step", so a buffer that was
        # never filled cannot match an unfilled cur_seq and get copied. Async mode proved this is not
        # paranoia -- every fill raised inside InferenceMode, cur_seq stayed at the initial value, and
        # zero-vs-zero matched, so the model consumed uninitialized device memory as n-gram features.
        # The safety property has to live in the tags, not in the ordering of the hooks.
        self.dev_seq = torch.full((self.max_rows,), self.STALE_NO_FILL, dtype=torch.int64,
                                  device=self.device)
        # ...and cur_seq starts at 0, one step BEFORE the first real tag, so the two sides disagree
        # initially. A single sentinel on both sides still matches itself: the first version of this fix
        # did exactly that and my own test caught it. Safety comes from the mismatch, not from the buffer
        # happening to contain zeros.
        self.cur_seq = torch.zeros(1, dtype=torch.int64, device=self.device)

        self._ids_host = torch.empty(self.max_rows, dtype=torch.int64, pin_memory=True)
        # Sized from the model state, not from guesses: an oversized step must be impossible by
        # construction rather than caught by a copy that happens to raise.
        self._qsl_host = torch.empty(int(max_reqs) + 1, dtype=torch.int64, pin_memory=True)
        self._ctx_host = torch.empty(int(max_reqs), int(ctx_len), dtype=torch.int64, pin_memory=True)
        self._mask = torch.empty(self.max_rows, dtype=torch.bool, device=self.device)
        self._ev = torch.cuda.Event()
        self._side = torch.cuda.Stream(self.device) if self.mode == MODE_ASYNC else None
        self._pending: list[tuple[int, object]] = []
        self._slot = 0
        self._seq = 0
        self.stats = {"steps": 0, "rows": 0, "sync_us": 0, "gather_us": 0, "skips": 0,
                      "stale_rows_seen": 0, "max_seq_lag": 0}
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="ple-stage") if self.mode == MODE_ASYNC else None
        self._cpu_ids = None
        self._wrapper = wrapper

    # ---------------- host-side ids ----------------------------------------

    def _ids_fn(self):
        if self._cpu_ids is None:
            self._cpu_ids = self._wrapper.cpu_ngram_ids_fn()
        return self._cpu_ids

    def _copy_inputs_to_host(self, input_ids, qsl, ctx, stream):
        """One D2H of this step's inputs into pinned slots, then `stream` owns the ordering.

        Everything here is small (a few tens of KB) but it is the only unavoidable host round trip in v2,
        so it is counted in stats rather than waved through.
        """
        n = int(input_ids.numel())
        if n > self._ids_host.numel():
            raise RuntimeError(f"step needs {n} id slots, staging holds {self._ids_host.numel()}")
        nr = int(ctx.shape[0])
        if nr > self._ctx_host.shape[0] or ctx.shape[1] > self._ctx_host.shape[1]:
            raise RuntimeError(
                f"step needs {nr}x{ctx.shape[1]} ngram context slots, staging has "
                f"{self._ctx_host.shape[0]}x{self._ctx_host.shape[1]}; the stager was sized from a "
                "smaller model state than the batch the engine is running")
        self._ids_host[:n].copy_(input_ids.reshape(-1), non_blocking=True)
        self._qsl_host[: nr + 1].copy_(qsl[: nr + 1], non_blocking=True)
        self._ctx_host[:nr, : ctx.shape[1]].copy_(ctx[:nr, : ctx.shape[1]], non_blocking=True)
        ev = torch.cuda.Event()
        ev.record(stream)
        return n, nr, ev

    # ---------------- the fill --------------------------------------------

    def _fill(self, ids_np, n_rows: int) -> None:
        slot = self._slot % self.arenas
        arena = self._host[slot]
        owned = (ids_np >= self.vocab_start) & (ids_np < self.vocab_end)
        self._st.gather_to(ids_np, int(arena.data_ptr()), owned=owned)
        self._seq += 1
        stream = torch.cuda.current_stream(self.device)
        self.dev[:n_rows].copy_(arena[:n_rows], non_blocking=True)
        # Tags are written on the device, in the same stream order as the rows, so a row and its tag
        # cannot disagree. fill_ takes a Python int: the earlier version copied a stride-0 expanded
        # pinned tensor, which is not a valid H2D and would have corrupted the tags.
        self.dev_seq[:n_rows].fill_(self._seq)
        self.cur_seq.fill_(self._seq)
        self._slot += 1
        self.stats["rows"] += n_rows

    def stage(self, input_ids, qsl, ctx, num_reqs: int) -> None:
        """Called from prepare_inputs, eagerly, once per step. Never inside a capture."""
        if torch.cuda.is_current_stream_capturing():
            raise RuntimeError(
                "PLE v2 staging ran inside a CUDA graph capture. Staging is host work and belongs in "
                "prepare_inputs, which runs eagerly before replay; reaching it from a captured region "
                "means the hook moved.")
        heads = self._heads()
        n_rows = int(input_ids.numel()) * heads
        if n_rows > self.max_rows:
            self.stats["skips"] += 1
            raise RuntimeError(
                f"PLE v2 staging capacity {self.max_rows} rows exceeded by {n_rows}; refusing to stage "
                "a partial step, because a short buffer would replay as silently missing features")
        if self.mode == MODE_ASYNC and self._side is not None:
            cur = torch.cuda.current_stream(self.device)
            # The side stream must not overtake the writer of input_ids, so it waits on an event recorded
            # where those copies were issued. The wait happens in the worker thread, not here: that is
            # the whole point of async mode -- the CPU does not stop between steps.
            with torch.cuda.stream(self._side):
                n, nr, ev = self._copy_inputs_to_host(input_ids, qsl, ctx, self._side)
            self._pool.submit(self._stage_async, n, nr, int(ctx.shape[1]), ev)
            return
        cur = torch.cuda.current_stream(self.device)
        import time
        t0 = time.perf_counter()
        n, nr, ev = self._copy_inputs_to_host(input_ids, qsl, ctx, cur)
        ev.synchronize()
        t1 = time.perf_counter()
        ids = self._ngram_ids_np(n, nr)
        t2 = time.perf_counter()
        self._fill(ids, n_rows)
        t3 = time.perf_counter()
        self.stats["steps"] += 1
        self.stats["sync_us"] += (t1 - t0) * 1e6      # wait for this step's inputs on the host
        self.stats["ids_us"] = self.stats.get("ids_us", 0.0) + (t2 - t1) * 1e6   # CPU n-gram ids
        self.stats["gather_us"] += (t3 - t2) * 1e6    # row gather + H2D enqueue
        if self.stats["steps"] % self._log_every == 0:
            s = self.stats
            logger.info("PLE stream v2 stats: steps=%d rows=%d d2h=%.0fus ids=%.0fus gather=%.0fus "
                        "(avg per step: %.0f/%.0f/%.0f us) skips=%d",
                        s["steps"], s["rows"], s["sync_us"], s["ids_us"], s["gather_us"],
                        s["sync_us"] / s["steps"], s["ids_us"] / s["steps"],
                        s["gather_us"] / s["steps"], s["skips"])

    def _ngram_ids_np(self, n: int, nr: int):
        import numpy as np
        ids = self._ids_fn()(
            self._ids_host[:n], self._qsl_host[: nr + 1], self._ctx_host[:nr])
        a = ids.numpy() if isinstance(ids, torch.Tensor) else np.asarray(ids)
        return np.ascontiguousarray(a.reshape(-1)[: n * self._heads()], dtype=np.int64)

    def _stage_async(self, n: int, nr: int, ctx_len: int, ev) -> None:
        try:
            # Engine tensors are created under torch.inference_mode(); an in-place update to an
            # inference tensor from a thread that is not in inference mode raises. Async mode produced
            # 526 of these and staged nothing at all.
            with torch.inference_mode():
                self._stage_async_inner(n, nr, ctx_len, ev)
        except Exception:
            self.stats["skips"] += 1
            logger.exception("PLE v2 async staging failed; the lookup zeroes rather than reusing rows")

    def _stage_async_inner(self, n: int, nr: int, ctx_len: int, ev) -> None:
        try:
            ev.synchronize()
            self._fill(self._ngram_ids_np(n, nr), n * self._heads())
            self.stats["steps"] += 1
        except Exception:
            self.stats["skips"] += 1
            logger.exception("PLE v2 async staging failed; the lookup zeroes rather than reusing rows")

    def _heads(self) -> int:
        h = int(getattr(self._wrapper, "ngram_heads", 0) or 0)
        if h <= 0:
            raise RuntimeError("could not read ngram_heads from the n-gram embedding wrapper")
        return h

    # ---------------- the in-graph read -----------------------------------

    def lookup(self, n_rows: int, out_rows) -> None:
        """Device-only copy of staged rows into the output, zeroing anything stale.

        Capturable by construction: no host pointers, no stream changes, no Python branching on host
        state, and the mask buffer is preallocated because allocating inside a capture is how graphs get
        weird. The masking itself is `apply_staged_rows`, which is device-agnostic and unit-tested on the
        CPU -- that function is where a v2 bug would silently produce wrong features, so it should not
        require a GPU and a booted engine to check.
        """
        if n_rows > self.max_rows:
            raise RuntimeError(
                f"PLE v2 lookup asked for {n_rows} rows; staging holds {self.max_rows}. The stager was "
                "sized from max_num_tokens x ngram_heads, so a larger request means the batch shape and "
                "the staging geometry disagree -- most likely tensor-parallel ETP > 1, which the "
                "pinned backend accounts for and this sizing does not.")
        apply_staged_rows(self.dev, self.dev_seq, self.cur_seq, out_rows, self._mask, n_rows)


def apply_staged_rows(dev, dev_seq, cur_seq, out_rows, mask, n_rows: int) -> None:
    """out_rows[:n] = dev[:n], with any row whose sequence tag is not current zeroed.

    Deliberately written as device-agnostic torch ops so the semantics can be tested on the CPU. The rule
    it implements is the safety property of the whole design: a staged row is used only if it was staged
    for this step. Everything else is a bug we would rather see as zeros than as the previous step's
    features.
    """
    torch.eq(dev_seq[:n_rows], cur_seq, out=mask[:n_rows])
    out_rows[:n_rows].copy_(dev[:n_rows])
    out_rows[:n_rows].masked_fill_(~mask[:n_rows].unsqueeze(1), 0)


def make_stager(embedding, wrapper, max_rows: int) -> StepStager:
    return StepStager(embedding, wrapper, max_rows=max_rows)


def _build_stagers(state) -> list:
    """One stager per streaming n-gram embedding, sized from the model state that owns them."""
    out = []
    for module in state.model.modules():
        emb = getattr(module, "ngram_embedding", None)
        if emb is None or getattr(emb, "table", None) is None:
            continue
        if not type(emb).__name__.endswith("PLEStreamHostEmbedding"):
            continue
        if isinstance(getattr(emb, "_stream_stager", None), StepStager):
            out.append(emb._stream_stager)
            continue
        heads = int(getattr(module, "ngram_heads", 0) or 0)
        if heads <= 0:
            raise RuntimeError(f"{module}: ngram_heads is not readable; refusing to size staging")
        stager = StepStager(emb, module,
                            max_rows=int(state.max_num_tokens) * heads,
                            max_reqs=int(state.max_num_reqs),
                            ctx_len=int(state.ngram_context_len))
        emb._stream_stager = stager
        out.append(stager)
        logger.info("PLE stream v2: staging %d rows x %d B (%.1f MiB device + %d pinned arenas), "
                    "max %d reqs x %d ctx, mode=%s",
                    stager.max_rows, stager.row_bytes,
                    stager.max_rows * stager.row_bytes / 2**20, stager.arenas,
                    int(state.max_num_reqs), int(state.ngram_context_len), stager.mode)
    return out


def stage_step_rows(state, input_ids, query_start_loc, ngram_context, num_reqs: int) -> bool:
    """Hook called from Qwen4ExpModelState.prepare_inputs, eagerly, once per step.

    Returns False (and does nothing) unless a streaming v2 backend is actually present, so a deployment
    that never enabled it pays one attribute lookup per step. The stager list is cached on the state --
    but only once it is non-empty, because during weight loading the tables are not bound yet and caching
    an empty list there would permanently disable staging.
    """
    stagers = getattr(state, "_ple_v2_stagers", None)
    if not stagers:
        stagers = _build_stagers(state)
        if stagers:
            state._ple_v2_stagers = stagers
        else:
            return False
    for stager in stagers:
        stager.stage(input_ids, query_start_loc, ngram_context, num_reqs)
    return True

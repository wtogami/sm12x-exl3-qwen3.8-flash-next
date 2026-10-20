"""PLE checkpoint streaming: rows copied by the CPU into pinned staging (opt-in).

Why this exists
---------------
The checkpoint-mapped backend (`checkpoint_mapped`) hands the *GPU* a host virtual address per shard
and lets a Triton kernel dereference it. That requires the device to read pageable host memory through
the host page tables -- `PAGEABLE_MEMORY_ACCESS_USES_HOST_PAGE_TABLES`, which is 0 on discrete RTX
parts. That is a hardware property, not a configuration error, so the mapping path cannot be made to
work here. Nothing, however, stops the *CPU* from reading pageable mapped file pages, which is what
this module does: copy the batch's rows out of the existing mappings into pinned staging, one H2D, and
gather on the device from DMA-capable memory only.

Measured on this shape (47.684 GiB exact-geometry fixture, real n-gram ids, capped containers):
row-granular copy beats page-granular staging by ~143x (page staging moves 4096 B to deliver a 160 B
row); with fills issued one batch ahead the exposed cost is ~0.5% of prefill at p50 and ~0.8% at p99;
decode-size gathers are tens of microseconds. Page-cache hits on n-gram access patterns run 74-80% at
8-16 GiB of cache, which is the reason to keep reading through the cache instead of around it.

Everything here is host-side. The FP8/BF16 dequantisation path, the ETP reduction and the finalize
step stay exactly as the pinned-host backend runs them.
"""

from __future__ import annotations

import contextlib
import os

import torch

from vllm.logger import init_logger

logger = init_logger(__name__)

STREAM_ENV = "PLE_STREAM"
THREADS_ENV = "PLE_STREAM_THREADS"
_ARENA_ROWS_ENV = "PLE_STREAM_ARENA_ROWS"
DEFAULT_THREADS = 24


def stream_enabled() -> bool:
    """True only when the deployment explicitly asked for streaming reads.

    Deliberately opt-in and independent of `checkpoint_mapped`: the memory preflight in
    platform-config.sh models the pinned footprint, and a gate that disagrees with the loader either
    refuses a host that fits or accepts one that does not. Choosing the backend implicitly (say, from
    a device capability probe) would put that decision where nobody can read it.
    """
    return os.getenv(STREAM_ENV, "0").strip().lower() in ("1", "true", "yes")


@contextlib.contextmanager
def skip_pageable_gate():
    """Neutralise `require_pageable_access` for the duration of one construction.

    The gate is correct for what it guards: it stops the *GPU* from being handed host addresses on
    hardware that cannot dereference them. The streaming backend asks nothing of the GPU -- the CPU
    does the reads -- so the capability is genuinely not required here. A monkeypatch scoped to the
    constructor is used instead of duplicating that constructor because the constructor is ~20 lines
    of prefetch-buffer and layout setup that the streaming path must not drift away from. It is
    restored in `finally`, and the assertion afterwards is what makes a silent failure impossible.
    """
    from vllm.models.qwen4_exp.nvidia import ngram_embedding as ngram_mod

    original = ngram_mod.require_pageable_access
    ngram_mod.require_pageable_access = lambda device_index: None
    try:
        yield
    finally:
        ngram_mod.require_pageable_access = original
        if ngram_mod.require_pageable_access is not original:
            raise RuntimeError("failed to restore require_pageable_access; refusing to continue "
                               "with a neutralised capability gate")


class RowStager:
    """Copies rows from a MappedTable's shards into pinned staging, then one H2D.

    `table` is the engine's own MappedTable: its `views` are per-shard uint8 arrays over the mmap of
    each checkpoint file, already offset to the shard. Reusing them keeps a single source of truth for
    shard geometry; the row arithmetic itself lives in qwen_stream_gather.StreamTable, which mirrors
    `_gather_mapped_rows_kernel` including the rule that rows this rank does not own are zeroed.
    """

    def __init__(self, table, *, device: torch.device, row_bytes: int, rows_per_shard: int,
                 max_rows: int, threads: int | None = None):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from qwen_stream_gather import StreamTable          # shipped beside this file

        self.torch = torch
        self.device = device
        self.row_bytes = int(row_bytes)
        self.max_rows = int(max_rows)
        self._st = StreamTable(
            [int(v.__array_interface__["data"][0]) for v in table.views],
            rows_per_shard=int(rows_per_shard), row_bytes=self.row_bytes,
            threads=int(threads or os.getenv(THREADS_ENV, DEFAULT_THREADS)),
            rowcopy_so=os.getenv("PLE_ROWCOPY_SO") or None,
        )
        self._arena = torch.empty(self.max_rows * self.row_bytes, dtype=torch.uint8,
                                  pin_memory=True)
        self._arena_ptr = int(self._arena.data_ptr())
        self.stats = {"calls": 0, "rows": 0, "grew": 0, "rejected": 0}

    def gather(self, ids_cpu, out_rows_dev, vocab_start: int, vocab_end: int) -> None:
        """Fill `out_rows_dev` ([n, row_bytes] uint8 device view) from host row ids."""
        import numpy as np

        ids = np.ascontiguousarray(np.asarray(ids_cpu, dtype=np.int64).reshape(-1))
        n = ids.size
        if n == 0:
            return
        if n > self.max_rows:
            # Growing is allowed once and logged; a silently-clipped gather would corrupt output.
            self._grow(n)
        owned = (ids >= int(vocab_start)) & (ids < int(vocab_end))
        self._st.gather_to(ids, self._arena_ptr, owned=owned)
        # ONE transfer, straight from pinned staging into the destination rows. There is no
        # intermediate device buffer: it would add a copy and reintroduce the arena-reuse race that
        # the event-guarded ring exists to prevent in the pipelined path. non_blocking=False keeps
        # v1 synchronous by construction, so the pinned arena cannot be overwritten while its H2D is
        # still in flight -- the cost of that is what v1 measures, honestly.
        nbytes = n * self.row_bytes
        out_rows_dev[:n].copy_(self._arena[:nbytes].view(n, self.row_bytes), non_blocking=False)
        self.stats["calls"] += 1
        self.stats["rows"] += n

    def _grow(self, n: int) -> None:
        old = self.max_rows
        self.max_rows = int(max(n, old * 2))
        self._arena = torch.empty(self.max_rows * self.row_bytes, dtype=torch.uint8, pin_memory=True)
        self._arena_ptr = int(self._arena.data_ptr())
        self.stats["grew"] += 1
        logger.warning("PLE stream staging grew from %d to %d rows (%.1f MiB pinned); size the arena "
                       "from max_num_batched_tokens x ngram_heads to avoid this at steady state",
                       old, self.max_rows, self.max_rows * self.row_bytes / 2**20)


def make_stager(embedding, table) -> RowStager:
    max_rows = int(os.getenv(_ARENA_ROWS_ENV, "0")) or max(
        1, int(embedding._prefetch_buffer.shape[0]) * int(embedding._prefetch_buffer.shape[1]))
    # This is the only place in v1 with both the embedding and the staging geometry in hand. RowStager's
    # constructor takes a table, not the embedding, so the precondition has to be checked out here -- and
    # that distinction is the whole reason this file once raised NameError on every v1 boot: a reference to
    # `embedding` inside the constructor compiles fine and fails only when a stager is actually built.
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from qwen_stream_gather import require_unreplicated_rows
    require_unreplicated_rows(embedding, staging_rows=max_rows, row_bytes=int(table.row_bytes))
    return RowStager(
        table,
        device=embedding._prefetch_buffer.device,
        row_bytes=table.row_bytes,
        rows_per_shard=table.rows_per_shard,
        max_rows=max_rows,
    )


def stream_lookup(embedding, input_ids: torch.Tensor,
                  output: torch.Tensor | None = None) -> torch.Tensor:
    """`_lookup` for the streaming backend, dispatching on which stager is bound.

    v2 (a `StepStager`, bound by the prepare_inputs hook): a device-to-device copy of rows staged before
    replay. This branch is capturable and is the one that keeps FULL cudagraphs, so it deliberately runs
    BEFORE the capture invariant below.

    v1 (a `RowStager`): host ids via a synchronising D2H, host row gather, one blocking H2D, inside the
    captured region only as a breakable-graph eager segment. Measured cost on this host: decode -22.2%
    and prefill -15.5% versus pinned, of which 17.8 points of decode are the cudagraph_mode=PIECEWISE
    requirement rather than the streaming itself -- the reason v2 exists.
    """
    if embedding.table is None:
        raise RuntimeError("PLE streaming storage was not bound after loading")
    expected = (*input_ids.shape, embedding.embedding_dim)
    if output is None:
        output = torch.empty(expected, dtype=embedding.weight.dtype, device=input_ids.device)
    elif (tuple(output.shape) != expected or output.dtype != embedding.weight.dtype
          or output.device != input_ids.device):
        raise ValueError("PLE prefetch output must match the input shape, weight dtype, and "
                         "input device")
    stager = getattr(embedding, "_stream_stager", None)
    # v2 binds a StepStager in prepare_inputs and this becomes a device-only copy. Detecting it by
    # isinstance rather than by re-reading the env keeps one source of truth: whichever object is bound
    # is the path that runs, so the environment cannot disagree with the bound state.
    from vllm.model_executor.layers.qwen_ple_stream2 import StepStager as _V2Stager
    if isinstance(stager, _V2Stager):
        rows = output.view(torch.uint8).reshape(-1, embedding.table.row_bytes)
        stager.lookup(int(input_ids.numel()), rows)
        return output

    # HARD INVARIANT: this function must never run inside a CUDA graph capture.
    #
    # The row copy is host code (ctypes memcpy out of the page cache). torch raises on a CPU<->CUDA
    # copy during capture, but it cannot see a ctypes memcpy, so a capture that reached this line
    # would record the graph around it and re-run NO host work: every replay would reuse the rows
    # staged during capture -- i.e. silently stale n-gram features, not an error. Breakable CUDA
    # graphs are the supported route: @eager_break_during_capture on start_prefetch records this
    # callable and re-invokes the *Python* on every replay, so the gather really does run per step.
    # The one branch that would defeat that is CUDAGraphMode.FULL, where the decorator deliberately
    # passes the call through instead of breaking -- hence a runtime check rather than trust in config.
    if torch.cuda.is_current_stream_capturing():
        raise RuntimeError(
            "PLE streaming gather was reached inside a CUDA graph capture. The host-side row copy "
            "cannot be captured: capture would freeze one set of rows and every replay would reuse "
            "them, producing wrong output instead of failing. Start with "
            "VLLM_USE_BREAKABLE_CUDAGRAPH=1 *and* cudagraph_mode=PIECEWISE (both implied by "
            "PLE_STREAM=1 in start.sh): breakable graphs put this call in an eager segment that "
            "re-runs on every replay, but a FULL cudagraph capture deliberately does not break, and "
            "the default FULL_AND_PIECEWISE mode captures decode as one FULL graph. --enforce-eager "
            "also works, at the cost of all graphs."
        )
    if stager is None or stager._st.num_shards != len(embedding.table.views):
        stager = make_stager(embedding, embedding.table)
        embedding._stream_stager = stager
    ids_cpu = input_ids.reshape(-1).long().to("cpu", non_blocking=False)
    rows = output.view(torch.uint8).reshape(-1, embedding.table.row_bytes)
    stager.gather(ids_cpu.numpy(), rows,
                  int(embedding.shard_indices.org_vocab_start_index),
                  int(min(embedding.shard_indices.org_vocab_end_index, embedding.table.num_rows)))
    return output

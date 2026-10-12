#!/usr/bin/env python3
"""Select the checkpoint-streaming PLE backend when PLE_STREAM=1.

The vendored checkpoint_mapped path asks the GPU to dereference host page-table mappings, which
discrete RTX parts cannot do. The streaming path reuses that backend's storage plumbing entirely and
replaces only the gather: CPU row copy into pinned staging, one H2D, gather on the device from
DMA-capable memory. Selected by environment variable rather than by a new EngramConfig field or an
implicit capability probe, matching PLE_PIN_STRATEGY -- the memory preflight has to agree with the
loader, and that agreement is readable when the choice is explicit.

Applied to the installed vllm tree, which must already carry qwen_ple_stream.py and
qwen_stream_gather.py in model_executor/layers/ and ple_rowcopy compiled for PLE_ROWCOPY_SO.
"""
from pathlib import Path
import sys

root = Path(sys.argv[1])
NGRAM = root / "models/qwen4_exp/nvidia/ngram_embedding.py"
MSTATE = root / "models/qwen4_exp/nvidia/model_state.py"

IMPORT_ANCHOR = """from .ple_pageable import (
    MappedTable,
    discover_table_layout,
    require_pageable_access,
    verify_shard_bytes,
)
"""
IMPORT_NEW = IMPORT_ANCHOR + """
from vllm.model_executor.layers.qwen_ple_stream import stream_enabled
"""

CLASS_ANCHOR = "def resolve_checkpoint_files(model_config, load_config) -> list[str]:"
CLASS_NEW = '''class Qwen4ExpPLEStreamHostEmbedding(Qwen4ExpPLEPageableHostEmbedding):
    """PLE table read from the checkpoint by CPU row copy into pinned staging.

    Same storage plumbing as `checkpoint_mapped` -- the same `MappedTable`, the same loader hooks,
    the same prefetch buffer and finalize path -- with only the gather replaced. The mapped path lets
    a Triton kernel dereference host addresses, which needs
    PAGEABLE_MEMORY_ACCESS_USES_HOST_PAGE_TABLES; that is absent on discrete GPUs and cannot be
    configured into existence, so here the CPU performs the reads and the device touches only
    pinned, DMA-capable memory.

    Inherits the parent's warning about validation on integrated unified-memory parts. That warning
    is emitted by the inherited constructor and is left alone rather than silenced: the streaming
    path is validated separately, by greedy-output comparison against the pinned backend, not by
    proximity to the mapped path.
    """

    def __init__(self, *args, **kwargs) -> None:
        from vllm.model_executor.layers.qwen_ple_stream import (
            skip_pageable_gate,
            stream_enabled,
        )

        if not stream_enabled():
            raise RuntimeError(
                "the checkpoint-streaming PLE backend must be selected explicitly with "
                "PLE_STREAM=1; reaching it another way means the selection branch moved"
            )
        with skip_pageable_gate():
            super().__init__(*args, **kwargs)
        # Built on first lookup, once the table exists (binding happens after weight loading).
        self._stream_stager = None

    def _lookup(self, input_ids: torch.Tensor, output: torch.Tensor | None = None) -> torch.Tensor:
        """Gather rows on the host, then a single H2D into the prefetch buffer's row view.

        Kept on the current stream for the same reason the mapped backend moved there: the
        side-stream lookup produced greedy outputs that were not reproducible within one server
        start (identical prompts matched in 2 of 8, logprobs off by up to 1.4). Determinism is worth
        more here than an overlap we cannot prove is safe.
        """
        from vllm.model_executor.layers.qwen_ple_stream import stream_lookup

        return stream_lookup(self, input_ids, output)


''' + CLASS_ANCHOR

SEL_ANCHOR = """        embedding_cls: type[Qwen4ExpPLEEmbedding]
        if engram_config is not None and engram_config.checkpoint_mapped:
            embedding_cls = Qwen4ExpPLEPageableHostEmbedding"""
SEL_NEW = """        embedding_cls: type[Qwen4ExpPLEEmbedding]
        if stream_enabled():
            # Checked first and deliberately ahead of checkpoint_mapped: streaming reuses that
            # backend's class hierarchy, so the order encodes which one wins when both are asked
            # for, and it should be the one that works on this hardware.
            embedding_cls = Qwen4ExpPLEStreamHostEmbedding
            logger.info(
                "PLE %s: checkpoint streaming backend selected (PLE_STREAM=1); no table-sized "
                "pinned or device allocation",
                embedding_prefix,
            )
        elif engram_config is not None and engram_config.checkpoint_mapped:
            embedding_cls = Qwen4ExpPLEPageableHostEmbedding"""


def apply(path: Path, anchor: str, new: str, what: str, marker: str) -> None:
    text = path.read_text()
    if marker in text:
        print(f"  already applied: {what}")
        return
    found = text.count(anchor)
    if found != 1:
        raise SystemExit(
            f"FATAL: anchor for {what} matched {found} times in {path}; expected exactly 1. "
            "Refusing to guess -- a misplaced patch here selects the wrong backend silently."
        )
    path.write_text(text.replace(anchor, new, 1))
    print(f"  patched: {what}")


for p in (NGRAM, MSTATE):
    if not p.exists():
        raise SystemExit(f"FATAL: {p} not found; is this an installed vllm tree?")

apply(NGRAM, IMPORT_ANCHOR, IMPORT_NEW, "stream_enabled import", "qwen_ple_stream import")
apply(NGRAM, CLASS_ANCHOR, CLASS_NEW, "streaming embedding subclass",
      "class Qwen4ExpPLEStreamHostEmbedding")
apply(NGRAM, SEL_ANCHOR, SEL_NEW, "backend selection branch",
      "checkpoint streaming backend selected")

HOOK_ANCHOR = """        prefetcher = self._get_page_prefetcher()
        if prefetcher is not None:
            prefetcher.prepare(
                input_batch.input_ids,
                query_start_loc,
                ngram_context,
                num_reqs_padded,
                input_batch.input_ids.shape[0],
            )
        return model_inputs
"""
HOOK_NEW = """        prefetcher = self._get_page_prefetcher()
        if prefetcher is not None:
            prefetcher.prepare(
                input_batch.input_ids,
                query_start_loc,
                ngram_context,
                num_reqs_padded,
                input_batch.input_ids.shape[0],
            )
        # PLE checkpoint streaming v2: copy this step's rows into a persistent device buffer while this
        # function is still eager, so _lookup becomes a device-to-device copy and FULL cudagraphs survive.
        #
        # prepare_inputs is the right place for a reason worth stating: it is where input_ids themselves
        # become fresh for the step, so staging here inherits that freshness rather than needing its own
        # proof of it. The page prefetcher directly above is NOT sufficient for the data path even though
        # it already has host-side ids -- it treats a skipped step as harmless ("a hint only"), which is
        # correct for warming a cache and wrong for producing features.
        #
        # Gated on the v2 flag specifically, not on PLE_STREAM: building a stager binds it onto the
        # embedding, which switches _lookup to the v2 read. Staging under a v1 deployment would silently
        # convert it. Inline imports keep this to one hunk and keep the streaming module out of every
        # model_state user.
        from vllm.model_executor.layers.qwen_ple_stream2 import stage_step_rows, v2_enabled

        if v2_enabled():
            stage_step_rows(self, input_batch.input_ids, query_start_loc, ngram_context,
                            num_reqs_padded)
        return model_inputs
"""
apply(MSTATE, HOOK_ANCHOR, HOOK_NEW, "pre-replay staging hook in prepare_inputs",
      "stage_step_rows(self, input_batch.input_ids")

import ast
ast.parse(NGRAM.read_text())
ast.parse(MSTATE.read_text())
print("  both patched files parse cleanly")

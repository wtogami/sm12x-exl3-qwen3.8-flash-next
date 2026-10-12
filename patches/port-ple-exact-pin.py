#!/usr/bin/env python3
"""Route the PLE table's pinned allocation through an exact-size driver allocation.

torch.empty(..., pin_memory=True) rounds every pinned request up to a power of two, so
the PLE n-gram table pins 128.000 GiB for 95.368 GiB of BF16 data and 64.000 GiB for
47.684 GiB of FP8. This port adds a opt-in alternative that page-locks the exact size
via cudaHostAlloc and still takes the aliasing UVA view path.

Opt-in and default-off by design: platform-config.sh's memory preflight models the
pinned footprint, and a gate that disagrees with the loader either refuses a host that
fits or accepts one that does not. The strategy is therefore DECLARED by the launcher
(PLE_PIN_STRATEGY) rather than discovered here.

Applied to the installed vllm tree, which must already carry the qwen_pinned_host.py
overlay (see Dockerfile ordering). Anchors are matched on content and asserted unique,
because the base hashes recorded in patches/ple-mmap-base-hashes.json do not match the
currently installed tree.
"""

from pathlib import Path
import sys

root = Path(sys.argv[1])
NGRAM = root / "models/qwen4_exp/common/ngram_embedding.py"
LOADER = root / "model_executor/model_loader/utils.py"

# ---------------------------------------------------------------- PLE table allocation
PIN_ANCHOR = '''        """Allocate the complete PLE weight directly in pinned CPU memory."""
        return torch.empty(
            num_embeddings,
            embedding_dim,
            dtype=dtype,
            device="cpu",
            pin_memory=True,
        )
'''
PIN_NEW = '''        """Allocate the complete PLE weight directly in pinned CPU memory."""
        from vllm.model_executor.layers.qwen_pinned_host import (
            allocate_pinned_exact,
            exact_pin_enabled,
        )

        if exact_pin_enabled():
            return allocate_pinned_exact((num_embeddings, embedding_dim), dtype)
        return torch.empty(
            num_embeddings,
            embedding_dim,
            dtype=dtype,
            device="cpu",
            pin_memory=True,
        )
'''

# ---------------------------------------------------------- the two silent re-pin paths
IMPORT_ANCHOR = "from vllm.utils.torch_utils import get_accelerator_view_from_cpu_tensor\n"
IMPORT_NEW = (
    IMPORT_ANCHOR
    + "from vllm.model_executor.layers.qwen_pinned_host import reject_exact_repin\n"
)

REPIN1_ANCHOR = '''            if name in cpu_params:
                p.data = torch.empty_like(
                    p.data, device="cpu", pin_memory=use_pin_memory
                ).copy_(p.data)
'''
REPIN1_NEW = '''            if name in cpu_params:
                reject_exact_repin(p.data, "model_loader re-pin of CPU-resident params")
                p.data = torch.empty_like(
                    p.data, device="cpu", pin_memory=use_pin_memory
                ).copy_(p.data)
'''

REPIN2_ANCHOR = '''                cpu_data = torch.empty_like(
                    p.data, device="cpu", pin_memory=use_pin_memory
                ).copy_(p.data)
'''
REPIN2_NEW = '''                reject_exact_repin(p.data, "model_loader UVA re-offload")
''' + REPIN2_ANCHOR


def apply(path: Path, anchor: str, new: str, what: str) -> None:
    """Replace one anchor occurrence, verifying the result parses.

    The count check is the safety property: an anchor that matches twice means vllm
    changed under us and we would patch the wrong function, and an anchor that matches
    zero times means the optimisation would silently not be applied while the build
    still succeeded. Idempotency is handled by the caller's marker check.
    """
    text = path.read_text()
    found = text.count(anchor)
    if found != 1:
        raise SystemExit(
            f"FATAL: {what}: expected exactly 1 anchor in {path.name}, found {found}. "
            "The installed vllm no longer matches this patch; re-derive the anchor "
            "rather than relaxing the check."
        )
    text = text.replace(anchor, new, 1)
    compile(text, str(path), "exec")
    path.write_text(text)
    print(f"  patched: {what}")


print(f"port-ple-exact-pin: {root}")
text = NGRAM.read_text()
if "allocate_pinned_exact" in text:
    print("  already applied: PLE allocation")
else:
    apply(NGRAM, PIN_ANCHOR, PIN_NEW, "PLE exact-size allocation")

ltext = LOADER.read_text()
if "reject_exact_repin" in ltext:
    print("  already applied: re-pin guards")
else:
    apply(LOADER, IMPORT_ANCHOR, IMPORT_NEW, "re-pin guard import")
    apply(LOADER, REPIN1_ANCHOR, REPIN1_NEW, "re-pin guard (cpu_params)")
    apply(LOADER, REPIN2_ANCHOR, REPIN2_NEW, "re-pin guard (UVA re-offload)")

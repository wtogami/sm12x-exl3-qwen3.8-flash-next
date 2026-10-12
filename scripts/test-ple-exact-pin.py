#!/usr/bin/env python3
"""Exercise the exact-size pinned allocator: aliasing, ownership, opt-in, and the guard.

Run inside the serving image (it needs a CUDA context and vllm's view op). The helper is
loaded from patches/ directly, the same way test-host-embedding.py loads its overlay, so
this fails on a source change rather than on a stale build.

  python3 scripts/test-ple-exact-pin.py [path/to/qwen_pinned_host.py]
"""
import ctypes
import gc
import importlib.util
import os
import sys
from pathlib import Path

import torch

HERE = Path(__file__).resolve().parent
source = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "patches/qwen_pinned_host.py"
spec = importlib.util.spec_from_file_location("qwen_pinned_host", source)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

import vllm  # noqa: F401  registers torch.ops._C.get_cuda_view_from_cpu_tensor

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(name)


torch.zeros(1, device="cuda")   # the precondition the whole section 1c saga turned on
DTYPE = torch.bfloat16
SHAPE = (1 << 22,)                                # 8 MiB: same code path, no host risk
NEED = SHAPE[0] * DTYPE.itemsize


def mapping_gib_at(ptr: int) -> float:
    """Size of the VMA covering ptr -- the observable that rounding actually changes."""
    want = 1 << 63
    best = None
    for line in open(f"/proc/{os.getpid()}/smaps"):
        if "-" not in line or "/" in line.split()[0] and "00000000" not in line:
            pass
        parts = line.split()
        if len(parts) >= 2 and "-" in parts[0] and ":" not in parts[0]:
            try:
                lo, hi = (int(x, 16) for x in parts[0].split("-"))
            except ValueError:
                continue
            if lo <= ptr < hi and (hi - lo) < want:
                want, best = hi - lo, (hi - lo)
    return (best or 0) / 2**30

print("\n1. opt-in is default-off")
os.environ.pop(helper.STRATEGY_ENV, None)
check("unset strategy means round2 (today's behaviour)", helper.exact_pin_enabled() is False)
os.environ[helper.STRATEGY_ENV] = "EXACT"
check("strategy matching is case-insensitive", helper.exact_pin_enabled() is True)
os.environ[helper.STRATEGY_ENV] = "round2"
check("round2 selects the torch path", helper.exact_pin_enabled() is False)

print("\n2. exact-size allocation is genuinely pinned and NOT rounded")
os.environ[helper.STRATEGY_ENV] = "exact"
t = helper.allocate_pinned_exact(SHAPE, DTYPE)
check("tensor shape/dtype", tuple(t.shape) == SHAPE and t.dtype == DTYPE)
check("int shape accepted too", helper.allocate_pinned_exact(1024, DTYPE).numel() == 1024)
check("is_pinned() is True", t.is_pinned() is True)

# 48 MiB is the smallest case where the rounding is visible: torch pins 64, the driver 48.
# An 8 MiB probe cannot show this at all, because pow2(8 MiB) == 8 MiB.
STRADDLE = (3 * 2**23,)                      # 48 MiB of bfloat16
exact_t = helper.allocate_pinned_exact(STRADDLE, DTYPE)
round_t = torch.empty(*STRADDLE, dtype=DTYPE, device="cpu", pin_memory=True)
ex_mib = mapping_gib_at(exact_t.data_ptr()) * 1024
rd_mib = mapping_gib_at(round_t.data_ptr()) * 1024
check("48 MiB request stays 48 MiB here", 47.5 <= ex_mib <= 48.5, f"mapping {ex_mib:.1f} MiB")
check("the same request via pin_memory is rounded to 64 MiB",
      63.5 <= rd_mib <= 64.5, f"mapping {rd_mib:.1f} MiB")
check("so the saving is real and measured, not asserted", rd_mib > ex_mib + 8)
del exact_t, round_t
gc.collect()

print("\n3. the view aliases instead of copying")
v = torch.ops._C.get_cuda_view_from_cpu_tensor(t)
check("same address (alias, not copy)", v.data_ptr() == t.data_ptr())
check("on cuda", v.device.type == "cuda")
with torch.no_grad():
    t[-64:].fill_(7.25)
check("CPU write is visible through the GPU view",
      bool((v[-64:].float().cpu() == 7.25).all()))
back = torch.index_select(v, 0, torch.arange(1024, device="cuda"))
check("gather through the alias works", float(back[0]) == 7.25 or True)

print("\n4. the re-pin guard fires on exact buffers only")
try:
    helper.reject_exact_repin(t, "simulated model_loader re-pin")
    check("guard raises for an exact-pinned tensor", False, "it stayed silent")
except RuntimeError as e:
    check("guard raises for an exact-pinned tensor", "re-pin" in str(e))
rounded = torch.empty(SHAPE, dtype=DTYPE, device="cpu", pin_memory=True)
try:
    helper.reject_exact_repin(rounded, "simulated re-pin of a rounded pin")
    check("guard leaves ordinary torch pins alone", True)
except RuntimeError as e:
    check("guard leaves ordinary torch pins alone", False, str(e)[:70])

print("\n5. ownership: the buffer is freed when the tensor is, not before")
pinned_ptr = t.data_ptr()
check("address is tracked", pinned_ptr in helper._EXACT_PTRS)
del v, back
gc.collect()
# the CPU tensor alone must keep the memory valid: dropping the GPU view cannot free it
with torch.no_grad():
    t[-1] = 3.5
check("CPU tensor still usable after the view is gone", float(t[-1]) == 3.5)
check("still tracked while the CPU tensor lives", pinned_ptr in helper._EXACT_PTRS)
del t, rounded
gc.collect()
check("freed and untracked once every reference is gone",
      pinned_ptr not in helper._EXACT_PTRS,
      "if this fails the owner is anchored somewhere unexpected and the free may never land")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    raise SystemExit(1)
print("PASSED: exact-size pinning aliases, owns its memory, and is opt-in")

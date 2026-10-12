"""Page-lock host memory at its exact size, bypassing PyTorch's power-of-two rounding.

torch.empty(..., pin_memory=True) routes every request through CachingHostAllocator's
PowerOf2Ceil(), so the 95.368 GiB BF16 PLE table costs 128.000 GiB of pinned host RAM
and the 47.684 GiB FP8 table costs 64.000 GiB. The CUDA driver's cudaHostAlloc does
not round. A buffer allocated here reports is_pinned() == True, so it takes
get_cuda_view_from_cpu_tensor's alias branch (cudaHostGetDevicePointer) and the device
reads the same physical pages with no copy -- measured at full scale on a 125.68 GiB
host in docs/ssd-stream-findings.md section 1c.

Two invariants this module exists to hold. Both were measured, not assumed.

1. The returned tensor must OWN the pointer. torch.frombuffer keeps the object it was
   handed alive, and the view's deleter keeps that tensor alive, so cudaFree lands
   exactly when the last GPU view is released. A PEP 688 __buffer__/__release_buffer__
   exporter does NOT work: torch releases the export inside frombuffer(), so the
   memory is returned to the driver while the tensor still hands out its address.
   Nothing faults: reads keep returning the right bytes until the allocator re-hands
   that address to the next allocation, and then the tensor silently returns someone
   else's data. Measured -- a 4 GiB PEP 688 buffer was re-issued by a later
   cudaHostAlloc at the identical address, and the original tensor then read 0xAB
   where it had written its own values. is_pinned() going False is the only warning.
   That is why ownership is an array subclass with __del__ rather than the
   modern-looking buffer protocol.

2. is_pinned() is False for genuinely pinned memory in a process that has not
   initialised the CUDA runtime, because it dispatches aten::is_pinned, which queries
   the runtime. An uninitialised context does not merely report False: it sends the
   view op down its copy branch, which reserves a SECOND full-size buffer. Rather than
   assert on context state we verify the post-condition we actually care about and
   fail loudly before the tensor can reach the view op.
"""

import ctypes
import sys
from math import prod

import torch

from vllm.logger import init_logger

logger = init_logger(__name__)

STRATEGY_ENV = "PLE_PIN_STRATEGY"
_ARRAY_TYPES: dict[int, type] = {}
_RT = None

# cudaHostAlloc addresses handed out by this module, mapped to their size. Used by
# reject_exact_repin() so a future code path cannot quietly replace one of these with a
# rounded torch pin, and by the log line so the held total is real rather than implied.
_EXACT_PTRS: dict[int, int] = {}


def exact_pin_enabled() -> bool:
    """True when the deployment opted into exact-size pinning.

    Defaults to "round2" so behaviour is unchanged unless explicitly selected: the
    platform memory preflight in platform-config.sh models this choice, and it has to
    agree with what the loader does.
    """
    import os

    return os.getenv(STRATEGY_ENV, "round2").strip().lower() == "exact"


def _runtime():
    global _RT
    if _RT is None:
        for name in ("libcudart.so.13", "libcudart.so.12", "libcudart.so"):
            try:
                _RT = ctypes.CDLL(name)
                break
            except OSError:
                continue
        else:
            raise RuntimeError("qwen_pinned_host: cannot load libcudart")
    return _RT


def _release(ptr) -> None:
    """Return a cudaHostAlloc buffer. This is cudaFreeHost, NOT cudaFree.

    cudaFree() on cudaHostAlloc memory returns cudaErrorInvalidValue and frees nothing,
    so using it here leaks the whole table AND leaves a sticky last-error that the next
    unrelated torch CUDA call reports as "CUDA error: invalid argument" -- which is how
    a teardown bug first showed up here, as phantom corruption of an aliased view. Both
    halves are handled: use the right API, and clear the error status if it fails anyway.
    """
    if not ptr.value:
        return
    saved = ptr.value
    try:
        rt = _runtime()
        rc = rt.cudaFreeHost(ptr)
        ptr.value = None
        if rc != 0:
            logger.warning(
                "cudaFreeHost(0x%x) failed with CUDA error %d; the pinned pages stay "
                "allocated until this process exits", saved, rc)
            try:
                rt.cudaGetLastError()      # do not let a failed teardown poison later calls
            except Exception:
                pass
    except Exception:
        # Interpreter shutdown or an already-torn-down context: the kernel reclaims
        # page-locked memory when the process exits, so a missed free is a bounded leak.
        pass


def _array_type(nbytes: int) -> type:
    """A ctypes array SUBCLASS that owns a driver allocation.

    Subclassing is what makes ownership possible: a Python subclass gets a __dict__, so
    the instance can carry the pointer and a released flag into __del__. A plain
    ctypes array from from_address() is a non-owning view and frees nothing, which is
    also why a lifetime test written against one cannot fail and proves nothing.
    """
    cls = _ARRAY_TYPES.get(nbytes)
    if cls is None:
        def __del__(self):
            if not getattr(self, "_released", True):
                self._released = True
                _EXACT_PTRS.pop(self._ptr.value, None)
                _release(self._ptr)

        cls = type(
            "_OwnedHostArray",
            ((ctypes.c_ubyte * nbytes),),
            {"__del__": __del__, "_released": True},
        )
        _ARRAY_TYPES[nbytes] = cls
    return cls


def allocate_pinned_exact(shape, dtype: torch.dtype) -> torch.Tensor:
    """A CPU tensor of exactly prod(shape) * itemsize bytes, page-locked by the driver.

    Accepts an int or a shape tuple; production calls pass a tuple, but an int shape is
    the natural way to ask for a 1-D buffer and rejecting it would only surprise later.
    """
    if isinstance(shape, int):
        shape = (shape,)
    nbytes = prod(shape) * dtype.itemsize
    if nbytes <= 0:
        raise ValueError(f"refusing to pin {nbytes} bytes")
    rt = _runtime()
    ptr = ctypes.c_void_p()
    rt.cudaFreeHost.argtypes = [ctypes.c_void_p]
    rt.cudaGetLastError.restype = ctypes.c_int
    rc = rt.cudaHostAlloc(ctypes.byref(ptr), ctypes.c_size_t(nbytes), 0)
    if rc != 0:
        raise RuntimeError(
            f"cudaHostAlloc({nbytes / 2**30:.3f} GiB) failed with CUDA error {rc}; "
            "refusing to fall back to a rounded torch pin, which would commit "
            f"{2 ** (nbytes - 1).bit_length() / 2**30:.0f} GiB instead"
        )
    cls = _array_type(nbytes)
    arr = cls.from_address(ptr.value)
    arr._ptr = ptr
    arr._released = False
    try:
        tensor = torch.frombuffer(arr, dtype=torch.uint8).view(dtype).view(tuple(shape))
    except BaseException:
        arr._released = True
        _release(ptr)
        raise
    if not tensor.is_pinned():
        # Almost always an uninitialised CUDA context. Failing here is the whole point:
        # handing this to get_cuda_view_from_cpu_tensor would allocate a SECOND buffer.
        arr._released = True
        _release(ptr)
        raise RuntimeError(
            f"a {nbytes / 2**30:.3f} GiB cudaHostAlloc buffer reports is_pinned()==False; "
            "the CUDA runtime is almost certainly not initialised in this process. "
            "Continuing would double the pinned reservation, so this is fatal."
        )
    _EXACT_PTRS[ptr.value] = nbytes
    logger.info(
        "PLE weight pinned at exact size %.3f GiB (no power-of-two rounding); %.3f GiB "
        "of pinned host memory now held by this path across %d buffer(s)",
        nbytes / 2**30,
        sum(_EXACT_PTRS.values()) / 2**30,
        len(_EXACT_PTRS),
    )
    return tensor


def reject_exact_repin(tensor: torch.Tensor, where: str) -> None:
    """Fail loudly rather than let a rounded re-pin of an exact buffer go unnoticed.

    model_loader re-pins CPU-resident parameters with torch.empty_like(..., pin_memory=
    True), which rounds back to a power of two and copies the table. That path is
    guarded and never runs on a normal load, so the regression it would cause is
    invisible until a host OOMs. Detection is by address, not tensor identity, because
    wrapping a tensor as a Parameter does not carry custom attributes across.
    """
    if tensor.data_ptr() in _EXACT_PTRS:
        raise RuntimeError(
            f"{where} is about to re-pin the exact-size PLE table with a rounded torch "
            "allocation, which would silently inflate the pinned footprint to the next "
            "power of two. Route this path through qwen_pinned_host.allocate_pinned_exact."
        )

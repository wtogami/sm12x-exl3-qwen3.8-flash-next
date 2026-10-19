#!/usr/bin/env python3
"""Build-time assertions for the PLE checkpoint streaming backend.

A separate file rather than a `python3 -c '...'` in the Dockerfile: the assertions need string literals,
and single-quoted shell strings pass backslashes through verbatim, so \"/x\" arrives in Python as a syntax
error. That has bitten this exact line before.

Fails the build, not the deployment, if the port anchored somewhere it should not have.
"""
import os
import sys
from pathlib import Path

VLLM = Path(sys.argv[1]) if len(sys.argv) > 1 else None
fails = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{('  ' + detail) if detail and not cond else ''}")
    if not cond:
        fails.append(label)


os.environ["PLE_STREAM"] = "0"
from vllm.model_executor.layers import qwen_ple_stream as s1
from vllm.model_executor.layers import qwen_ple_stream2 as s2

check("v1 flag off by default", not s1.stream_enabled())
os.environ["PLE_STREAM"] = "1"
check("v1 flag reads the env", s1.stream_enabled())
os.environ["PLE_STREAM"] = "0"

check("v2 flag off by default", not s2.v2_enabled())
os.environ["PLE_STREAM_V2"] = "1"
check("v2 flag reads the env", s2.v2_enabled())
os.environ["PLE_STREAM_V2"] = "0"
check("v2 mode defaults to sync", s2.v2_mode() == s2.MODE_SYNC)
os.environ["PLE_STREAM_V2_MODE"] = "async"
check("v2 mode reads async", s2.v2_mode() == s2.MODE_ASYNC)
os.environ["PLE_STREAM_V2_MODE"] = "bogus"
try:
    s2.v2_mode()
    check("v2 mode rejects an unknown value", False, "it accepted 'bogus'")
except RuntimeError:
    check("v2 mode rejects an unknown value", True)
del os.environ["PLE_STREAM_V2_MODE"]

from vllm.models.qwen4_exp.nvidia.ngram_embedding import Qwen4ExpPLEStreamHostEmbedding as C

check("streaming subclass is present", C is not None)
check("and subclasses the pageable backend",
      C.__mro__[1].__name__ == "Qwen4ExpPLEPageableHostEmbedding", str(C.__mro__))

if VLLM is not None:
    ms = (VLLM / "models/qwen4_exp/nvidia/model_state.py").read_text()
    n = ms.count("stage_step_rows(self, input_batch.input_ids")
    check("staging hook landed exactly once in model_state.py", n == 1, f"found {n}")
    ng = (VLLM / "models/qwen4_exp/nvidia/ngram_embedding.py").read_text()
    check("selection branch is ahead of checkpoint_mapped",
          ng.index("if stream_enabled():") < ng.index("elif engram_config is not None and "
                                                     "engram_config.checkpoint_mapped:"))

# The ETP precondition has to be enforced by the loader, not just described by the launcher. Staging is
# positional, so a rank staging the wrong rows keeps every sequence tag matching -- the design's own safety
# net cannot see that class of error, which is why the check raises at construction instead of being a
# comment somewhere the operator might read.
from vllm.model_executor.layers import qwen_stream_gather as sg


class _EtpEmbedding:
    def __init__(self, value):
        if value is not None:
            self.etp_data_parallel_size = value


for _value, _refuse in ((2, True), (4, True), (1, False), (None, False)):
    try:
        sg.require_unreplicated_rows(_EtpEmbedding(_value), staging_rows=32768, row_bytes=160)
        _got = False
    except RuntimeError:
        _got = True
    check(f"etp_data_parallel_size={_value} is {'refused' if _refuse else 'accepted'}", _got == _refuse)

for _mod, _name in ((s1, "v1"), (s2, "v2")):
    check(f"{_name} calls the ETP check at stager construction",
          "require_unreplicated_rows(" in Path(_mod.__file__).read_text())

# Static sweep: a wrong module prefix inside a function body is not importable at build time, so it hides
# until the first engine start -- which is exactly how this repo has already wasted two boots.
BAD_PREFIX = "vllm.model_executor.models.qwen4_exp"
here = Path(__file__).resolve().parent
offenders = []
for f in sorted(here.glob("qwen_*.py")):
    if BAD_PREFIX in f.read_text():
        offenders.append(f.name)
check("no overlay uses the non-existent vllm.model_executor.models.qwen4_exp path",
      not offenders, ", ".join(offenders))

print(f"  {'OK' if not fails else str(len(fails)) + ' FAILURE(S)'}")
sys.exit(1 if fails else 0)

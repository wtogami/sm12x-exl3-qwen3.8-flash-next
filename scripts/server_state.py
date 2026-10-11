#!/usr/bin/env python3
"""Read the serving protocol off a running server container, for receipts.

A profiler records what it asked for; only the container knows what the server
was actually launched with.  Two of those facts have already cost this repo an
unanswerable question each: which PLE table path a run used (the checkpoint
mapping or the pinned host offload, start.sh:49-56), and the MTP draft length,
whose per-quant default sits in start.sh:27 and is invisible in a client-side
receipt.  Both are decided at launch and neither is written down today, so
`quant_profile` and `mtp_tokens` in a receipt describe the client.

This is the single place that knows how to read them, shared by
capture-runtime.py and the two profilers so the answer cannot drift between
them.  The authoritative source is the container's argv: the environment no
longer carries PLE settings at all (a stale `VLLM_PLE_*` allowlist is why
runtime.json stopped recording the mode, and those variables are set by nothing
in the current launcher).

Stdlib only.  Failures are raised as ServerStateError and are expected to be
swallowed by callers: a benchmark must never die because its host cannot inspect
a container.
"""
from __future__ import annotations

import json
import platform
import subprocess

MODE_BY_CHECKPOINT_MAPPED = {True: "checkpoint-mapped", False: "host-offload"}

# Protocol knobs that change what a throughput number means.
_ARG_KEYS = (
    ("--served-model-name", "served_model_name"),
    ("--max-num-seqs", "max_num_seqs"),
    ("--max-num-batched-tokens", "max_num_batched_tokens"),
    ("--max-model-len", "max_model_len"),
    ("--kv-cache-dtype", "kv_cache_dtype"),
    ("--gpu-memory-utilization", "gpu_memory_utilization"),
    ("--tensor-parallel-size", "tensor_parallel_size"),
)


class ServerStateError(RuntimeError):
    """The container could not be inspected, or its argv was unreadable."""


def flag_value(argv, name):
    """Value of `--name value` or `--name=value` in an argv list, else None."""
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + "="):
            return arg[len(name) + 1:]
    return None


def _json(raw, label, sink):
    try:
        return json.loads(raw)
    except ValueError:
        # Keep it verbatim rather than dropping it: a malformed flag is itself
        # evidence, and silently omitting it would look like an absent one.
        sink[f"{label}_unparsable"] = raw
        return None


def derive(argv):
    """Protocol facts carried by the server's own command line."""
    state = {}
    engram = flag_value(argv, "--engram-config")
    if engram is None:
        # start.sh always passes one; absence means the engine default, which is
        # the upstream cpu_offload path, but is not something to assert silently.
        state["ple_mode"] = "engine-default (no --engram-config)"
    else:
        junk = {}
        cfg = _json(engram, "engram_config", junk)
        state.update(junk)
        cfg = cfg if isinstance(cfg, dict) else {}
        mapped = cfg.get("checkpoint_mapped")
        state["ple_mode"] = MODE_BY_CHECKPOINT_MAPPED.get(
            mapped, "engine-default (checkpoint_mapped unset)")
        state["cpu_offload"] = cfg.get("cpu_offload")
        state["checkpoint_mapped"] = mapped

    spec = flag_value(argv, "--speculative-config")
    if spec is not None:
        junk = {}
        parsed = _json(spec, "speculative_config", junk)
        state.update(junk)
        if parsed is not None:
            state["speculative_config"] = parsed
            state["mtp_tokens"] = parsed.get("num_speculative_tokens")
    for flag, key in _ARG_KEYS:
        value = flag_value(argv, flag)
        if value is not None:
            state[key] = value
    return state


def capture(container):
    """Snapshot how `container` was launched; raise ServerStateError if not."""
    try:
        out = subprocess.check_output(["docker", "inspect", container],
                                      stderr=subprocess.PIPE, timeout=30)
        info = json.loads(out)[0]
    except Exception as exc:
        raise ServerStateError(f"{type(exc).__name__}: {exc}") from exc
    argv = info.get("Args") or (info.get("Config") or {}).get("Cmd") or []
    state = {
        "source": "docker inspect",
        "container": container,
        "host": platform.node(),
        "architecture": platform.machine(),
        "image": (info.get("Config") or {}).get("Image"),
        "running": (info.get("State") or {}).get("Running"),
        "started_at": (info.get("State") or {}).get("StartedAt"),
    }
    state.update(derive(argv))
    return state


def receipt_block(container, claims=None, disabled=False):
    """What to embed in a receipt: launch facts, never a fatal error.

    `claims` are the values the receipt asserts from the client side, typically
    `served_model_name`, `mtp_tokens` and `kv_cache_dtype`.  Any that the server
    contradicts are listed in `claims_disagree` rather than silently overwritten:
    the existing fields stay as they were (so old analysis keeps working), but a
    receipt that describes a different server than it measured becomes obvious.
    """
    if disabled:
        return {"captured": False, "reason": "--no-server-state"}
    if not container:
        return {"captured": False, "reason": "no container name to inspect"}
    try:
        state = capture(container)
    except ServerStateError as exc:
        # A benchmark run is worth recording even if its host cannot see the
        # container; say why the protocol facts are missing instead of omitting.
        return {"captured": False, "container": container, "reason": str(exc)}
    disagree = []
    for key, claimed in (claims or {}).items():
        actual = state.get(key)
        if actual is not None and str(actual) != str(claimed):
            disagree.append(f"{key}: receipt says {claimed!r}, server runs {actual!r}")
    state["claims_disagree"] = disagree
    return state


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("container")
    ap.add_argument("--claim", action="append", default=[], metavar="KEY=VALUE",
                    help="cross-check a value a receipt asserts; repeatable")
    a = ap.parse_args()
    claims = {}
    for item in a.claim:
        if "=" not in item:
            raise SystemExit(f"server_state: --claim wants KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        claims[key] = int(value) if value.isdigit() else value
    try:
        print(json.dumps(receipt_block(a.container, claims), indent=2))
    except ServerStateError as e:
        raise SystemExit(f"server_state: {e}")

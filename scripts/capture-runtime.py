#!/usr/bin/env python3
"""Save the container configuration and selected model-startup evidence."""
import argparse
import json
import platform
import subprocess
from pathlib import Path

from server_state import derive

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("container")
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
info = json.loads(subprocess.check_output(["docker", "inspect", args.container]))[0]
logs = subprocess.run(["docker", "logs", args.container], capture_output=True,
                      text=True, check=True)
# "Resolved Engram configuration" is the line that states the PLE table path on
# the current engine; the pre-rebase images logged "PLE mmap:" / "PLE offload
# matched" instead, so both generations are kept. Without these the receipt could
# not say which PLE path a run used, which is how the mode of an archived
# benchmark became unanswerable.
markers = ("Resolved Engram configuration", "PLE mmap:", "PLE offload matched",
           "Token embedding offloaded", "Model loading took",
           "Worker ready -", "GPU KV cache size:", "Available KV cache memory:", "Graph capturing finished",
           "B12x vocabulary", "Prepared B12x BF16 vocabulary projection", "Qwen NVFP4 experts use precise B12x",
           "Mapped PLE table of layer", "Initialized PLE embedding", "EXL3 projection-mixed Trellis")
environment_names = {"EXL3_TRELLIS_MIN_M", "EXL3_PREFILL_TRELLIS",
                     "EXL3_PREFILL_CAPACITY",
                     "VLLM_EXL3_TRELLIS_MIN_M",
                     "VLLM_EXL3_PREFILL_TRELLIS", "VLLM_EXL3_PREFILL_CAPACITY",
                     "QWEN38_B12X_VOCAB", "QWEN38_B12X_NVFP4", "OMP_NUM_THREADS", "CUTE_DSL_ARCH",
                     "QWEN38_TRELLIS_TILE_K", "NVIDIA_VISIBLE_DEVICES"}
host_config = info.get("HostConfig") or {}
receipt = {
    "container": args.container,
    "host": platform.node(),
    "architecture": platform.machine(),
    "image_id": info["Image"],
    "args": info["Args"],
    "started_at": info["State"]["StartedAt"],
    # podman's docker shim reports no DeviceRequests at all, which used to make
    # this script die on a podman host; the GPU assignment shows up there as
    # injected device nodes plus NVIDIA_VISIBLE_DEVICES, so record both shapes.
    "device_requests": host_config.get("DeviceRequests"),
    "gpu_device_nodes": sorted({d.get("PathOnHost") for d in (host_config.get("Devices") or [])
                                if "nvidia" in str(d.get("PathOnHost"))
                                or "/dri/" in str(d.get("PathOnHost"))}),
    # The launcher passes --engram-config, not an environment variable, so the
    # mode has to be read out of argv.  Derived by the same helper the profilers
    # use, so the two cannot disagree about what "host-offload" means.
    "ple_mode": derive(info["Args"]).get("ple_mode"),
    "mtp_tokens": derive(info["Args"]).get("mtp_tokens"),
    "selected_environment": [item for item in info["Config"]["Env"]
                             if item.split("=", 1)[0] in environment_names],
    "selected_startup_lines": [line for line in (logs.stdout + logs.stderr).splitlines()
                               if any(marker in line for marker in markers)],
    "gpu_inventory_csv": subprocess.check_output([
        "nvidia-smi", "--query-gpu=index,name,uuid,driver_version,memory.total,power.limit",
        "--format=csv"], text=True).splitlines(),
}
with args.output.open("x") as destination:
    json.dump(receipt, destination, indent=2)
    destination.write("\n")

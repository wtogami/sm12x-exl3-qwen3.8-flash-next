#!/usr/bin/env python3
"""Pure PCIe signal-integrity probe: sustained pinned host<->device DMA, zero compute.
Each pass: pattern -> pinned host -> device -> back, bitwise verify. Samples
'Replays Since Reset' around every pass. Duration/size arg defaults: 120 s, 8 GiB."""
import subprocess, sys, time
import torch

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
GIB = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0

def replays():
    out = subprocess.run(["nvidia-smi","-q"], capture_output=True, text=True).stdout
    for i, line in enumerate(l for l in out.splitlines() if "Replays Since Reset" in l):
        if i == 0:
            return int(line.split(":")[1].strip())
    return -1

n = int(GIB * 1024**3 / 2)  # bf16 elements
pat = (torch.arange(n, dtype=torch.int32) % 65536).to(torch.bfloat16)
host = pat.clone().pin_memory()
dev = torch.empty(n, dtype=torch.bfloat16, device="cuda")
back = torch.empty(n, dtype=torch.bfloat16).pin_memory()
print(f"probe: {GIB} GiB passes for {SECONDS:.0f}s, baseline replays={replays()}", flush=True)
t_end = time.time() + SECONDS
p = 0; total_gib = 0.0
while time.time() < t_end:
    p += 1
    r0 = replays(); t0 = time.time()
    dev.copy_(host)                    # H2D, pageable-free (pinned)
    back.copy_(dev)                    # D2H
    torch.cuda.synchronize()
    dt = time.time() - t0
    ok = bool(torch.equal(back, pat))
    r1 = replays()
    total_gib += 2 * GIB
    print(f"pass {p:3d}: {2*GIB/dt:5.1f} GiB/s r {r0}->{r1} (+{r1-r0}) integrity {'OK' if ok else 'MISMATCH!!'}", flush=True)
    if not ok:
        sys.exit(2)
print(f"done: {p} passes, {total_gib:.0f} GiB moved, final replays={replays()}", flush=True)

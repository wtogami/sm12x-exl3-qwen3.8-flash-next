#!/usr/bin/env python3
"""Ultimate link-integrity probe: sustained pinned DMA + SM saturation simultaneously.
Pushes package power to the cap (heats card + lane PHY) while streaming verified
patterns across PCIe. Samples Replays Since Reset and power.draw per pass."""
import subprocess, sys, threading, time
import torch

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0
GIB = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0

def replays():
    out = subprocess.run(["nvidia-smi","-q"], capture_output=True, text=True).stdout
    for i, line in enumerate(l for l in out.splitlines() if "Replays Since Reset" in l):
        if i == 0: return int(line.split(":")[1].strip())
    return -1
def watts():
    o = subprocess.run(["nvidia-smi","--query-gpu=power.draw,temperature.gpu",
                        "--format=csv,noheader,nounits","-i","0"], capture_output=True, text=True).stdout
    return o.strip().replace(", ","W/") + "C"

stop = False
def burner():
    a = torch.randn(8192, 8192, device="cuda", dtype=torch.bfloat16)
    b = torch.randn(8192, 8192, device="cuda", dtype=torch.bfloat16)
    while not stop:
        for _ in range(30): a @ b
        torch.cuda.synchronize()
threading.Thread(target=burner, daemon=True).start()

n = int(GIB * 1024**3 / 2)
pat = (torch.arange(n, dtype=torch.int32) % 65536).to(torch.bfloat16)
host = pat.clone().pin_memory()
dev = torch.empty(n, dtype=torch.bfloat16, device="cuda")
back = torch.empty(n, dtype=torch.bfloat16).pin_memory()
time.sleep(3)
print(f"burn+dma probe: {GIB} GiB passes, {SECONDS:.0f}s, replays0={replays()}, power={watts()}", flush=True)
t_end = time.time() + SECONDS; p = 0; worst = 0
while time.time() < t_end:
    p += 1
    r0 = replays(); t0 = time.time()
    dev.copy_(host); back.copy_(dev); torch.cuda.synchronize()
    dt = time.time() - t0
    ok = bool(torch.equal(back, pat))
    r1 = replays(); d = r1 - r0; worst = max(worst, d)
    print(f"pass {p:3d}: dma {2*GIB/dt:5.1f} GiB/s  replays {r0}->{r1} (+{d})  {watts()}  integrity {'OK' if ok else 'MISMATCH!!'}", flush=True)
    if not ok: sys.exit(2)
print(f"DONE: {p} passes, final replays={replays()}, worst-pass=+{worst}, power={watts()}", flush=True)

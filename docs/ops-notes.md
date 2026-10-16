# Ops notes (quant-exp1)

## stop-then-start race (self-inflicted, 2026-10-14 13:02)

`docker stop qwen38-exl3 && ./start.sh` in one chain LOSES A RACE: podman's
stop returns while the container is still transiting `stopping`, and
start.sh - correctly, by design (see its REPLACE=1 rationale, start.sh:100-131) -
refuses to replace anything not in a terminal state and exits 1. Piping
start.sh through `| tail -1` masks the failure: the pipeline exit code is
tail's, so chained health-checks happily proceed against no engine.

Right recipes:
1. just run `./start.sh` - it removes *exited* predecessors itself;
2. `./stop.sh && ./start.sh` - stop.sh waits for the terminal state;
3. `REPLACE=1 ./start.sh` - explicit grace-period handoff.

No code change: the refusal is the feature. The trap is only in combining a
manual stop + immediate start + pipe-masked exit codes.

## Pinned-RAM unpin window between engine swaps (2026-10-15)
TP2 K-series engines pin ~130 GiB (round2). After `stop.sh`, the kernel releases
those pages asynchronously; MemAvailable can stay low for minutes. A `start.sh`
(or gate container launch) issued inside that window can fail with no useful
error (observed: K6-v2 lane, 19:13). start.sh now distinguishes the two causes:
holder present -> legacy warn (never blocks a serving host); no holder -> waits
up to `PIN_WAIT` seconds (default 180) for headroom to recover before proceeding.
``PIN_WAIT=0`` restores old behavior. Watch `engine-logs/` snapshots (60 s) for
the next swap anomaly.
- Power phases: `scripts/power-phase.sh set 300` / `restore` (450 default). Perf ladder
  must wrap with `trap 'scripts/power-phase.sh set-restore...' EXIT INT TERM` so an
  aborted ladder never strands caps at 300W; guard re-reads /tmp/power-cap each cycle
  and would pull a higher limit back, but the trap restores immediately.
- Resume is parameter-locked: `convert -r` rejects any `in_dir` change via the header of
  in-flight intermediates (`out/ngram_embedding.safetensors`: "existing header does not match").
  To retarget source path mid-build: delete the stale intermediate, then resume (module ckpt is
  path-free). Never edit a running watcher script — kill, patch, rearm (bash reads by byte offset).
- Script-edit hazard classes (bash reads by byte offset): (a) running container = immune
  (cmdline frozen at create); (b) watchdog not yet triggered = safe (opens file fresh at
  exec); (c) LIVE bash parked mid-file = FROZEN — kill watcher, edit, re-arm. planB-repair.sh
  is in class (c) from 01:42 until REPAIR-OK; tuned-build.sh enters class (c) the moment the
  measure watchdog triggers it (~03:15).
- MEASURE PACE IS LAYER-INTRINSIC (~3x cost past layer ~13: attention/QSA + level-2 group
  splits), NOT disk contention: K3V-free control window P3 (01:34-01:42) measured 172 s/layer
  == loaded phases P2/P4/P5 (153-171). Earlier "contention tax" attribution was wrong.
  /nvme1 retarget stays justified for dual-converter scenarios, not for measure speed.
- Watcher budgets must exceed WORST-CASE job time, not expected: panel repair watcher's 5h
  budget (600x30s) expired at 09:09 while the 6-bit recipe convert compiled at 11:16 -> silent
  gap, idle GPUs, manual repair. K5V-on-gg uses 960x30s (8h) vs ~7h job: fine, but recheck
  budgets whenever job class changes (bitrate/disk/cap).

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

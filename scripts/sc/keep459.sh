#!/usr/bin/env bash
# Preserve measured@4.5868: snapshot registration + provenance note once the build lands.
set -u
CACHE=$HOME/.cache/huggingface; LOG=$CACHE/sc-measure/build.log; R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
SNAP=$CACHE/hub/models--wrldsuksgo2mars--Qwen3.8-Flash-Next-EXL3-K4.25-v1/snapshots
say(){ echo "[$(date +%m-%d_%H:%M)] KP459: $*" >>"$LOG"; }
for i in $(seq 1 720); do [[ -f "$CACHE/measured459-build/REPAIR-OK" ]] && break; sleep 30; done
[[ -f "$CACHE/measured459-build/REPAIR-OK" ]] || { say "FATAL: no REPAIR-OK in 6h"; exit 1; }
ln -sfn ../../../measured459-build/out "$SNAP/measured459"
sha=$(zcat "$R/quant/recipe-measured459.yaml.gz" | sha256sum | cut -d' ' -f1)
cat > "$R/benchmarks/measured425-20261017/MEASURED-459-KEPT.md" <<EOF
# measured@4.5868 — kept for future use (built 2026-10-17)

- Snapshot: \`measured459\` -> measured459-build/out (registered in the EXL3-K4.25-v1 repo dir)
- Lineage: quant/recipe-measured459.yaml.gz (sha256 $sha) <- sc-measure/recipe-measured459.yaml
  <- knapsack(b=6.0, converged) on measurement-panel-l3.json <- patched exllamav3 measure
  (port-exl3-measure-ovr) on the author's panel corpus; achieved expert mean 4.5868 bpw
  (expert rate hist {3:16.9k, 4:16.4k, 5:23.0k, 6:17.4k}).
- Known limitation: engine (b12x fused-MoE loader) rejects >2 non-consecutive tiers, so it
  does not boot today. Full value offline: base-KLD measurement (sc-measure/basekld-measured459.json),
  future loader support, and it is the reference for the 4.59 frontier point.
- Rebuild path is fully in-repo: emit-recipe.py (budget 6.0) + build-measured459.sh.
EOF
for i in $(seq 1 240); do grep -qa "RB4 COMPLETE" "$LOG" && break; sleep 30; done
sleep 60
cd "$R" && git add benchmarks/measured425-20261017/MEASURED-459-KEPT.md scripts/sc/keep459.sh && git commit -q "measured459 preserved: snapshot registration + provenance (unserveable on current loader, kept for KLD reference and future support)" && git push -q gg quant-exp1 && say "snapshot + provenance committed" || say "WARN commit failed"

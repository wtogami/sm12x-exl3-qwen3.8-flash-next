# measured@4.5868 — kept for future use (built 2026-10-17)

- Snapshot: `measured459` -> measured459-build/out (registered in the EXL3-K4.25-v1 repo dir)
- Lineage: quant/recipe-measured459.yaml.gz (sha256 8896374de476d7e5b6e26df8b25ec0d82b4b82c51c774b54ae003dcff6866362) <- sc-measure/recipe-measured459.yaml
  <- knapsack(b=6.0, converged) on measurement-panel-l3.json <- patched exllamav3 measure
  (port-exl3-measure-ovr) on the author's panel corpus; achieved expert mean 4.5868 bpw
  (expert rate hist {3:16.9k, 4:16.4k, 5:23.0k, 6:17.4k}).
- Known limitation: engine (b12x fused-MoE loader) rejects >2 non-consecutive tiers, so it
  does not boot today. Full value offline: base-KLD measurement (sc-measure/basekld-measured459.json),
  future loader support, and it is the reference for the 4.59 frontier point.
- Rebuild path is fully in-repo: emit-recipe.py (budget 6.0) + build-measured459.sh.

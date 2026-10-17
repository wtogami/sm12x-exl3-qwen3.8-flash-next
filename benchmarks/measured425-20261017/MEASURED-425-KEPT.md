# measured@4.2465 — kept for future use (built 2026-10-17)

- Snapshot: `measured425` -> measured425-build/out (registered by RB4 at 15:25)
- Lineage: quant/recipe-measured425.yaml.gz (sha256 728464a4fd584d7787a0e1ec328f1fd01bbc28d8156819a23c3f191e7fbda2c6) <- emit-recipe.py (knapsack b=4.25)
  on measurement-panel-l3.json <- patched exllamav3 measure (port-exl3-measure-ovr) on the
  author's panel corpus; expert rate hist {3:20992, 4:24576, 5:19456, 6:8704}.
- Oracle-relevant: this allocation IS the optimizer's answer at the vendor's budget —
  first-order pred KLD 0.01414; true base KLD via sc-measure/basekld-measured425.json.
- Known limitation: engine b12x loader accepts only two consecutive tiers; card spans
  {3,4,5,6} so it does not boot today. Kept for base-KLD, loader-support follow-up, and
  rebuild reproducibility (emit-recipe.py budget 4.25 + build-measured425.sh, all in-repo).

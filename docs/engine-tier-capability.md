# Engine tier capability (T2 audit, 2026-10-17)

What the serving engine can actually load, per code path. Evidence: `/opt/b12x`
(kernel library, pinned `c76a40ee` per PROVENANCE) and the vLLM-side adapter
(`engine-overlay/exl3.py`, derived from the image via `patches/port-exl3-tier-key.py`).

| path | mixing | tier constraints (kernel-level) | proven by |
|---|---|---|---|
| uniform layer | one rate per layer, varies across layers | any integers 2..6 per layer | K3..K6V ladder, tuned heuristic cards (served+qualified 2026-10-16) |
| rank-sliced mixed (TP, no EP) | per-EXPERT tiers inside a layer | **2..3 distinct rates, NO adjacency requirement** (`w4a16/mixed_trellis.py:113-121`); adapter adds `len==2` only (`exl3.py:2130`) | vendor card on gg (TP1) today |
| projection-mixed EP (our TP2+EP serving config) | per-PROJECTION tiers inside a layer | **2..3 distinct AND CONSECUTIVE, canonical family (3,4,5)** (`fused_moe/trellis.py:96-98` `_projection_tier_bits`; `_CANONICAL_TIERS=(3,4,5)` at :32; adapter mirrors at `exl3.py:2294`) | vendor card {4,5} (its 3:5:8 gate/up/down mix lands exactly on the canonical adjacent pair) |

## Consequences

1. The two measured cards (`measured425`, `measured459`) are layer-mixed with
   non-adjacent projection tiers (e.g. {3,5}) — valid EXL3 **format**, invalid for every
   engine path beyond offline use. Kept as artifacts (`MEASURED-*-KEPT.md`).
2. **The blocker is kernel-library policy, not adapter code.** Revises the earlier
   interim assessment ("adapter-only fix"): `_projection_tier_bits` enforces
   consecutiveness inside b12x, and the EP native-tier builder hardcodes the (3,4,5)
   family. Support for {3,5} or 4-rate spans requires b12x changes (upstream request
   or pinned fork), plus descriptor/route-map specialization work.
3. **Serveable measured recipes need no kernel work** — pick granularity/mode:
   - `ENGINE_MODE=uniform-layer` (solve on l2 measurement): free rates; measured cost
     (first-order): 0.01949@4.24 vs 0.01414 l3 — behaviorally likely a wash under
     saturation, to be confirmed against the true-KL trio running now.
   - `ENGINE_MODE=rank-sliced-mixed`: per-expert 2-rate, e.g. {3,5} or {3,4,5}→2 used,
     TP2 without EP.
   - `ENGINE_MODE=proj-mixed-ep` with `TIER_WHITELIST=4,5` (or solve restricted to a
     consecutive triple): vendor-shaped l3 recipes boot today on the EP path.
4. `scripts/sc/emit-recipe.py` now enforces this matrix at emit time (env
   `ENGINE_MODE`, `TIER_WHITELIST`) — the class of tonight's failure (optimizer emits →
   engine rejects at boot) is structurally prevented.

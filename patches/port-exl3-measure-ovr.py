#!/usr/bin/env python3
"""port-exl3-measure-ovr: make measure_model's params["ovr"] swaps effective on the
block_sparse_mlp MoE (Qwen3.8-Flash-Next), and wire the packed-file calibration into
measure_model. Root cause of the 2026-10-16 all-dkld=0.0 measurement: the fused-kernel,
batched-reconstruct and bc graph/DQ tiers inside BlockSparseMLP.forward consume the
module's own weight buffers and never call the per-expert linear forwards, which is
where quant/exl3.py implements ovr delegation. dsv4.py guards this with a "converting"
check (line ~1584); block_sparse_mlp.py was written without it. Base KLD was correct
because baseline forwards don't need ovr - only the candidate forwards degenerated.

Fix (all guards inert unless params carry capture/quant_preserve/ovr/reconstruct):
  block_sparse_mlp.forward: under "converting", route to the dense per-expert torch
  path (bzn tier off, fused executor + min_rows + batched-recon + scratch tables off,
  bc graph/DQ branch off). mlp.py: same guard on the GatedMLP/ParallelGatedMLP fast
  tiers (shared experts are optimizer targets too).
  measure_model.py: -cd packed-token calibration (get_file_calibration, same safetensors
  contract as convert -cd), a first-chunk self-check that raises when two different
  candidate bitrates produce bit-identical override outputs, and a final degenerate-
  measurement abort when every dkld is exactly 0.0.

Targets the RUNTIME tree (exv3-env-161, 2026-10-13 vintage). Idempotent: refuses to
re-apply. Backups: <file>.pre-ovr-fix.orig
"""
import sys, shutil, pathlib

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                    else "/home/opencode/.cache/huggingface/exv3-env-161/exllamav3")

def patch(rel, edits):
    p = ROOT / rel
    s = p.read_text()
    if "__port-exl3-measure-ovr__" in s:
        sys.exit(f"ALREADY PATCHED: {p}")
    for old, new in edits:
        if s.count(old) != 1:
            sys.exit(f"ANCHOR {s.count(old)}x in {rel}: {old[:70]!r}")
        s = s.replace(old, new)
    shutil.copy(p, str(p) + ".pre-ovr-fix.orig")
    p.write_text(s)
    print(f"patched {p}")

BSM = "modules/block_sparse_mlp.py"
patch(BSM, [
    # marker rides on the converting-flags line
    ("        bszn_eligible = self.bc is not None and bsz <= MAX_BSZN",
     "        bszn_eligible = self.bc is not None and bsz <= MAX_BSZN\n"
     "        # __port-exl3-measure-ovr__: measure/optimize overrides live in the per-expert\n"
     "        # linear forwards (quant/exl3.py); every buffer-direct tier below must be bypassed\n"
     "        # or a params[\"ovr\"] silently reproduces the baseline (mirrors dsv4.py converting)\n"
     "        converting = any(k in params for k in (\"capture\", \"quant_preserve\", \"ovr\", \"reconstruct\"))\n"
     "        if converting:\n"
     "            bszn_eligible = False"),
    ("            (bsz >= self.f_threshold and not bszn_eligible) or not self.is_quantized or",
     "            (bsz >= self.f_threshold and not bszn_eligible) or converting or not self.is_quantized or"),
    ("                if self.fused_mode_buffers is not None and num_tokens * top_k <= self.fused_rows:",
     "                if self.fused_mode_buffers is not None and not converting \\\n"
     "                        and num_tokens * top_k <= self.fused_rows:"),
    ("                    if self.fused_mode_buffers is not None:\n"
     "                        min_rows = self.fused_rows",
     "                    if self.fused_mode_buffers is not None and not converting:\n"
     "                        min_rows = self.fused_rows"),
    ("                    recon = self._batch_recon_layer(y)",
     "                    recon = None if converting else self._batch_recon_layer(y)"),
    ("                # num_active -1 = unknown (all fused), kernel launches at max concurrency\n"
     "                if self.fused_mode_buffers is not None:",
     "                # num_active -1 = unknown (all fused), kernel launches at max concurrency\n"
     "                if self.fused_mode_buffers is not None and not converting:"),
    ("                    if self.bc is not None and self.support_quant_paths:\n"
     "                        # Graph path",
     "                    if self.bc is not None and self.support_quant_paths and not converting:\n"
     "                        # Graph path"),
])

patch("modules/mlp.py", [
    ("            self.bc is not None and bsz == 1 and q_len == 1 and",
     "            self.bc is not None and \"ovr\" not in params and bsz == 1 and q_len == 1 and  # __port-exl3-measure-ovr__"),
    ("                if self.bc is not None and bsz * q_len <= MAX_BSZN:",
     "                if self.bc is not None and \"ovr\" not in params and bsz * q_len <= MAX_BSZN:  # __port-exl3-measure-ovr__"),
    ("                elif self.multi_gu[s] is None or bsz * q_len > 32:",
     "                elif self.multi_gu[s] is None or bsz * q_len > 32 or \"ovr\" in params:  # __port-exl3-measure-ovr__"),
])

patch("conversion/measure_model.py", [
    ("from .calibration_data import get_default_calibration",
     "from .calibration_data import get_default_calibration, get_file_calibration"),
    ('parser.add_argument("-ms", "--max_sys", type = float, default = 8, help = "Max system memory for state data, in GB")',
     'parser.add_argument("-ms", "--max_sys", type = float, default = 8, help = "Max system memory for state data, in GB")\n'
     'parser.add_argument("-cd", "--cal_data", type = str, default = None, help = "Packed calibration file (safetensors: input_ids [rows, cols] + optional lengths)")'),
    ('        "cal_cols": args.cal_cols,',
     '        "cal_cols": args.cal_cols,\n        "cal_data": args.cal_data,'),
    ("    state = get_default_calibration(args, tokenizer)",
     "    state = get_file_calibration(args, tokenizer) if args.get(\"cal_data\") else get_default_calibration(args, tokenizer)"),
    ("        cand_kld = [[] for _ in range(num_cand)]\n        base_kld = 0",
     "        cand_kld = [[] for _ in range(num_cand)]\n        base_kld = 0\n"
     "        _ovr_prev = None   # __port-exl3-measure-ovr__ first-chunk inert-override self-check"),
    ("        flatten(r_targets, args[\"level\"])\n        targets_per_layer.append(s_targets)",
     "        flatten(r_targets, args[\"level\"])\n"
     "        # __port-exl3-measure-ovr__: drop groups whose targets are not EXL3-quantized.\n"
     "        # FP16/BF16 linears have no bitrate override (LinearFP16 never consults params['ovr']\n"
     "        # and the candidate models carry identical bytes for them); keeping such groups\n"
     "        # wastes a full-block forward per candidate and yields structurally-zero dkld rows.\n"
     "        s_targets = [g for g in s_targets if any(\n"
     "            config_q[0].stc.has_tensor(k + \".trellis\") for k in g)]\n"
     "        targets_per_layer.append(s_targets)"),
    ("                        s = modules[0].prepare_for_device(states_q, params)\n"
     "                        s = modules[0].forward(s, params)\n"
     "                        if last_fwd:\n"
     "                            cand_kld[k][-1] = kldiv(s, new_states_ref) - base_kld",
     "                        s = modules[0].prepare_for_device(states_q, params)\n"
     "                        s = modules[0].forward(s, params)\n"
     "                        if _ovr_prev is not None and _ovr_prev is not False and _ovr_prev[0] == idx:\n"
     "                            if torch.equal(_ovr_prev[1], s):\n"
     "                                raise RuntimeError(\n"
     "                                    f\"measure: params['ovr'] is INERT at module {idx} - two override groups \"\n"
     "                                    f\"produced bit-identical outputs; this architecture's MoE forward must \"\n"
     "                                    f\"route overrides through the per-expert linear forwards\")\n"
     "                            print(f\" -- {col_green}ovr self-check passed: override outputs diverge at module {idx}{col_default}\")\n"
     "                            _ovr_prev = False\n"
     "                        elif _ovr_prev is not False:\n"
     "                            _ovr_prev = (idx, s.clone())\n"
     "                        if last_fwd:\n"
     "                            cand_kld[k][-1] = kldiv(s, new_states_ref) - base_kld"),
    ("    # Compile and write results",
     "    if num_cand and all(v == 0.0 for lst in cand_kld for v in lst):\n"
     "        raise RuntimeError(\"measure: DEGENERATE - every candidate dkld is exactly 0.0 (override swap inert?)\")\n\n"
     "    # Compile and write results"),
])
print("OK - all three files patched")

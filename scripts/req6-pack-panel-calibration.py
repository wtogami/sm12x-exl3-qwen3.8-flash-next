#!/usr/bin/env python3
"""Pack agentic-quant-panel-v1 (calibration split) into the exllamav3
-cd format: safetensors with int32 input_ids (rows, cols) right-padded +
lengths per row. Tokenization recipe per the panel card: one user message
per record, target chat template applied once with add_generation_prompt.
Records target ~768 rendered tokens; width 2048 preserves every row."""
import json, sys, hashlib
import torch
from safetensors.torch import save_file
from transformers import AutoTokenizer

PANEL, MODEL_DIR, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
WIDTH = 2048
tok = AutoTokenizer.from_pretrained(MODEL_DIR)
rows, lens = [], []
for line in open(PANEL):
    r = json.loads(line)
    text = tok.apply_chat_template([{"role": "user", "content": r["prompt"]}],
                                   tokenize=False, add_generation_prompt=True)
    ids = tok(text, add_special_tokens=False)["input_ids"][:WIDTH]
    rows.append(ids); lens.append(len(ids))
n_over = sum(1 for l in lens if l == WIDTH)
t = torch.full((len(rows), WIDTH), tok.pad_token_id if tok.pad_token_id is not None else 0, dtype=torch.int32)
for i, ids in enumerate(rows): t[i, :len(ids)] = torch.tensor(ids, dtype=torch.int32)
assert t.max().item() < getattr(tok, "actual_vocab_size", 248320), "ids outside vocab"
save_file({"input_ids": t, "lengths": torch.tensor(lens, dtype=torch.int32)}, OUT,
          metadata={"format": "pt", "source": "agentic-quant-panel-v1/calibration", "tokenizer": MODEL_DIR})
sha = hashlib.sha256(open(OUT, "rb").read()).hexdigest()
print(f"rows={len(rows)} width={WIDTH} tokens={sum(lens):,} max_len={max(lens)} rows_at_width={n_over}")
print(f"sha256={sha} -> {OUT}")

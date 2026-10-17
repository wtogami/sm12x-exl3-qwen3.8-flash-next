# Stream the BF16 reference over the first 64 panel rows (cc=512) exactly as
# measure_model does, and dump per-position top-64 log-softmax + logsumexp.
# Same state pipeline as measure -> positions align 1:1 with vLLM prompt_logprobs.
import torch, json, os
from exllamav3 import Config, Model, Tokenizer
from exllamav3.conversion.calibration_data import get_file_calibration
from exllamav3.util.progress import ProgressBar
torch.set_grad_enabled(False)
dev = torch.device("cuda:0")
CFG = "/hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540"
args = {"cal_rows": 64, "cal_cols": 512, "cal_data": "/hf/calibration/agentic-quant-panel-v1/panel-qwen-packed-2048.safetensors"}
cfg = Config.from_directory(CFG)
model = Model.from_config(cfg)
tok = Tokenizer.from_config(cfg)
rows = get_file_calibration(args, tok)
states = torch.cat(rows, dim=0).to(dev)
params = {"activate_all_experts": True, "attn_mode": "flash_attn_nc"}
n = len(model.modules)
for i, module in enumerate(model.modules):
    print(f"module {i+1}/{n} {module.key}", flush=True)
    cfg.stc.begin_deferred_load(); module.load(dev); cfg.stc.end_deferred_load(); cfg.stc.close()
    s = module.prepare_for_device(states, params)
    s = module.forward(s, params)
    module.unload(); del module
    if i == n - 1:
        logits = s.float() if isinstance(s, torch.Tensor) else s.out.float()
        lse = torch.logsumexp(logits, dim=-1)
        topv, topi = torch.topk(logits, 64, dim=-1)
        torch.save({"topk_vals": topv.cpu(), "topk_ids": topi.cpu(), "lse": lse.cpu(),
                    "rows": states.cpu()}, "/hf/sc-measure/bf16-logits.pt")
        print("DUMPED", tuple(logits.shape))
    else:
        states = (s.cpu() if isinstance(s, torch.Tensor) else s.out.cpu())
        del s

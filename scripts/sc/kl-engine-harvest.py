# Harvest per-position prompt_logprobs (top-20) from a live engine for the SAME 64
# panel rows the BF16 tree dump processed. Token ids are regenerated via the exact
# get_file_calibration path measure_model/bf16-logitdump used -> deterministic alignment.
import json, sys, urllib.request, torch
from exllamav3 import Config, Tokenizer
from exllamav3.conversion.calibration_data import get_file_calibration
url, out = sys.argv[1], sys.argv[2]
CFG = "/hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540"
cfg = Config.from_directory(CFG)
tok = Tokenizer.from_config(cfg)
args = {"cal_rows": 64, "cal_cols": 512,
        "cal_data": "/hf/calibration/agentic-quant-panel-v1/panel-qwen-packed-2048.safetensors"}
ids = torch.cat(get_file_calibration(args, tok), dim=0)  # (64, 512) token ids, same cat as tree dump
assert ids.dtype in (torch.int32, torch.int64), ids.dtype
print("calibration ids", tuple(ids.shape), "max", int(ids.max()), flush=True)
res = []
for r in range(ids.shape[0]):
    body = json.dumps({"model": "qwen38-exl3", "prompt": ids[r].tolist(), "max_tokens": 1,
                       "temperature": 1.0, "prompt_logprobs": 20}  # T=1: vLLM logprobs are post-sampling-params; T=0 would report argmax deltas).encode()
    req = urllib.request.Request(url + "/v1/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        j = json.loads(resp.read())
    pl = j["choices"][0].get("prompt_logprobs")
    step = [{int(k): round(v["logprob"], 6) for k, v in (pos or {}).items()} for pos in pl]
    res.append(step)
    if (r + 1) % 8 == 0: print(f"row {r+1}/64", flush=True)
torch.save(res, out)
print("HARVESTED", out)

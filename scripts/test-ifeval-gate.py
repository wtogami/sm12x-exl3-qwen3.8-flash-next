#!/usr/bin/env python3
"""IFEval gate: Google's published instruction-following eval (541 prompts,
25 verifiable instruction types) scored with the OFFICIAL strict+loose checkers
(local copy under /hf/evals/ifeval). Deterministic greedy decoding; verification
runs on visible content only (reasoning excluded, per gate policy).
Receipts: per-prompt instruction outcomes + prompt-level pass."""
import argparse, json, os, sys, time, urllib.request
sys.path.insert(0, "/hf/evals/ifeval")
os.environ.setdefault("NLTK_DATA", "/hf/nltk_data")
from instruction_following_eval import instructions_registry as REG
ap = argparse.ArgumentParser()
ap.add_argument("--base-url", default="http://127.0.0.1:8001")
ap.add_argument("--model", required=True)
ap.add_argument("--output", required=True)
ap.add_argument("--limit", type=int, default=0)
a = ap.parse_args()
rows = [json.loads(l) for l in open("/hf/evals/ifeval/ifeval.jsonl")]
rows.sort(key=lambda r: r["key"])
if a.limit: rows = rows[:a.limit]
def check(inst_id, kw, resp):
    cls = REG.INSTRUCTION_DICT[inst_id]
    ins = cls(inst_id)
    keys = set(ins.get_instruction_args_keys())
    args = {k: v for k, v in (kw or {}).items() if k in keys}
    ins.build_description(**args)
    return ins.check_following(resp)
def loose_variants(resp):
    import re
    v = [resp]
    no_b = re.sub(r"[*_](.+?)[*_]", r"\1", resp, flags=re.S)
    v.append(no_b)
    v.append("\n".join(resp.split("\n")[1:]))
    v.append("\n".join(resp.split("\n")[:-1]))
    return v
tot_inst = ok_inst_strict = ok_inst_loose = ok_prompt_strict = ok_prompt_loose = 0
with open(a.output, "w") as f:
    for row in rows:
        body = json.dumps({"model": a.model, "temperature": 0, "max_tokens": 2048,
                           "messages": [{"role": "user", "content": row["prompt"]}]}, ).encode()
        t0 = time.time()
        req = urllib.request.Request(a.base_url + "/v1/chat/completions", body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            d = json.loads(r.read())
        ch = d["choices"][0]
        resp = ch["message"].get("content") or ""
        variants = loose_variants(resp)
        res = []
        for inst_id, kw in zip(row["instruction_id_list"], row["kwargs"]):
            try: s = bool(check(inst_id, kw, resp))
            except Exception: s = False
            l = s or any(bool(check(inst_id, kw, v)) for v in variants)
            res.append({"id": inst_id, "strict": s, "loose": l})
            tot_inst += 1; ok_inst_strict += s; ok_inst_loose += l
        ps = all(x["strict"] for x in res); pl = all(x["loose"] for x in res)
        ok_prompt_strict += ps; ok_prompt_loose += pl
        f.write(json.dumps({"record": "measurement", "key": row["key"], "n_inst": len(res),
              "instructions": res, "prompt_strict": ps, "prompt_loose": pl,
              "finish": ch.get("finish_reason"), "seconds": round(time.time()-t0,2),
              "vis_len": len(resp)}) + "\n"); f.flush()
    n = len(rows)
    f.write(json.dumps({"record": "summary", "total": n,
          "prompt_strict": round(ok_prompt_strict/n,4), "prompt_loose": round(ok_prompt_loose/n,4),
          "inst_strict": round(ok_inst_strict/tot_inst,4), "inst_loose": round(ok_inst_loose/tot_inst,4)}) + "\n")
print(f"IFEVAL prompt={ok_prompt_strict}/{n} strict loose={ok_prompt_loose}/{n} inst={ok_inst_strict}/{tot_inst}")

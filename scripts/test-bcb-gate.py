#!/usr/bin/env python3
"""BigCodeBench-Hard (v0.1.2, 148 tasks) gate. Two-stage, deterministic:
  --stage gen   : chat-completion per task (greedy), extract one python fence -> gen file
  --stage score : execute generation + official unittest tests in a SANDBOXED process
                  (run the score stage with --network=none); pass@1 by unittest success.
Deviation from leaderboard: model receives the def-signature scaffold (code_prompt)
as context; protocol is fixed across cards, which is what a regression gate needs."""
import argparse, json, os, re, subprocess, sys, tempfile, time, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--base-url", default="http://127.0.0.1:8001")
ap.add_argument("--model", default="")
ap.add_argument("--output", required=True)
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--stage", choices=["gen", "score", "all"], default="all")
ap.add_argument("--gen-file", default="")
a = ap.parse_args()
tasks = [json.loads(l) for l in open("/hf/evals/bigcodebench/hard.jsonl")]
tasks.sort(key=lambda t: t["task_id"])
if a.limit: tasks = tasks[:a.limit]

SYS = ("You are an expert Python programmer. Implement the requested function exactly. "
       "Output ONLY one ```python code block containing the complete code (imports and the "
       "function definition). No prose, no tests, no usage example.")
fence_re = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)

def gen_all():
    rows = []
    with open(a.output, "w") as f:
        for t in tasks:
            user = (t["instruct_prompt"] + "\n\nScaffold (complete this implementation):\n```python\n"
                    + t["code_prompt"] + "```")
            body = json.dumps({"model": a.model, "temperature": 0, "max_tokens": 1600,
                "chat_template_kwargs": {"enable_thinking": False}, "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": user}]}).encode()
            req = urllib.request.Request(a.base_url + "/v1/chat/completions", body,
                                         headers={"Content-Type": "application/json"})
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=900) as r:
                d = json.loads(r.read())
            ch = d["choices"][0]
            vis = ch["message"].get("content") or ""
            m = fence_re.search(vis)
            code = m.group(1) if m else vis
            if not code.strip():
                code = ""
            row = {"task_id": t["task_id"], "record": "gen", "code": code,
                   "finish": ch.get("finish_reason"), "seconds": round(time.time()-t0, 2)}
            f.write(json.dumps(row) + "\n"); f.flush(); rows.append(row)
    return rows

RUNNER = ("\n\nimport unittest, sys\n"
          "_r = unittest.TextTestRunner(verbosity=0).run("
          "unittest.defaultTestLoader.loadTestsFromModule(sys.modules['__main__']))\n"
          "sys.exit(0 if _r.wasSuccessful() else 1)\n")

def score(gen_rows):
    out = []
    by_id = {t["task_id"]: t for t in tasks}
    with tempfile.TemporaryDirectory() as td:
        for g in gen_rows:
            t = by_id.get(g["task_id"])
            if not t: continue
            path = os.path.join(td, g["task_id"].replace("/", "_") + ".py")
            if not g["code"].strip():
                out.append({"task_id": g["task_id"], "record": "measurement", "status": "empty",
                            "detail": "no visible code", "gen_seconds": g.get("seconds")})
                continue
            open(path, "w").write(g["code"] + "\n\n" + t["test"] + RUNNER)
            status, detail = "pass", ""
            try:
                p = subprocess.run([sys.executable, path], capture_output=True, text=True,
                                   timeout=60, cwd=td,
                                   env={"PATH": "/usr/bin:/bin", "HOME": td, "MPLBACKEND": "Agg",
                                        "OPENBLAS_NUM_THREADS": "2", "OMP_NUM_THREADS": "2"})
                if p.returncode != 0:
                    err = (p.stderr or "")
                    kind = "fail"
                    if "ModuleNotFoundError" in err or "ImportError" in err: kind = "env"
                    elif re.search(r"NameError.*" + re.escape(t["entry_point"]), err): kind = "name"
                    status, detail = kind, err.strip().splitlines()[-1][:160] if err.strip() else "rc!=0"
            except subprocess.TimeoutExpired:
                status, detail = "timeout", "60s"
            out.append({"task_id": g["task_id"], "record": "measurement", "status": status,
                        "detail": detail, "gen_seconds": g.get("seconds")})
    n = len(out); ok = sum(1 for r in out if r["status"] == "pass")
    counts = {}
    for r in out: counts[r["status"]] = counts.get(r["status"], 0) + 1
    with open(a.output, "w") as f:
        for r in out: f.write(json.dumps(r) + "\n")
        f.write(json.dumps({"record": "summary", "total": n, "passed": ok,
              "pass_at_1": round(ok/n, 4) if n else 0, "statuses": counts}) + "\n")
    print(f"BCB-HARD pass@1={ok}/{n} statuses={counts}")

if a.stage in ("gen", "all"):
    rows = gen_all()
    if a.stage == "gen":
        print(f"gen done: {len(rows)} generations -> {a.output}")
    else:
        gen_path, a.output = a.output, (a.gen_file or a.output.replace(".jsonl", "") + "-scored.jsonl")
        rows = [json.loads(l) for l in open(gen_path)]
        score(rows)
if a.stage == "score":
    rows = [json.loads(l) for l in open(a.gen_file)]
    score(rows)

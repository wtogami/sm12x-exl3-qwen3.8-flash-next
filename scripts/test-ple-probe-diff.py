import json, subprocess, sys
P = "/home/opencode/sm12x-exl3-qwen3.8-flash-next/scripts/ple-probe.py"
names = ["long-context","code","arithmetic","verbatim","multilingual","multi-step"]
def w(path, label, texts):
    with open(path,"a") as f:
        for n,t in zip(names, texts):
            f.write(json.dumps({"label":label,"name":n,"prompt_tokens":100,"strict":"x","text":t})+"\n")
# case 1: identical
f1="/tmp/opencode/d1.jsonl"
open(f1,"w").close()
ts=[f"answer {i} "*8 for i in range(6)]
w(f1,"A",ts); w(f1,"B",ts)
r=subprocess.run([sys.executable,"-I",P,"--diff",f1,"A","B"],capture_output=True,text=True)
print("[identical]", r.returncode); print("\n".join("   "+l for l in r.stdout.splitlines()[:3]))
assert r.returncode==0 and "IDENTICAL" in r.stdout
# case 2: one early cascade flip in 2 of 6
f2="/tmp/opencode/d2.jsonl"; open(f2,"w").close()
ts2=list(ts); ts2[1]="answer 1 aliz "*3+"totally different tail "*5; ts2[3]="completely different from char zero "*4
w(f2,"A",ts); w(f2,"B",ts2)
r=subprocess.run([sys.executable,"-I",P,"--diff",f2,"A","B"],capture_output=True,text=True)
print("[cascade]", r.returncode); print("\n".join("   "+l for l in r.stdout.splitlines() if l.strip()))
assert r.returncode==1 and "2 of 6 prompts differ" in r.stdout, r.stdout
# case 3: mismatched labels must be an error, not a silent pass
f3="/tmp/opencode/d3.jsonl"; open(f3,"w").close(); w(f3,"A",ts)
r=subprocess.run([sys.executable,"-I",P,"--diff",f3,"A","B"],capture_output=True,text=True)
print("[missing label]", r.returncode, r.stdout.strip())
assert r.returncode==1
# case 4: empty file
f4="/tmp/opencode/d4.jsonl"; open(f4,"w").close()
r=subprocess.run([sys.executable,"-I",P,"--diff",f4,"A","B"],capture_output=True,text=True)
print("[empty]", r.returncode, r.stdout.strip()); assert r.returncode==1
print("all four diff-mode cases behave")

# ---- resolve_model: the served name must come from the engine -----------------
import importlib.util, io
spec = importlib.util.spec_from_file_location("ple_probe", P)
pp = importlib.util.module_from_spec(spec); spec.loader.exec_module(pp)

seen = {}
def make_opener(payload):
    def opener(req, timeout=60):
        seen["url"] = req.full_url
        return io.StringIO(payload)
    return opener

# case 5: the models URL is derived from the completions URL, and the first id wins
got = pp.resolve_model("http://127.0.0.1:8001/v1/completions",
                       opener=make_opener('{"data":[{"id":"qwen38-exl3"},{"id":"other"}]}'))
print("[resolve]", seen["url"], "->", got)
assert seen["url"] == "http://127.0.0.1:8001/v1/models", seen["url"]
assert got == "qwen38-exl3", got

# case 6: an empty model list is an error, not an empty string that every request then 404s on
try:
    pp.resolve_model("http://127.0.0.1:8001/v1/completions", opener=make_opener('{"data":[]}'))
    raise AssertionError("empty /v1/models must raise")
except RuntimeError as e:
    print("[resolve empty]", str(e)[:60])

print("resolve_model behaves for both hosts' profiles")


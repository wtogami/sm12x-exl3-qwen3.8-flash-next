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

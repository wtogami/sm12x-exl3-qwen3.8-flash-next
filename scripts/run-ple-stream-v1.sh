#!/bin/bash
# Run on gg. v1 acceptance: breakable graphs + cudagraph_mode=PIECEWISE, since v1 gathers inside the
# captured region and a FULL graph declines to break for host code. Superseded by run-ple-stream-v2.sh
# (v1 costs 22.2% decode vs v2's 7.4%, of which 17.8pp was the graph regime alone); kept because it is
# the design the fallback flag still selects and because its restore-trap shape is load-bearing.
# Streaming acceptance, attempt 2: breakable CUDA graphs enabled (the only mode where a host-side
# gather can run correctly per replay). Evidence is archived before any container is removed.
set -uo pipefail
ACC=/home/rtx/accept
BASE_SHA=f91132436fb09e45
LOG=$ACC/stream2.log

archive_then_restore() {
  local rc=$?
  podman inspect ple-stream-test > $ACC/stream2-inspect.json 2>/dev/null || true
  podman logs ple-stream-test > "$LOG" 2>&1 || true
  echo "  [restore] archived $(wc -l < "$LOG" 2>/dev/null || echo 0) log lines to $LOG"
  podman rm -f ple-stream-test >/dev/null 2>&1 || true
  podman start qwen38-exl3-ple8 >/dev/null 2>&1 || bash $ACC/run-restore.sh >/dev/null 2>&1
  local code=000
  for i in $(seq 1 90); do
    code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' http://127.0.0.1:8001/health 2>/dev/null)
    [ "$code" = "200" ] && break; sleep 10
  done
  sleep 15
  bash /home/rtx/greedy.sh > $ACC/greedy-restored3.txt 2>&1
  local sha=$(sha256sum < $ACC/greedy-restored3.txt | cut -c1-16)
  echo "  [restore] health=$code  greedy=$sha  $([ "$sha" = "$BASE_SHA" ] && echo MATCH || echo MISMATCH)"
  awk '/^Shmem:/ {printf "  [restore] Shmem %.2f GiB\n", $2/1048576}' /proc/meminfo
  nvidia-smi --query-gpu=memory.used --format=csv,noheader | sed 's/^/  [restore] vram /'
  return $rc
}
trap archive_then_restore EXIT INT TERM

echo "=== ensure the streaming run line asks for breakable graphs ==="
if ! grep -q VLLM_USE_BREAKABLE_CUDAGRAPH $ACC/run-stream.sh; then
  sed -i 's|\(localhost/qwen38-exactpin:stream\)|-e VLLM_USE_BREAKABLE_CUDAGRAPH=1 \1|' $ACC/run-stream.sh
fi
sed -i 's/-e VLLM_USE_BREAKABLE_CUDAGRAPH=[01]* -e VLLM_USE_BREAKABLE_CUDAGRAPH=1/-e VLLM_USE_BREAKABLE_CUDAGRAPH=1/' $ACC/run-stream.sh
grep -o "\-e VLLM_USE_BREAKABLE_CUDAGRAPH=[0-9]*" $ACC/run-stream.sh | sed 's/^/  /'
python3 - <<'PY'
import shlex
toks = shlex.split(open("/home/rtx/accept/run-stream.sh").read().strip().splitlines()[-1][5:])
img = next((i for i, t in enumerate(toks) if "exactpin:stream" in t), None)
bad = [t for t in toks[img+1:] if t in ("-e", "-d", "--detach")]
print("  flags after image:", bad or "none")
assert not bad, "run line is malformed"
PY

echo "=== stop serving engine, start streaming ==="
podman stop -t 30 qwen38-exl3-ple8 >/dev/null && echo "  stopped"
sleep 5
bash $ACC/run-stream.sh >/dev/null 2>&1 && echo "  container started" || { echo "  run REJECTED"; exit 1; }
code=000
for i in $(seq 1 150); do
  if [ -z "$(podman ps -q --filter name=ple-stream-test)" ]; then
    echo "  container EXITED at t=$((i*10))s"; exit 1
  fi
  code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' http://127.0.0.1:8001/health 2>/dev/null)
  [ "$code" = "200" ] && { echo "  HEALTHY after $((i*10))s"; break; }
  sleep 10
done
[ "$code" = "200" ] || { echo "  never became healthy"; exit 1; }

echo
echo "=== 1. graph mode and backend selection ==="
podman logs ple-stream-test 2>&1 | grep -m1 -iE "breakable|cudagraph_mode|CompilationMode" | cut -c1-150 | sed 's/^/  /'
podman logs ple-stream-test 2>&1 | grep -m1 "streaming backend selected" | cut -c1-150 | sed 's/^/  /'
podman logs ple-stream-test 2>&1 | grep -ciE "Cannot copy between CPU and CUDA" | sed 's/^/  capture-copy errors: /'

echo
echo "=== 2. memory: the table must not be pinned (expect Shmem ~2.2 GiB, baseline 66.18) ==="
awk '/^Shmem:|^Cached:|^MemAvailable:/ {printf "  %-14s %8.2f GiB\n", $1, $2/1048576}' /proc/meminfo
nvidia-smi --query-gpu=memory.used --format=csv,noheader | sed 's/^/  vram used: /'

echo
echo "=== 3. correctness: greedy output vs pinned baseline $BASE_SHA ==="
bash /home/rtx/greedy.sh > $ACC/greedy-stream.txt 2>&1
sha=$(sha256sum < $ACC/greedy-stream.txt | cut -c1-16)
echo "  streaming sha: $sha   bytes: $(wc -c < $ACC/greedy-stream.txt)"
if [ "$sha" = "$BASE_SHA" ]; then echo "  PASS byte-identical"; else
  echo "  FAIL differs:"; diff $ACC/greedy-pinned-run1.txt $ACC/greedy-stream.txt | head -12 | sed 's/^/    /'
fi

echo
echo "=== 4. read behaviour: device pages read by a second greedy burst (cache now warm) ==="
r0=$(awk '$3=="nvme0n1"{print $6}' /proc/diskstats)
bash /home/rtx/greedy.sh >/dev/null 2>&1
r1=$(awk '$3=="nvme0n1"{print $6}' /proc/diskstats)
echo "  sectors read: $((r1-r0))  (~$(( (r1-r0)/2/1024/1024 )) GiB)"
awk '/^Cached:/ {printf "  page cache: %.2f GiB\n", $2/1048576}' /proc/meminfo

echo
echo "=== 5. throughput probe (not a verdict -- graph mode differs from baseline) ==="
python3 - <<'PY'
import json, time, urllib.request
def bench(n, maxtok):
    t0 = time.time(); tok = 0
    for i in range(n):
        body = json.dumps({"model": "qwen38-exl3-ple8", "temperature": 0,
                           "max_tokens": maxtok,
                           "messages": [{"role": "user", "content": "Explain page-cache reuse in long-context inference, in detail."}]}).encode()
        req = urllib.request.Request("http://127.0.0.1:8001/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            d = json.load(r)
        tok += d["usage"]["completion_tokens"] + d["usage"]["prompt_tokens"]
    dt = time.time() - t0
    print(f"  {n} reqs, {maxtok} max_tokens: {dt:.2f}s  {tok/dt:8.1f} tok/s aggregate")
bench(4, 128)
bench(4, 512)
PY
echo "  (baseline numbers for the pinned engine are captured next, same script, same host state)"
python3 - <<'PY'
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:8001/metrics", timeout=10) as r:
    txt = r.read().decode()
for line in txt.splitlines():
    if line.startswith("vllm:time_to_first_token_seconds_sum") or line.startswith("vllm:e2e_request_latency_seconds_sum"):
        print("  " + line[:100])
PY
echo ACCEPT2DONE

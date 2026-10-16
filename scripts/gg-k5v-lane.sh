#!/usr/bin/env bash
# K5V on gg (idle GPU, recipe-uniform5 native there) -> repair on gg -> copy+verify on burst.
# Vanilla protocol (no -cd, K6V-mirror). Log: ~/.cache/huggingface/gg-k5v.log
set -u
GG="rtx@gg"; R=/home/opencode/sm12x-exl3-qwen3.8-flash-next
CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
LOG="$CACHE/gg-k5v.log"; say(){ echo "[$(date +%m-%d_%H:%M)] $*" >>"$LOG"; }
[[ -f "$CACHE/req5v-build/.transfer-complete" ]] && { echo "already complete"; exit 0; }

ssh -o ConnectTimeout=8 $GG '
set -e
[[ -f ~/.cache/huggingface/req6-build/recipe-uniform5.yaml ]] || { echo "no recipe on gg"; exit 1; }
[[ -d ~/.cache/huggingface/exv3-env-161 ]] || { echo "no env on gg"; exit 1; }
mkdir -p ~/.cache/huggingface/req5v-build/out ~/.cache/huggingface/req5v-build/work
RESUME=(); [[ -f ~/.cache/huggingface/req5v-build/work/ckpt/job.json ]] && RESUME=(-r)
podman rm -f req5v-convert 2>/dev/null || true
podman run -d --name req5v-convert --device nvidia.com/gpu=0 \
  -v ~/.cache/huggingface:/hf:z --entrypoint sh localhost/qwen38-rtx:local -c "
export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras
export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:\$LD_LIBRARY_PATH
exec python3 -m exllamav3.conversion.convert_model \
  -i /hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540 \
  -o /hf/req5v-build/out -w /hf/req5v-build/work \
  -rcp /hf/req6-build/recipe-uniform5.yaml -b 5 -hb 16 -mb 6 -vb 16 -cb mcg -d 0 ${RESUME[*]:-}"
echo gg-req5v-launched' || { say "FATAL: gg launch failed"; exit 1; }
say "K5V convert launched on gg"

ssh $GG 'cat > ~/gg-k5v-repair.sh && chmod +x ~/gg-k5v-repair.sh' <<'REOF'
#!/usr/bin/env bash
set -u
LOG="$HOME/.cache/huggingface/gg-k5v-repair.log"; say(){ echo "[$(date +%m-%d_%H:%M)] $*" >>"$LOG"; }
say "repair watcher start"
for i in $(seq 1 960); do
  podman logs req5v-convert 2>&1 | grep -qa "Finished compiling" && break
  sleep 30
done
podman logs req5v-convert 2>&1 | grep -qa "Finished compiling" || { say "FAIL: no completion marker"; exit 1; }
say "compiled; repair chain"
podman run --rm --device nvidia.com/gpu=0 -v "$HOME/.cache/huggingface:/hf:z" -v "$HOME/qscripts:/s:z" --entrypoint sh localhost/qwen38-rtx:local -c '
export PYTHONPATH=/hf/exv3-env-161:/hf/exv3-runtime:/hf/pip-extras
export LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/torch/lib:$LD_LIBRARY_PATH
set -e
SRC=/hf/hub/models--Qwen--Qwen3.8-Flash-Next/snapshots/de4b8e4d43b917e7706784d8bb445c9af86a3540
python3 /s/req6-ngram-reinject.py $SRC /hf/req5v-build/out | tail -1
python3 /s/req6-mtp-repair.py /hf/req5v-build/out | tail -1
python3 /s/req6-ledger-filter.py /hf/req5v-build/out/quantization_config.json
python3 /s/req6-strip-ghost-keys.py /hf/req5v-build/out | tail -1
' >>"$LOG" 2>&1 && touch "$HOME/.cache/huggingface/req5v-build/REPAIR-OK" && say "gg REPAIR-OK" || say "FAIL repair chain"
REOF
ssh $GG 'setsid nohup bash ~/gg-k5v-repair.sh >/dev/null 2>&1 & echo "gg repair watcher armed"' >>"$LOG" 2>&1

setsid nohup bash -c '
CACHE="'"$CACHE"'"; R="'"$R"'; LOG="$CACHE/gg-k5v.log"; say(){ echo "[$(date +%m-%d_%H:%M)] $*" >>"$LOG"; }
say "copier armed"
for i in $(seq 1 1440); do
  ssh -o ConnectTimeout=8 rtx@gg "[[ -f ~/.cache/huggingface/req5v-build/REPAIR-OK ]]" 2>/dev/null && break
  sleep 60
done
ssh -o ConnectTimeout=8 rtx@gg "[[ -f ~/.cache/huggingface/req5v-build/REPAIR-OK ]]" 2>/dev/null || { say "FATAL: gg never REPAIR-OK"; exit 1; }
say "gg REPAIR-OK seen; rsync (ngram excluded)"
mkdir -p "$CACHE/req5v-build"
rsync -a --exclude "model-ngram-*" rtx@gg:.cache/huggingface/req5v-build/out/ "$CACHE/req5v-build/out/" >>"$LOG" 2>&1 \
  || { say "FATAL: rsync failed"; exit 1; }
say "rsync done; hardlinking canonical ngram shards"
for f in "$CACHE"/req3-build/out/model-ngram-*.safetensors; do ln -f "$f" "$CACHE/req5v-build/out/$(basename "$f")"; done \
  || { say "FATAL: ngram injection failed"; exit 1; }
python3 "$R/scripts/gg-k5v-verify.py" "$CACHE" >>"$LOG" 2>&1 \
  && { touch "$CACHE/req5v-build/.transfer-complete"; say "K5V ON BURST COMPLETE: verified + deduped"; } \
  || say "WARN: verify failed - manual look needed"
' >/dev/null 2>&1 &
say "copier watcher pid $!"
echo "K5V remote lane armed (log: $LOG)"

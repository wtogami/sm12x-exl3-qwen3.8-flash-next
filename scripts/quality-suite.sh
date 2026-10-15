#!/usr/bin/env bash
# Quality-only qualification for the additional EXL3 FP8-PLE checkpoint.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BASE_URL="${BASE_URL:-http://127.0.0.1:8001}"
MODEL="${MODEL:-qwen38-exl3-ple8}"
RESULT_DIR="${RESULT_DIR:?Set a new result directory}"
[[ ! -e "$RESULT_DIR" ]] || { echo "Results already exist: $RESULT_DIR" >&2; exit 1; }
mkdir -p "$RESULT_DIR"
COMMON=(--base-url "$BASE_URL" --model "$MODEL")
python3 "$SCRIPT_DIR/test-api-tool-constraints.py" "${COMMON[@]}" --output "$RESULT_DIR/api-tools.jsonl"
python3 "$SCRIPT_DIR/test-vision-vllm.py" "${COMMON[@]}" --image-counts 1 4 16 --output "$RESULT_DIR/vision.json"
# Preserve responses and incidental request timings; do not infer performance equivalence.
python3 "$SCRIPT_DIR/benchmark-workloads.py" "${COMMON[@]}" --suite seven --runs 12 --warmups 0 --output "$RESULT_DIR/seven.jsonl"
python3 "$SCRIPT_DIR/benchmark-workloads.py" "${COMMON[@]}" --suite orchid --runs 20 --warmups 0 --output "$RESULT_DIR/orchid.jsonl"
python3 "$SCRIPT_DIR/test-context-retrieval.py" "${COMMON[@]}" --filler-tokens 8192 240000 --positions 0.05 0.5 0.95 --output "$RESULT_DIR/retrieval.jsonl"
python3 "$SCRIPT_DIR/test-biomed-analysis.py" "${COMMON[@]}" --runs 4 --output "$RESULT_DIR/biomed.jsonl"
# Published deterministic gates (run in-image; need nltk/langdetect + /hf/evals data)
in_image() { # gate-script receipt-file limit-envvar
  local script=$1 out=$2 lim=$3 limarg=""
  [[ -n "${!lim:-}" && "${!lim}" != "0" ]] && limarg="--limit ${!lim}"
  docker run --rm --network=host \
    -v "${HF_CACHE:-$HOME/.cache/huggingface}:/hf:z" -v "$SCRIPT_DIR:/s:z" -v "$(realpath "$RESULT_DIR"):/r:z" \
    -e PYTHONPATH=/hf/pip-extras --entrypoint python3 localhost/qwen38-rtx:local \
    "/s/$script" --base-url "$BASE_URL" --model "$MODEL" --output "/r/$out" $limarg "${@:4}"
}
in_image test-pubmedqa-gate.py pubmedqa.jsonl PQA_LIMIT
in_image test-ifeval-gate.py ifeval.jsonl IFE_LIMIT

# BigCodeBench-Hard: generate against engine, score in network-isolated container
in_image test-bcb-gate.py bcb-gen.jsonl BCB_LIMIT --stage gen
docker run --rm --network=none \
  -v "${HF_CACHE:-$HOME/.cache/huggingface}:/hf:z" -v "$SCRIPT_DIR:/s:z" -v "$(realpath "$RESULT_DIR"):/r:z" \
  --entrypoint python3 localhost/qwen38-bcb:local \
  /s/test-bcb-gate.py --stage score --gen-file /r/bcb-gen.jsonl --output /r/bcb.jsonl

# Run the pinned full tool-eval-bench suite separately, including Hard Mode.

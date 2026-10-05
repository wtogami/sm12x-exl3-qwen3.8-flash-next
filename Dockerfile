# syntax=docker/dockerfile:1.7
ARG EXL3_SOURCE_IMAGE=ghcr.io/tpurtell/glm-5.3-flash-exl3-4bpw-2x-rtx@sha256:48e254d94f58137c8707e6044cde4528c6af3fdd9702726b9b362e9b0e0b4629
# vllm/vllm-openai:v0.31.0 (official multiarch images, CUDA 13.0; upstream now
# contains the QSA FP8 KV cache (PR 55557), so only the mmap PLE backport
# remains vendored).
ARG VLLM_BASE_IMAGE=docker.io/vllm/vllm-openai@sha256:a4a4c0437bf7240089da5f08aa370c4aee17ae5290f7a3b468825ee26c4c3a6b
FROM --platform=linux/amd64 ${EXL3_SOURCE_IMAGE} AS exl3_source
FROM ${VLLM_BASE_IMAGE}
ARG CUTE_DSL_ARCH=sm_120a
ARG B12X_VOCAB=0
ENV CUTE_DSL_ARCH=${CUTE_DSL_ARCH} QWEN38_B12X_VOCAB=${B12X_VOCAB}
ARG B12X_COMMIT=c76a40ee684cb3ef7d2c223d56a9b9cff25a3a1e
RUN B12X_COMMIT=${B12X_COMMIT} python3 - <<'PY'
import os, tarfile, urllib.request
from pathlib import Path
commit = os.environ['B12X_COMMIT']
urllib.request.urlretrieve(f'https://github.com/tpurtell/sparkinfer-glmrt/archive/{commit}.tar.gz', '/tmp/b12x.tar.gz')
with tarfile.open('/tmp/b12x.tar.gz') as archive:
    archive.extractall('/opt', filter='data')
Path(f'/opt/sparkinfer-glmrt-{commit}').rename('/opt/b12x')
Path('/tmp/b12x.tar.gz').unlink()
PY
RUN python3 -m pip install --no-deps -e /opt/b12x
COPY --from=exl3_source /usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/quantization/exl3.py /usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/quantization/exl3.py
COPY patches/port-exl3-qwen38.py /tmp/port-exl3-qwen38.py
COPY patches/qwen_host_embedding.py /usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/qwen_host_embedding.py
COPY patches/qwen_vocab_projection.py /usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/qwen_vocab_projection.py
COPY patches/qwen_nvfp4_moe.py /usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/qwen_nvfp4_moe.py
RUN python3 /tmp/port-exl3-qwen38.py /usr/local/lib/python3.12/dist-packages/vllm \
 && python3 -c 'from vllm.model_executor.layers.quantization import get_quantization_config; assert get_quantization_config("exl3").__name__ == "Exl3Config"'

# Upstream correction merged after the v0.31.0 branch cut, applied at a pinned
# commit with strict base hashes and fuzz 0:
#   PR 58439 + stacked 58835  checkpoint-mapped (mmap) PLE table with readahead
# (PR 55557's fp8_e4m3 QSA KV cache shipped upstream in v0.31.0 and was dropped
# from this recipe.)
COPY patches/port-ple-mmap.py patches/ple-mmap-pr58439-58835-v0.31.patch patches/ple-mmap-base-hashes.json /tmp/ple-mmap/
RUN python3 /tmp/ple-mmap/port-ple-mmap.py /usr/local/lib/python3.12/dist-packages/vllm
LABEL io.tpurtell.ple-mmap.pr="58439+58835" \
      io.tpurtell.ple-mmap.commit="47b9933db82d"

# Recipe-local ports, applied after the vendored upstream patches.
COPY patches/port-host-embedding.py /tmp/port-host-embedding.py
COPY patches/port-vocab-projection.py /tmp/port-vocab-projection.py
COPY patches/port-nvfp4-moe.py /tmp/port-nvfp4-moe.py
COPY patches/port-exl3-ple-fp8.py /tmp/port-exl3-ple-fp8.py
RUN python3 /tmp/port-host-embedding.py /usr/local/lib/python3.12/dist-packages/vllm \
 && python3 /tmp/port-vocab-projection.py /usr/local/lib/python3.12/dist-packages/vllm \
 && python3 /tmp/port-nvfp4-moe.py /usr/local/lib/python3.12/dist-packages/vllm \
 && python3 /tmp/port-exl3-ple-fp8.py /usr/local/lib/python3.12/dist-packages/vllm \
 && python3 -c 'from vllm.model_executor.layers.quantization import get_quantization_config; assert get_quantization_config("exl3").__name__ == "Exl3Config"' \
 && python3 -c 'import vllm.models.qwen4_exp.nvidia.ple_pageable, vllm.models.qwen4_exp.nvidia.ngram_embedding'
ENV EXL3_TRELLIS_MIN_M=1 \
    EXL3_PREFILL_TRELLIS=1 \
    EXL3_PREFILL_CAPACITY=2048

# Structured-output regressions: the reasoning-end guard (c6e19b3be243) and the
# XGrammar termination fix (PR 52805) shipped upstream in v0.30.0; these CPU
# AST regressions keep the fixes verified in the assembled image.
COPY scripts/test-xgrammar-termination.py scripts/test-structured-output-reasoning.py /opt/qwen38-tests/
RUN python3 /opt/qwen38-tests/test-xgrammar-termination.py \
 && python3 /opt/qwen38-tests/test-structured-output-reasoning.py
LABEL org.opencontainers.image.source="https://github.com/tpurtell/sm12x-exl3-qwen3.8-flash-next" \
      io.tpurtell.b12x.commit="${B12X_COMMIT}" \
      io.tpurtell.structured-output.reasoning-fix="c6e19b3be243-upstream" \
      io.tpurtell.structured-output.termination-fix="vllm-pr-52805-upstream"

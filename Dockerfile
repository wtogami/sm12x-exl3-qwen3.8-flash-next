# syntax=docker/dockerfile:1.7
ARG EXL3_SOURCE_IMAGE=ghcr.io/tpurtell/glm-5.3-flash-exl3-4bpw-2x-rtx@sha256:48e254d94f58137c8707e6044cde4528c6af3fdd9702726b9b362e9b0e0b4629
# vllm/vllm-openai:v0.32.0 (official multiarch images, CUDA 13.0; pinned to the
# tag digest reported by the registry).
ARG VLLM_BASE_IMAGE=docker.io/vllm/vllm-openai@sha256:befccf0e7e4089aa23926bb6f887a85c0d46c2a99e6de5c5ffdf5cb7957c5b34
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
COPY patches/qwen_pinned_host.py /usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/qwen_pinned_host.py
RUN python3 /tmp/port-exl3-qwen38.py /usr/local/lib/python3.12/dist-packages/vllm \
 && python3 -c 'from vllm.model_executor.layers.quantization import get_quantization_config; assert get_quantization_config("exl3").__name__ == "Exl3Config"'

# Upstream corrections merged after the v0.31.0 branch cut, applied at a pinned
# head with strict base hashes and fuzz 0:
#   PR 58439 + stacked 58835  checkpoint-mapped (mmap) PLE table with readahead
# (head 047fc1c, still open upstream; re-anchored byte-clean onto v0.32.0.)
COPY patches/port-ple-mmap.py patches/ple-mmap-pr58439-58835-v0.32.patch patches/ple-mmap-base-hashes.json /tmp/ple-mmap/
RUN python3 /tmp/ple-mmap/port-ple-mmap.py /usr/local/lib/python3.12/dist-packages/vllm
LABEL io.tpurtell.ple-mmap.pr="58439+58835" \
      io.tpurtell.ple-mmap.commit="047fc1c"

# Recipe-local ports, applied after the vendored upstream patches.
# port-nvfp4-ple-ct.py was dropped here: v0.32.0 upstream now routes
# CompressedTensors/INC/Quark/ModelOpt-mixed-precision PLE layers to the
# unquantized method itself (per-layer scheme check; strictly better scoped
# than the recipe port).
# port-mrope-positions-factor.py (the blunt x4 workaround for the 59853
# regression) was replaced 2026-10-19 by port-mrope-served-window.py, the
# exact upstream fix we filed as vllm-project/vllm#62409.
COPY patches/port-host-embedding.py /tmp/port-host-embedding.py
COPY patches/port-vocab-projection.py /tmp/port-vocab-projection.py
COPY patches/port-nvfp4-moe.py /tmp/port-nvfp4-moe.py
COPY patches/port-mrope-served-window.py /tmp/port-mrope-served-window.py
COPY patches/port-exl3-ple-fp8.py /tmp/port-exl3-ple-fp8.py
COPY patches/port-ple-exact-pin.py /tmp/port-ple-exact-pin.py
COPY patches/port-ple-stream.py /tmp/port-ple-stream.py
COPY patches/qwen_ple_stream.py /usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/qwen_ple_stream.py
COPY patches/qwen_stream_gather.py /usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/qwen_stream_gather.py
COPY patches/ple_rowcopy.c /tmp/ple_rowcopy.c
RUN python3 /tmp/port-host-embedding.py /usr/local/lib/python3.12/dist-packages/vllm \
 && python3 /tmp/port-vocab-projection.py /usr/local/lib/python3.12/dist-packages/vllm \
 && python3 /tmp/port-nvfp4-moe.py /usr/local/lib/python3.12/dist-packages/vllm \
 && python3 /tmp/port-mrope-served-window.py /usr/local/lib/python3.12/dist-packages/vllm \
 && python3 /tmp/port-exl3-ple-fp8.py /usr/local/lib/python3.12/dist-packages/vllm \
 && python3 /tmp/port-ple-exact-pin.py /usr/local/lib/python3.12/dist-packages/vllm \
 && mkdir -p /opt/qwen38 && gcc -O3 -shared -fPIC -o /opt/qwen38/ple_rowcopy.so /tmp/ple_rowcopy.c \
 && python3 /tmp/port-ple-stream.py /usr/local/lib/python3.12/dist-packages/vllm \
 && python3 -c 'from vllm.model_executor.layers.quantization import get_quantization_config; assert get_quantization_config("exl3").__name__ == "Exl3Config"' \
 && python3 -c 'import vllm.models.qwen4_exp.nvidia.ple_pageable, vllm.models.qwen4_exp.nvidia.ngram_embedding' \
 && python3 -c 'import os; from vllm.model_executor.layers import qwen_ple_stream as s; assert not s.stream_enabled(); os.environ["PLE_STREAM"]="1"; assert s.stream_enabled(); os.environ["PLE_STREAM"]="0"; from vllm.models.qwen4_exp.nvidia.ngram_embedding import Qwen4ExpPLEStreamHostEmbedding as C; assert C.__mro__[1].__name__ == "Qwen4ExpPLEPageableHostEmbedding", C.__mro__'
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

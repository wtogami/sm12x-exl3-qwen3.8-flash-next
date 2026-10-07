#!/usr/bin/env python3
"""Allow CompressedTensors (NVFP4) checkpoints to load unquantized PLE tables.

CompressedTensors checkpoints (e.g. RedHatAI Qwen3.8-Flash-Next-NVFP4)
quantize Linear targets only; the Qwen4Exp PLE n-gram table is an embedding
and stays BF16. Qwen4ExpPLEEmbeddingMethod.from_quant_config nevertheless
raised NotImplementedError for any non-FP8 quant config, which blocked
startup. Treat the PLE table as unquantized under CompressedTensorsConfig.
"""
from pathlib import Path
import sys

root = Path(sys.argv[1])
path = root / "models/qwen4_exp/common/ngram_embedding.py"
text = path.read_text()

old_import = "from vllm.model_executor.layers.quantization.fp8 import Fp8Config\n"
assert text.count(old_import) == 1
text = text.replace(
    old_import,
    old_import
    + "from vllm.model_executor.layers.quantization.compressed_tensors"
    + ".compressed_tensors import CompressedTensorsConfig\n",
)

old = """        if isinstance(
            quant_config, ModelOptQuantConfigBase
        ) and quant_config.is_layer_excluded(prefix):
            return Qwen4ExpPLEUnquantizedEmbeddingMethod()
        if not isinstance(quant_config, Fp8Config):
"""
new = """        if isinstance(
            quant_config, ModelOptQuantConfigBase
        ) and quant_config.is_layer_excluded(prefix):
            return Qwen4ExpPLEUnquantizedEmbeddingMethod()
        if isinstance(quant_config, CompressedTensorsConfig):
            # CompressedTensors checkpoints (e.g. NVFP4) quantize Linear
            # targets only; the PLE n-gram table is an embedding and stays
            # unquantized.
            return Qwen4ExpPLEUnquantizedEmbeddingMethod()
        if not isinstance(quant_config, Fp8Config):
"""
assert text.count(old) == 1
text = text.replace(old, new)
compile(text, str(path), "exec")
path.write_text(text)
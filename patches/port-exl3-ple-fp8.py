#!/usr/bin/env python3
"""Honor the explicit qflashrt.fp8-ple.v1 annotation in EXL3 hybrids.

The upstream v0.30.0 PLE selector rejects an unknown quant config type, while
EXL3 checkpoints keep the PLE table unquantized (BF16) or annotate one
global-scale FP8 table in the quantization config meta.  Teach
Qwen4ExpPLEEmbeddingMethod.from_quant_config about Exl3Config: unquantized by
default, FP8 only for the annotated module.
"""
from pathlib import Path
import sys

root = Path(sys.argv[1])
path = root / 'model_executor/layers/quantization/exl3.py'
s = path.read_text()
old = '        base_quantization_config: dict[str, Any] | None = None,\n'
assert s.count(old) == 1
s = s.replace(old, old + '        qflashrt_ple: dict[str, Any] | None = None,\n')
old = '        self.bits = bits\n'
assert s.count(old) == 1
s = s.replace(old, old + '        self.qflashrt_ple = qflashrt_ple\n')
old = '            base_quantization_config=config.get("base_quantization_config"),\n'
assert s.count(old) == 1
s = s.replace(old, old + '            qflashrt_ple=config.get("meta", {}).get("qflashrt_ple"),\n')
compile(s, str(path), 'exec')
path.write_text(s)

path = root / 'models/qwen4_exp/nvidia/ngram_embedding.py'
s = path.read_text()
old = '''        if quant_config is None:
            return Qwen4ExpPLEUnquantizedEmbeddingMethod()
'''
assert s.count(old) == 1
s = s.replace(old, old + '''        if type(quant_config).__name__ == "Exl3Config":
            annotation = getattr(quant_config, "qflashrt_ple", None)
            if annotation is None:
                return Qwen4ExpPLEUnquantizedEmbeddingMethod()
            module = annotation.get("module", "")
            expected = {
                "schema": "qflashrt.fp8-ple.v1",
                "quant_algo": "FP8",
                "storage_dtype": "F8_E4M3",
                "scale_dtype": "BF16",
                "arithmetic_transform": False,
                "weight_scale": module + ".weight_scale",
            }
            if not module.startswith("model.language_model.layers.") or any(
                annotation.get(key) != value for key, value in expected.items()
            ):
                raise ValueError("Unsupported EXL3 FP8 PLE annotation")
            tail = module.split("model.language_model.", 1)[1]
            if prefix.endswith(tail):
                return Qwen4ExpPLEFp8EmbeddingMethod()
            return Qwen4ExpPLEUnquantizedEmbeddingMethod()
''')
compile(s, str(path), 'exec')
path.write_text(s)
print('EXL3 annotated PLE preserves global-scale FP8 host storage')

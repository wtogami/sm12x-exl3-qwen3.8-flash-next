#include "quant/reconstruct.cuh"
#include <torch/extension.h>
PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.def("reconstruct", &reconstruct, "reconstruct");
    m.def("reconstruct_slice", &reconstruct_slice, "reconstruct_slice");
    m.def("reconstruct_had_slice", &reconstruct_had_slice, "reconstruct_had_slice");
}

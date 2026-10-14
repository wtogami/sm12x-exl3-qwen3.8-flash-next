import pathlib, re, sys
src = pathlib.Path(sys.argv[1]); s = src.read_text()

# 1) drop the address-taking instance tables (trigger for CUDA13 legacy stubs)
s = re.sub(r"constexpr auto reconstruct[a-z_]*kernel_instances = std::array\s*\{[^}]*\};\s*\n", "", s)

# 2) replace each dispatch+launch block with a direct-id switch
sites = [
  ("reconstruct_kernel_instances", "reconstruct_kernel", "blockDim", 4,
   "half* a0 = (half*) unpacked.data_ptr();\n    const uint16_t* a1 = (const uint16_t*) packed.data_ptr();\n    int a2 = packed_cols;\n    int a3 = packed_n_offset;"),
  ("reconstruct_had_kernel_instances", "reconstruct_had_kernel", "RH_THREADS", 6,
   "half* a0 = (half*) unpacked.data_ptr();\n    const uint16_t* a1 = (const uint16_t*) packed.data_ptr();\n    const half* a2 = (const half*) suh.data_ptr();\n    const half* a3 = (const half*) svh.data_ptr();\n    int a4 = (int) packed.size(1);\n    int a5 = (int) (n_offset / 16);"),
  ("reconstruct_had_batch_kernel_instances", "reconstruct_had_batch_kernel", "blockDim", 6,
   "half* a0 = (half*) unpacked.data_ptr();\n    const uint16_t* const* a1 = (const uint16_t* const*) packed_ptrs.data_ptr();\n    const half* const* a2 = (const half* const*) suh_ptrs.data_ptr();\n    const half* const* a3 = (const half* const*) svh_ptrs.data_ptr();\n    int a4 = n / 16;\n    size_t a5 = (size_t) k * n;"),
  ("reconstruct_batch_kernel_instances", "reconstruct_batch_kernel", "blockDim", 4,
   "half* a0 = (half*) unpacked.data_ptr();\n    const uint16_t* const* a1 = (const uint16_t* const*) packed_ptrs.data_ptr();\n    int a4_dummy_unused = 0; (void) a4_dummy_unused;"),
]
# batch sites reuse original arg tails; handle per-family arg strings explicitly
args_by_family = {
  "reconstruct_kernel": "a0, a1, a2, a3",
  "reconstruct_had_kernel": "a0, a1, a2, a3, a4, a5",
  "reconstruct_had_batch_kernel": "a0, a1, a2, a3, a4, a5",
  "reconstruct_batch_kernel": "a0, a1, n / 16, (size_t) k * n",
}
for tbl, fam, block, nargs, decls in sites:
    i = s.index(f"    TORCH_CHECK(cbi >= 0 && cbi < (int) {tbl}.size(),")
    j = s.index("cuda_check(cudaPeekAtLastError());", i) + len("cuda_check(cudaPeekAtLastError());")
    cases = []
    for cb in range(3):
        for K in range(1, 9):
            idx = cb * 8 + K - 1
            cases.append(f"      case {idx}: {fam}<{K},{cb}><<<gridDim, {block}, 0, stream>>>({args_by_family[fam]}); break;")
    for K in range(1, 4):
        idx = 24 + K - 1
        cases.append(f"      case {idx}: {fam}<{K},2,true><<<gridDim, {block}, 0, stream>>>({args_by_family[fam]}); break;")
    new = (f"    TORCH_CHECK(cbi >= 0 && cbi < 27, \"kernel index out of range: \", cbi);\n"
           f"    {decls}\n"
           f"    switch (cbi) {{\n" + "\n".join(cases) + "\n"
           f"      default: TORCH_CHECK(false, \"bad kernel index\");\n"
           f"    }}\n    cuda_check(cudaGetLastError());")
    s = s[:i] + new + s[j:]
src.write_text(s)
print("patched v2: tables removed, direct-id switches for 4 families")

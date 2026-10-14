import pathlib, sys
src = pathlib.Path(sys.argv[1]); s = src.read_text()
sites = [
  ("reconstruct_fn", "reconstruct_kernel_instances", "blockDim",
   [("(half*) unpacked.data_ptr()","half*"),
    ("(const uint16_t*) packed.data_ptr()","const uint16_t*"),
    ("packed_cols","int"), ("packed_n_offset","int")]),
  ("reconstruct_had_fn", "reconstruct_had_kernel_instances", "RH_THREADS",
   [("(half*) unpacked.data_ptr()","half*"),
    ("(const uint16_t*) packed.data_ptr()","const uint16_t*"),
    ("(const half*) suh.data_ptr()","const half*"),
    ("(const half*) svh.data_ptr()","const half*"),
    ("packed.size(1)","int"), ("(int) (n_offset / 16)","int")]),
  ("reconstruct_had_batch_fn", "reconstruct_had_batch_kernel_instances", "blockDim",
   [("(half*) unpacked.data_ptr()","half*"),
    ("(const uint16_t* const*) packed_ptrs.data_ptr()","const uint16_t* const*"),
    ("(const half* const*) suh_ptrs.data_ptr()","const half* const*"),
    ("(const half* const*) svh_ptrs.data_ptr()","const half* const*"),
    ("n / 16","int"), ("(size_t) k * n","size_t")]),
  ("reconstruct_batch_fn", "reconstruct_batch_kernel_instances", "blockDim",
   [("(half*) unpacked.data_ptr()","half*"),
    ("(const uint16_t* const*) packed_ptrs.data_ptr()","const uint16_t* const*"),
    ("n / 16","int"), ("(size_t) k * n","size_t")]),
]
for fn, var, block, args in sites:
    hdr = f"    auto {fn} = {var}[cbi];"
    i = s.index(hdr)
    j = s.index(");", s.index("<<<", i)) + 2
    decls = "\n".join(f"    {ty} a{i2} = {expr};" for i2, (expr, ty) in enumerate(args))
    argarr = ",".join(f"&a{i2}" for i2 in range(len(args)))
    new = (f"{hdr}\n{decls}\n    void* args[] = {{{argarr}}};\n"
           f"    cudaLaunchKernel((const void*){fn}, gridDim, {block}, args, 0, stream);")
    s = s[:i] + new + s[j:]
src.write_text(s)
print("patched 4 indirect launches -> cudaLaunchKernel")

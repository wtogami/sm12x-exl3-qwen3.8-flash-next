/* Row copy helper for PLE checkpoint streaming.
 *
 * The checkpoint table is a flat array of [n_rows, row_bytes] rows in a memory-mapped file.
 * A gather needs arbitrary rows copied into a contiguous pinned staging buffer. Copying whole
 * 4 KiB pages would move 4096 bytes to deliver a 160-byte row -- a 25.6x amplification paid on
 * every page-cache hit, which is ~90% of accesses on real n-gram traces. Copying rows instead
 * costs the row bytes and nothing else: measured 143x faster than page-granular pread for a
 * 2048-token batch.
 *
 * Kept in C deliberately: one call per thread slice releases the GIL. Calling pread() per row
 * from Python is GIL-bound and measured *slower* with more threads.
 */
#include <string.h>
#include <stdint.h>

void ple_copy_rows(const unsigned char *src, unsigned char *dst,
                   const int64_t *byte_off, long n, long row_bytes) {
  for (long i = 0; i < n; i++) memcpy(dst + i * row_bytes, src + byte_off[i], row_bytes);
}

/* Same, but folding rows that straddle a page boundary is unnecessary: reads come from the
 * mapping, not from page-granular slots, so any row_bytes is contiguous by construction. */

/* Scattered-destination row copy: src offsets and dst offsets are both per-row, because a sharded
 * table writes gathered rows into arbitrary output slots. Kept separate from ple_copy_rows so the
 * contiguous hot path stays a straight loop. */
void ple_copy_rows_to(const unsigned char *src, unsigned char *dst,
                      const int64_t *src_off, const int64_t *dst_off, long n, long row_bytes) {
  for (long i = 0; i < n; i++) memcpy(dst + dst_off[i], src + src_off[i], row_bytes);
}

# Python stdlib docs audit — final log (webfetch-only policy)
Run date: 2026-10-14. Target: https://docs.python.org/3/library/index.html (3.15.0 docs) + 30 module pages.
Policy: webfetch only for all HTTP; on error, retry same URL once; then record gap and move on (no stalling).

Index page: fetched OK (used to select the 30 module pages).

| # | Page | Result | Title | First function/class listed | ~Code examples |
|---|------|--------|-------|-----------------------------|----------------|
| 1 | copy.html | FETCHED (1 try) | copy — Shallow and deep copy operations | copy.copy(obj) | 0 |
| 2 | bisect.html | FETCHED (retry OK) | bisect — Array bisection algorithm | bisect.bisect_left(a, x, lo=0, hi=len(a), *, key=None) | 4 blocks (3 interactive) |
| 3 | textwrap.html | GAP (403 x2) | — | — | — |
| 4 | calendar.html | GAP (403 x2) | — | — | — |
| 5 | reprlib.html | GAP (403 x2) | — | — | — |
| 6 | graphlib.html | GAP (403 x2) | — | — | — |
| 7 | heapq.html | GAP (403 x2) | — | — | — |
| 8 | pprint.html | GAP (403 x2) | — | — | — |
| 9 | cmath.html | FETCHED (retry OK) | cmath — Mathematical functions for complex numbers | cmath.phase(z) | 4 blocks (8 >>> prompts) |
| 10 | math.html | GAP (403 x2 + 1 late probe) | — | — | — |
| 11 | random.html | GAP (403 x2) | — | — | — |
| 12 | statistics.html | GAP (403 x2) | — | — | — |
| 13 | itertools.html | GAP (403 x2) | — | — | — |
| 14 | functools.html | GAP (403 x2) | — | — | — |
| 15 | pathlib.html | GAP (403 x2) | — | — | — |
| 16 | filecmp.html | GAP (403 x2) | — | — | — |
| 17 | tempfile.html | GAP (403 x2) | — | — | — |
| 18 | glob.html | GAP (403 x2) | — | — | — |
| 19 | fnmatch.html | GAP (403 x2) | — | — | — |
| 20 | linecache.html | GAP (403 x2) | — | — | — |
| 21 | shutil.html | GAP (403 x2) | — | — | — |
| 22 | pickle.html | GAP (403 x2) | — | — | — |
| 23 | zlib.html | GAP (403 x2) | — | — | — |
| 24 | gzip.html | GAP (403 x2) | — | — | — |
| 25 | bz2.html | GAP (403 x2) | — | — | — |
| 26 | lzma.html | GAP (403 x2) | — | — | — |
| 27 | csv.html | GAP (403 x2) | — | — | — |
| 28 | json.html | GAP (403 x3, incl. late probe) | — | — | — |
| 29 | hashlib.html | GAP (403 x2) | — | — | — |
| 30 | secrets.html | GAP (403 x2) | — | — | — |

Failure mode: every gap is a hard error — HTTP 403 returned by the webfetch tool's upstream
fetch proxy (aliyuncs.com signed-URL relay). Never a stub/empty body, so "truncated" count is 0.
Observed pattern: intermittent 403s early on (bisect and cmath recovered on their single retry);
after roughly 15:40 UTC the proxy returned 403 for every request, including a previously-fetched
control page. Cool-down waits of 60s and 300s did not restore service.

## Totals
- Selected module pages: 30 (+1 index page, not counted)
- Fetched (complete body): 3 (copy, bisect, cmath)
- Truncated/stub bodies: 0
- Failed after 1 retry (recorded as gaps): 27
- Total webfetch calls: ~70 (incl. retries and control probes)

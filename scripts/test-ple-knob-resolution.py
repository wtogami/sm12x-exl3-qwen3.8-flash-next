#!/usr/bin/env python3
"""Resolve the single streaming knob through start.sh's forwarding, offline.

Harness comes from test-container-lifecycle.py: sandbox() builds a fake docker that logs its argv, run()
invokes start.sh against it and returns the logged lines. Importing runs that suite first, which is a
useful extra gate. Checks are on the exact `-e` pairs that reach the container plus the launcher-side
graph flags in the logged run line. Every check is capable of failing.
"""
import importlib.util
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]   # same convention as the other offline tests
spec = importlib.util.spec_from_file_location('lc', str(root / 'scripts/test-container-lifecycle.py'))
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)          # runs its own asserts first

fails = 0


def main():
    global fails
    with tempfile.TemporaryDirectory() as folder:
        base = lc.sandbox(Path(folder))

        def check(label, pairs, ins=(), nots=(), over=None):
            global fails
            out, calls = lc.run('start.sh', {**base, **(over or {})})
            run = next((c for c in calls if c.startswith('run ')), '')
            ok = out.returncode == 0 and all(f'-e {kv}' in run for kv in pairs) \
                 and all(n in run for n in ins) and all(n not in run for n in nots)
            print(('PASS ' if ok else 'FAIL ') + label)
            if not ok:
                fails += 1
                print('   rc=%s pairs=%s' % (out.returncode, pairs))
                print('   run:', run[:700])
                print('   stderr:', out.stderr[:300])

        # default: pinned reads, nothing streamed, graphs untouched
        check('unset -> pair 0/0',
              ('PLE_STREAM=0', 'PLE_STREAM_V2=0'), (), ('PIECEWISE',))
        # the single simple switch: staged design, FULL_AND_PIECEWISE kept as is
        check('PLE_STREAM=1 -> pair 1/1, graphs untouched',
              ('PLE_STREAM=1', 'PLE_STREAM_V2=1'), (), ('PIECEWISE',),
              {'PLE_STREAM': '1'})
        # legacy spelling means the same thing now
        check('PLE_STREAM_V2=1 alone -> pair 1/1',
              ('PLE_STREAM=1', 'PLE_STREAM_V2=1'), (), ('PIECEWISE',),
              {'PLE_STREAM_V2': '1'})
        # diagnostic arm: old v1 combo, forced regime visible in the run line
        check('PLE_STREAM=piecewise -> pair 1/0 + PIECEWISE + breakable',
              ('PLE_STREAM=1', 'PLE_STREAM_V2=0', 'VLLM_USE_BREAKABLE_CUDAGRAPH=1'),
              ('PIECEWISE',), (), {'PLE_STREAM': 'piecewise'})
        # explicit pair honoured verbatim, as the historical drivers send it
        check('explicit STREAM=1 V2=0 -> forcing applies',
              ('PLE_STREAM=1', 'PLE_STREAM_V2=0', 'VLLM_USE_BREAKABLE_CUDAGRAPH=1'),
              ('PIECEWISE',), (),
              {'PLE_STREAM': '1', 'PLE_STREAM_V2': '0'})


main()
print('fails=%d' % fails)
raise SystemExit(0 if fails == 0 else 1)

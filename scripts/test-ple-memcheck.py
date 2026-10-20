#!/usr/bin/env python3
"""Check the PLE pinned-memory preflight: arithmetic, verdicts, and skip paths."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
# Geometry taken from the real EXL3 checkpoints.
NGRAM_ROWS, PLE_DIM, VOCAB, HIDDEN = 20_000_000, 2560, 248_320, 2560
TABLE_FP8_GIB = NGRAM_ROWS * PLE_DIM * 1 / 2**30        # 47.68
TABLE_BF16_GIB = NGRAM_ROWS * PLE_DIM * 2 / 2**30       # 95.40
EMBED_GIB = VOCAB * HIDDEN * 2 / 2**30                  # 1.18
# What torch.empty(pin_memory=True) really asks the kernel for: every pinned
# allocation is rounded up to a power of two, unconditionally.
def pinned(gib: float) -> float:
    import math
    return 2.0 ** math.ceil(math.log2(gib))
BF16_PIN_GIB = pinned(TABLE_BF16_GIB)                   # 128
FP8_PIN_GIB = pinned(TABLE_FP8_GIB)                     # 64
EMBED_PIN_GIB = pinned(EMBED_GIB)                       # 2
DEMAND_BF16_GIB = BF16_PIN_GIB + EMBED_PIN_GIB          # 130
DEMAND_FP8_GIB = FP8_PIN_GIB + EMBED_PIN_GIB            # 66
KIB = 1024 * 1024  # meminfo is in KiB; this value is 1 GiB in KiB


def config(path: Path, **overrides) -> Path:
    text = {'ngram_vocab_size_base': NGRAM_ROWS, 'ple_embed_dim': PLE_DIM,
            'vocab_size': VOCAB, 'hidden_size': HIDDEN}
    text.update(overrides)
    path.write_text(json.dumps({'text_config': {k: v for k, v in text.items()
                                                if v is not None}}))
    return path


def check(cfg: Path, bytes_per_element: int, memtotal_gib: float, memavail_gib: float,
          strategy: str | None = None):
    script = ('source "%s/platform-config.sh" && qwen38_ple_memcheck "%s" %d %d %d'
              % (root, cfg, bytes_per_element, int(memtotal_gib * KIB), int(memavail_gib * KIB)))
    if strategy is not None:
        script = 'export PLE_PIN_STRATEGY=%s; ' % strategy + script
    return subprocess.run(['/bin/bash', '-c', script], capture_output=True, text=True)


results = []
with tempfile.TemporaryDirectory() as folder:
    cfg = config(Path(folder) / 'config.json')

    # gg cannot serve the BF16 table; the preflight must refuse it there. This is
    # the real outcome the threshold was anchored against, not a synthetic case.
    refused = check(cfg, 2, 125.68, 121.0)
    assert refused.returncode != 0, 'BF16 on a 125.68 GiB host must refuse'
    assert '%.1f' % TABLE_BF16_GIB in refused.stderr, refused.stderr
    assert 'exl3-ple8' in refused.stderr and 'QWEN38_SKIP_MEMCHECK' in refused.stderr
    # It must refuse for the right reason: 95.4 GiB of checkpoint is 128 GiB of
    # pin, so the host is short by 130 against 125.68, not 95.4 against 69.
    assert '%.1f GiB' % BF16_PIN_GIB in refused.stderr, refused.stderr
    assert '%.1f GiB' % DEMAND_BF16_GIB in refused.stderr, refused.stderr
    # The 183 GiB qualification host did serve it, at 130/183 = 71.0% of RAM
    # pinned: must not refuse, but must warn, since that is the highest share
    # with a recorded successful pin and anything above it is untested.
    allowed = check(cfg, 2, 183.0, 180.0)
    assert allowed.returncode == 0, 'BF16 on the qualification host must be allowed'
    assert 'WARNING' in allowed.stderr, '71.0% of RAM pinned sits in the warn band'
    # Same profile on a generously sized host: allowed and quiet.
    quiet = check(cfg, 2, 995.6, 900.0)
    assert quiet.returncode == 0 and 'WARNING' not in quiet.stderr
    # FP8 halves the table, which is exactly why it fits where BF16 does not.
    fp8 = check(cfg, 1, 125.68, 121.0)
    assert fp8.returncode == 0 and '%.1f' % TABLE_FP8_GIB in fp8.stderr
    assert 'WARNING' not in fp8.stderr, 'FP8 pinning 66 of 125.68 GiB should not warn'
    assert '%.1f GiB pins %.1f GiB' % (TABLE_FP8_GIB, FP8_PIN_GIB) in fp8.stderr, fp8.stderr

    # Regression: the one case the old 55%-of-installed-RAM gate got wrong. A
    # table of exactly 65 GiB is 52.7% of a 125.68 GiB host, so the old bracket
    # passed it, and it cannot possibly start -- 65 GiB is one GiB past a power
    # of two, so the pin rounds to 128 GiB and the demand is 130 GiB.
    just_over = config(Path(folder) / 'just_over.json',
                       ngram_vocab_size_base=int(65 * 2**30 / PLE_DIM / 1))
    rounded = check(just_over, 1, 125.68, 121.0)
    assert rounded.returncode != 0, (
        'a 65 GiB table pins 128 GiB and must be refused on 125.68 GiB; the old '
        'derived-vs-55% rule passed this: %s' % rounded.stderr)
    assert '128.0 GiB' in rounded.stderr, rounded.stderr
    # The false positive found on the real host: the FP8 profile is serving on
    # gg right now, which is exactly why MemAvailable reads 48.2 GiB. A snapshot
    # of used memory must warn, never refuse a profile installed RAM allows.
    pressured = check(cfg, 1, 125.68, 48.2)
    assert pressured.returncode == 0, 'low MemAvailable must not refuse a profile RAM allows'
    assert 'WARNING' in pressured.stderr and 'available' in pressured.stderr
    # Reported figures must match the checkpoint geometry.
    assert '%.1f' % (TABLE_FP8_GIB + EMBED_GIB) in fp8.stderr, fp8.stderr

    # Unparseable or PLE-less checkpoints must skip rather than guess.
    empty = Path(folder) / 'empty.json'
    empty.write_text('{}')
    skipped = check(empty, 1, 125.68, 121.0)
    assert skipped.returncode == 0 and 'skipping' in skipped.stderr
    missing = check(config(Path(folder) / 'missing.json'), 1, 125.68, 121.0)
    assert missing.returncode == 0
    absent = check(Path(folder) / 'nope.json', 1, 125.68, 121.0)
    assert absent.returncode == 0 and 'skipping' in absent.stderr
    # PLE_PIN_STRATEGY has to model what the loader actually does. round2 is torch's
    # pin_memory path (rounds up); exact page-locks the true size through the driver.
    bf16_exact = check(cfg, 2, 125.68, 49.0, strategy='exact')
    assert refused.returncode != 0, 'round2 must still refuse BF16 on gg'
    assert bf16_exact.returncode == 0, 'exact must flip that same verdict: 95.4+2 fits'
    assert '%.1f GiB pins %.1f GiB' % (TABLE_BF16_GIB, TABLE_BF16_GIB) in bf16_exact.stderr
    demand_exact = TABLE_BF16_GIB + EMBED_PIN_GIB
    assert '%.1f' % demand_exact in bf16_exact.stderr, bf16_exact.stderr
    assert 'WARNING' in bf16_exact.stderr, 'exact still pins 77.5%% of the host: warn band'
    assert 'only honoured by a build carrying' in bf16_exact.stderr, \
        'exact must warn that an image without the port silently ignores the variable'
    assert 'as 128.0 GiB' in bf16_exact.stderr, \
        'the fallback figure in that warning must be the rounded size the broken loader would pin'
    fp8_exact = check(cfg, 1, 125.68, 121.0, strategy='exact')
    assert fp8_exact.returncode == 0 and 'WARNING' not in fp8_exact.stderr, \
        'FP8 exact pins 49.7 of 125.68 GiB; neither refusal nor warning'
    assert 'as 64.0 GiB' in fp8_exact.stderr, 'same fallback figure, FP8 sizes'
    tiny_exact = check(cfg, 2, 64.0, 60.0, strategy='exact')
    assert tiny_exact.returncode != 0, 'exact must still refuse BF16 on a 64 GiB host'
    bogus = check(cfg, 1, 125.68, 121.0, strategy='nonsense')
    assert bogus.returncode == 0 and 'strategy round2' in bogus.stderr, \
        'an unrecognised strategy falls back to the conservative rounding model'
    results.append({'pin_strategy_exact_flips_verdict': True,
                    'pin_strategy_warns_when_unsupported': True,
                    'pin_strategy_still_refuses_when_genuinely_too_small': True,
                    'pin_strategy_unknown_value_is_conservative': True})
    results.append({'refuses_bf16_on_small_host': True, 'allows_bf16_on_qualification_host': True,
                    'allows_fp8_where_bf16_refused': True, 'memory_pressure_warns_not_refuses': True,
                    'unparseable_skips': True, 'passed': True})

# Skip paths in start.sh: a checkpoint-mapped table pins nothing, and a caller
# supplied --engram-config means the placement is unknown.
with tempfile.TemporaryDirectory() as folder:
    bin_dir = Path(folder) / 'bin'
    bin_dir.mkdir()
    fake = bin_dir / 'docker'
    # inspect must fail the way a real engine fails when no container of that
    # name exists; echoing argv would report a phantom predecessor and start.sh
    # would refuse its own run.
    fake.write_text('#!/bin/sh\nif [ "$1" = "inspect" ]; then exit 1; fi\n'
                    'if [ "$1" = "stop" ] || [ "$1" = "rm" ]; then exit 0; fi\n'
                    'printf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    cache = Path(folder) / 'hf'
    revision = '73a050c27b8c488c65acd6d1c74e45ff02be5fab'
    snap = cache / 'hub' / 'models--wrldsuksgo2mars--Qwen3.8-Flash-Next-EXL3-K4.25-v1' / 'snapshots' / revision
    snap.mkdir(parents=True)
    config(snap / 'config.json')
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'HF_CACHE': str(cache),
           'RUNTIME_CACHE': str(Path(folder) / 'runtime'), 'HOME': str(Path(folder) / 'home')}
    for key in ('IMAGE', 'PLE_MMAP', 'VLLM_PLE_MMAP', 'MTP_TOKENS', 'GPU', 'ENFORCE_EAGER'):
        env.pop(key, None)
    for label, override, argv in (
            ('checkpoint-mapped', {'PLE_MMAP': '1'}, []),
            ('custom engram-config', {}, ['--engram-config', '{"cpu_offload": true}']),
            ('explicit skip', {'QWEN38_SKIP_MEMCHECK': '1'}, [])):
        out = subprocess.run(['/bin/bash', str(root / 'start.sh'), *argv], env={**env, **override},
                             capture_output=True, text=True)
        assert out.returncode == 0, (label, out.stderr)
        assert 'pinned host memory' not in out.stderr, '%s must skip the preflight' % label
    enabled = subprocess.run(['/bin/bash', str(root / 'start.sh')], env=env,
                             capture_output=True, text=True)
    assert 'pinned host memory' in enabled.stderr, 'resident-table start must run the preflight'
    results.append({'skips_on_checkpoint_mapped': True, 'skips_on_custom_engram_config': True,
                    'skips_on_override': True, 'runs_by_default': True, 'passed': True})

for line in results:
    print(json.dumps(line))

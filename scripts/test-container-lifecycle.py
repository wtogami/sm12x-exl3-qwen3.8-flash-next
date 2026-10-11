#!/usr/bin/env python3
"""Check the container lifecycle: stopped predecessors, refusal to cut over a live
one, and the stop grace period.

Everything here runs against a fake docker that appends its argv to a log and
answers `inspect` from FAKE_STATE, so no engine and no container are involved.
The docker (not podman) shim is modelled because the name and state handling under
test is identical on both engines, and it keeps the CDI path out of the picture.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
REVISION = '888306bd3996d6317758c07df50622829259ad17'      # QUANT=exl3-ple8
CACHE = 'models--wrldsuksgo2mars--Qwen3.8-Flash-Next-EXL3-K4.25-PLE-FP8-v1'
NAME = 'qwen38-exl3-ple8'

FAKE_DOCKER = r'''#!/bin/sh
if [ "$1" = "--version" ]; then printf "Docker version 27.5.1, build 9f9e405\n"; exit 0; fi
printf "%s\n" "$*" >> "$FAKE_LOG"
case "$1" in
  inspect)
    if [ -n "${FAKE_STATE:-}" ]; then printf "%s\n" "$FAKE_STATE"; exit 0; fi
    printf "Error: no such container\n" >&2; exit 1 ;;
  ps) exit 0 ;;
  stop|rm) printf "%s\n" "$2" ;;
  run) printf 'e0d0ba7d1f2a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4\n' ;;
esac
exit 0
'''


def sandbox(tmp: Path) -> dict:
    bin_dir = tmp / 'bin'
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / 'docker'
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)
    (bin_dir / 'uname').write_text('#!/bin/sh\nprintf "x86_64\\n"\n')
    (bin_dir / 'uname').chmod(0o755)
    core = tmp / 'core'
    core.mkdir(exist_ok=True)
    for name in ('cat', 'cut', 'dirname', 'grep', 'head', 'id', 'mkdir', 'mktemp', 'mv',
                 'rm', 'sed', 'sha256sum', 'sort', 'touch', 'paste'):
        target = shutil.which(name)
        if target:
            try:
                os.symlink(target, core / name)
            except FileExistsError:
                pass
    snap = tmp / 'hf' / 'hub' / CACHE / 'snapshots' / REVISION
    snap.mkdir(parents=True)
    # Geometry only has to parse; the memory gate is skipped below so the
    # lifecycle behaviour is what is under test.
    (snap / 'config.json').write_text(json.dumps({'text_config': {
        'ngram_vocab_size_base': 20_000_000, 'ple_embed_dim': 2560,
        'vocab_size': 248_320, 'hidden_size': 2560}}))
    base = {k: v for k, v in os.environ.items()
            if k not in ('XDG_CONFIG_HOME', 'IMAGE', 'HF_CACHE', 'RUNTIME_CACHE', 'HOME',
                         'QUANT', 'CONTAINER_NAME', 'REPLACE', 'STOP_TIMEOUT',
                         'QWEN38_STOP_TIMEOUT', 'QWEN38_SKIP_MEMCHECK', 'FAKE_STATE', 'FAKE_LOG')}
    (tmp / 'home').mkdir(exist_ok=True)
    return base | {'PATH': f'{bin_dir}:{core}', 'HOME': str(tmp / 'home'),
                   'HF_CACHE': str(tmp / 'hf'), 'RUNTIME_CACHE': str(tmp / 'runtime'),
                   'QUANT': 'exl3-ple8', 'IMAGE': 'fake:local',
                   'QWEN38_SKIP_MEMCHECK': '1', 'FAKE_LOG': str(tmp / 'calls.log')}


def run(script: str, env: dict, state: str | None = None):
    log = Path(env['FAKE_LOG'])
    log.write_text('')
    calls = {k: v for k, v in env.items() if k.startswith('FAKE_')}
    calls['FAKE_STATE'] = state or ''
    out = subprocess.run(['/bin/bash', str(root / script)], env={**env, **calls},
                         capture_output=True, text=True)
    lines = log.read_text().splitlines()
    invocations = [line for line in lines if line.startswith(('run ', 'stop ', 'rm ', 'inspect ', 'ps '))]
    return out, invocations


results = []
with tempfile.TemporaryDirectory() as folder:
    tmp = Path(folder)
    env = sandbox(tmp)

    # 1. Nothing there: a plain start, no destructive calls.
    out, calls = run('start.sh', env)
    assert out.returncode == 0, out.stderr
    assert any(c.startswith('run ') and f'--name {NAME}' in c for c in calls), calls
    assert not any(c.startswith(('rm ', 'stop ')) for c in calls), calls
    fresh_ok = True

    # 2/3. A dead predecessor owns the name. Both engines reject the run in that
    # state, so start.sh has to clean it up or stop.sh -> start.sh never works.
    removed = []
    for state in ('exited', 'created', 'dead'):
        out, calls = run('start.sh', env, state)
        assert out.returncode == 0, (state, out.stderr)
        assert f'rm {NAME}' in calls, (state, calls)
        assert any(c.startswith('run ') for c in calls), (state, calls)
        assert f'Removing {state} container' in out.stderr, (state, out.stderr)
        assert not any(c.startswith('stop ') for c in calls), 'a dead container has nothing to stop'
        removed.append(state)

    # 4. A live predecessor is refused, and nothing at all is launched or killed.
    for state in ('running', 'restarting', 'paused'):
        out, calls = run('start.sh', env, state)
        assert out.returncode != 0, f'{state} predecessor must not be replaced silently'
        assert 'REPLACE=1' in out.stderr and './stop.sh' in out.stderr, (state, out.stderr)
        assert not any(c.startswith('run ') for c in calls), f'{state}: refused start must not run'
        assert not any(c.startswith('rm ') for c in calls), f'{state}: refused start must not remove'
        assert not any(c.startswith('stop ') for c in calls), f'{state}: refusal must not kill it either'

    # 5. REPLACE=1 does the handoff, and does it with the grace period -- the
    # point of not using --replace is that --replace would use the 10s default.
    out, calls = run('start.sh', {**env, 'REPLACE': '1'}, 'running')
    assert out.returncode == 0, out.stderr
    stop_calls = [c for c in calls if c.startswith('stop ')]
    assert stop_calls and '--time' in stop_calls[0], (stop_calls, out.stderr)
    default_grace = stop_calls[0].split('--time')[1].split()[0]
    assert f'rm {NAME}' in calls and any(c.startswith('run ') for c in calls), calls
    assert default_grace != '10', 'replacing must not inherit the too-short default grace'

    out, calls = run('start.sh', {**env, 'REPLACE': '1', 'STOP_TIMEOUT': '137'}, 'running')
    stop_calls = [c for c in calls if c.startswith('stop ')]
    assert '137' in stop_calls[0], stop_calls
    # One knob, both scripts: STOP_TIMEOUT must reach the REPLACE=1 handoff too.
    out, calls = run('start.sh', {**env, 'REPLACE': '1', 'QWEN38_STOP_TIMEOUT': '137'}, 'running')
    stop_calls = [c for c in calls if c.startswith('stop ')]
    assert '137' in stop_calls[0], stop_calls

    # 6/7. stop.sh: grace period honoured, override honoured, missing container
    # explained rather than dumped as an engine error.
    out, calls = run('stop.sh', env, 'running')
    assert out.returncode == 0, out.stderr
    stop_calls = [c for c in calls if c.startswith('stop ')]
    assert stop_calls and '--time' in stop_calls[0], calls
    assert NAME in stop_calls[0], calls
    assert stop_calls[0].split('--time')[1].split()[0] == default_grace, calls
    out, calls = run('stop.sh', {**env, 'STOP_TIMEOUT': '7'}, 'running')
    stop_calls = [c for c in calls if c.startswith('stop ')]
    assert stop_calls[0].split('--time')[1].split()[0] == '7', calls
    out, calls = run('stop.sh', env, state=None)
    assert out.returncode != 0, 'stopping a name that does not exist must not look like success'
    assert 'QUANT' in out.stderr, out.stderr
    assert not any(c.startswith('stop ') for c in calls), 'must not call stop on a missing container'

    # 8. The design decision itself: nobody should "simplify" this back to
    # --replace, which stops with the runtime default and cuts over silently.
    # Checked against executable lines only -- the comments argue against the
    # flag by name, and that prose is the reason the guard exists.
    code = '\n'.join(line for line in (root / 'start.sh').read_text().splitlines()
                     if not line.lstrip().startswith('#'))
    assert '--replace' not in code, 'start.sh must not use --replace'
    assert not any('--replace' in c for c in calls if c.startswith('run ')), calls
    results.append({'clean_start': fresh_ok, 'dead_predecessor_removed': removed,
                    'live_predecessor_refused': ['running', 'restarting', 'paused'],
                    'replace_uses_grace_period': default_grace,
                    'stop_honours_grace_and_override': True,
                    'stop_missing_container_explained': True,
                    'no_replace_flag': True, 'passed': True})

for line in results:
    print(json.dumps(line))

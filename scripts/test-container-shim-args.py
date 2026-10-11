#!/usr/bin/env python3
"""Check the docker-vs-podman argument split and CDI provisioning, no container launched."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
OLD, NEW = '615.71.09', '620.11.01'

# Models a real spec: a soname that survives upgrades, versioned libraries and
# firmware that do not, a device node, and a library the toolkit only discovers
# once the driver install provides it. Paths resolve into the sandbox so
# satisfiability can be exercised.
CDI_SCRIPT = """#!/bin/sh
for arg in "$@"; do case "$arg" in --output=*) out="${arg#--output=}";; esac; done
# nvidia-ctk rewrites an --output path whose extension it does not recognise
# rather than writing where it was told; any caller must expect this.
case "$out" in *.yaml|*.json) ;; *) out="$out.yaml";; esac
{
  printf 'cdiVersion: 0.7.0\\nkind: nvidia.com/gpu\\ndevices:\\n  - name: all\\n  - name: 0\\n'
  printf '    containerEdits:\\n      deviceNodes:\\n        - path: /dev/null\\n      mounts:\\n'
  printf '        - hostPath: DRIVER_ROOT/lib/libcuda.so.1\\n          containerPath: /usr/lib64/libcuda.so.1\\n'
  printf '        - hostPath: DRIVER_ROOT/lib/libnvidia-ml.so.VER\\n          containerPath: /usr/lib64/libnvidia-ml.so.VER\\n'
  printf '        - hostPath: DRIVER_ROOT/firmware/VER/gsp.bin\\n          containerPath: /lib/firmware/gsp.bin\\n'
  if [ -e DRIVER_ROOT/lib/libnvidia-new.so ]; then
    printf '        - hostPath: DRIVER_ROOT/lib/libnvidia-new.so\\n          containerPath: /usr/lib64/libnvidia-new.so\\n'
  fi
} > "$out"
"""


def install_driver(tmp: Path, version: str, keep_old: bool = False) -> None:
    """Put the files that driver version claims into place, as a package install would."""
    lib = tmp / 'driver' / 'lib'
    lib.mkdir(parents=True, exist_ok=True)
    (lib / 'libcuda.so.1').write_text('')            # soname, survives every upgrade
    (lib / f'libnvidia-ml.so.{version}').write_text('')
    firmware = tmp / 'driver' / 'firmware' / version
    firmware.mkdir(parents=True, exist_ok=True)
    (firmware / 'gsp.bin').write_text('')
    if keep_old:
        return
    # dnf update removes the superseded versioned files and keeps the soname.
    for path in lib.glob('libnvidia-ml.so.*'):
        if path.name != f'libnvidia-ml.so.{version}':
            path.unlink()
    for folder in (tmp / 'driver' / 'firmware').iterdir():
        if folder.is_dir() and folder.name != version:
            shutil.rmtree(folder)


def sandbox(tmp: Path, shim: str, driver: str | None, hermetic: bool = True) -> dict:
    """Fake docker echoing its argv, plus uname and the NVIDIA toolkit binaries.

    hermetic drops the real PATH so a toolkit installed on the test host cannot
    stand in for the one under test.
    """
    bin_dir = tmp / 'bin'
    bin_dir.mkdir(parents=True, exist_ok=True)
    version = 'podman version 5.8.7' if shim == 'podman' else 'Docker version 27.5.1'
    scripts = {'docker': f'if [ "$1" = "--version" ]; then printf "{version}\\n"; exit 0; fi\n'
                         'if [ "$1" = "inspect" ]; then\n'
                         '  if [ -n "${FAKE_STATE:-}" ]; then printf "%s\\n" "$FAKE_STATE"; exit 0; fi\n'
                         '  printf "Error: no such container\\n" >&2; exit 1\n'
                         'fi\n'
                         'if [ "$1" = "stop" ] || [ "$1" = "rm" ]; then exit 0; fi\n'
                         'printf "%s\\n" "$@"',
               'uname': 'printf "%s\\n" "$TEST_ARCH"'}
    if driver:
        scripts['nvidia-smi'] = f'printf "{driver}\\n"'
        scripts['nvidia-ctk'] = (CDI_SCRIPT.replace('DRIVER_ROOT', str(tmp / 'driver'))
                                 .replace('VER', driver))
        scripts['nvidia-cdi-hook'] = 'exit 0'
    for name, body in scripts.items():
        path = bin_dir / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)
    if hermetic:
        core = tmp / 'core'
        core.mkdir(exist_ok=True)
        for name in ('cat', 'cut', 'dirname', 'grep', 'head', 'id', 'mkdir', 'mktemp', 'mv',
                     'rm', 'sed', 'sha256sum', 'sort', 'touch'):
            try:
                os.symlink(shutil.which(name), core / name)
            except FileExistsError:
                pass  # sandbox() runs again against one tmp to model an upgrade
        path = f'{bin_dir}:{core}'
    else:
        path = f'{bin_dir}:{os.environ["PATH"]}'
    home = tmp / 'home'
    home.mkdir(parents=True, exist_ok=True)
    base = {key: value for key, value in os.environ.items()
            if key not in ('XDG_CONFIG_HOME', 'IMAGE', 'QUANT', 'HF_TOKEN', 'HOME', 'RUNTIME_CACHE',
                           'PLE_MMAP', 'VLLM_PLE_MMAP', 'MTP_TOKENS', 'LONGCTX', 'GPU', 'ENFORCE_EAGER')}
    return base | {'PATH': path, 'HOME': str(home), 'TEST_ARCH': 'x86_64',
                   'HF_CACHE': str(tmp / 'hf'), 'RUNTIME_CACHE': str(tmp / 'runtime')}


def run(script: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(['/bin/bash', str(root / script)], env=env, capture_output=True, text=True)


def ok(script: str, env: dict) -> str:
    result = run(script, env)
    assert result.returncode == 0, (script, result.stdout, result.stderr)
    return result.stdout + result.stderr


def checkpoint(env: dict) -> None:
    """start.sh refuses to launch without an unpacked checkpoint on the host."""
    folder = (Path(env['HF_CACHE']) / 'hub'
              / 'models--wrldsuksgo2mars--Qwen3.8-Flash-Next-EXL3-K4.25-v1'
              / 'snapshots' / '73a050c27b8c488c65acd6d1c74e45ff02be5fab')
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'config.json').write_text('{}')


def spec_of(env: dict) -> Path:
    return Path(env['HOME']) / '.config' / 'cdi' / 'nvidia.yaml'


results = []
for shim in ('docker', 'podman'):
    with tempfile.TemporaryDirectory() as folder:
        tmp = Path(folder) / shim
        install_driver(tmp, OLD)
        env = sandbox(tmp, shim, OLD)
        checkpoint(env)
        download = ok('download.sh', env).splitlines()
        assert f'{tmp}/hf:/hf:z' in download, 'the cache mount must be relabelled with :z'
        assert ('--userns=keep-id' in download) == (shim == 'podman')
        assert 'HF_TOKEN' not in download
        assert 'HF_TOKEN' in ok('download.sh', env | {'HF_TOKEN': 'hf_secret'}).splitlines()
        # start.sh runs first so a bare pull.sh deployment is covered: it has to
        # reach the GPU without build.sh having provisioned anything.
        start = ok('start.sh', env).splitlines()
        assert f'{tmp}/runtime:/root/.cache:z' in start, 'the runtime cache mount must be relabelled'
        assert f'{tmp}/hf:/root/.cache/huggingface:ro,z' in start, ':Z would isolate it from download.sh'
        assert ('--gpus' in start) == (shim == 'docker'), 'podman rejects the docker device= syntax'
        assert ('nvidia.com/gpu=all' in start) == (shim == 'podman')
        assert 'NVIDIA_VISIBLE_DEVICES=all' in start or shim == 'docker'
        assert 'CUTE_DSL_ARCH=sm_120a' in ok('build.sh', env)
        spec = spec_of(env)
        assert spec.is_file() == (shim == 'podman'), 'CDI provisioning must follow the shim'
        if shim == 'podman':
            assert (spec.parent / '.nvidia.yaml.qwen38').is_file(), 'a generated spec records its digest'
            # A second *.yaml in a scanned directory is a second spec of the
            # same kind, which makes podman reject the whole directory, so a
            # leftover here is not cosmetic.
            assert [p.name for p in spec.parent.glob('*.yaml')] == ['nvidia.yaml'], 'no spec leftovers'
            assert 'kind: nvidia.com/gpu' in spec.read_text(), 'the installed spec has content'
            assert not list(Path(env['HOME'], '.config').glob('.qwen38-cdi-*')), 'staging dir is removed'
        results.append({'shim': shim, 'userns_keep_id': shim == 'podman',
                        'relabelled_mounts': True, 'cdi_provisioned': shim == 'podman', 'passed': True})

# build.sh must still build when CDI provisioning cannot succeed; start.sh
# needs a GPU to do anything, so it stops there.
with tempfile.TemporaryDirectory() as folder:
    tmp = Path(folder)
    env = sandbox(tmp, 'podman', driver=None)
    checkpoint(env)
    build = ok('build.sh', env)
    assert 'nvidia-container-toolkit' in build, 'a missing toolkit must name the package'
    assert 'CUTE_DSL_ARCH=sm_120a' in build, 'a CDI failure must not stop the GPU-free build'
    refused = run('start.sh', env)
    assert refused.returncode != 0 and 'nvidia-container-toolkit' in refused.stderr
    assert not spec_of(env).exists()
    results.append({'toolkit': 'missing', 'build_still_runs': True, 'start_refuses': True, 'passed': True})

with tempfile.TemporaryDirectory() as folder:
    tmp = Path(folder)
    install_driver(tmp, OLD)
    env = sandbox(tmp, 'podman', OLD)
    spec = spec_of(env)
    marker = spec.parent / '.nvidia.yaml.qwen38'
    dropin_dir = tmp / 'home' / '.config' / 'containers' / 'containers.conf.d'
    ok('build.sh', env)
    registered = [p for p in dropin_dir.glob('*.conf') if f'"{spec.parent}"' in p.read_text()]
    assert len(registered) == 1, 'the spec dir must be registered once'
    assert 'Generated' not in ok('build.sh', env), 'a routine launch stays quiet'

    # Our spec is regenerated unconditionally, so a driver update needs no
    # staleness heuristic: the new paths, and any the old spec never named.
    install_driver(tmp, NEW)
    env = sandbox(tmp, 'podman', NEW)
    out = ok('build.sh', env)
    assert f'(driver {OLD} -> {NEW})' in out and f'.so.{NEW}' in spec.read_text()
    assert f'.so.{OLD}' not in spec.read_text() and NEW in marker.read_text()
    assert [p.name for p in spec.parent.glob('*.yaml')] == ['nvidia.yaml'], 'no spec leftovers'

    # A library the newer driver adds is invisible to any existence check,
    # because the old spec simply never referenced it. Regeneration finds it.
    (tmp / 'driver' / 'lib' / 'libnvidia-new.so').write_text('')
    ok('build.sh', env)
    assert 'libnvidia-new.so' in spec.read_text(), 'newly added host libraries are picked up'

with tempfile.TemporaryDirectory() as folder:
    tmp = Path(folder)
    install_driver(tmp, OLD)
    env = sandbox(tmp, 'podman', OLD)
    checkpoint(env)
    spec = spec_of(env)
    ok('build.sh', env)
    # Narrowing the spec by hand, here dropping the all entity, changes its
    # digest and transfers ownership: it is then validated but never rewritten.
    # Regenerating unconditionally, or requiring an all entity, both destroy it.
    spec.write_text(spec.read_text().replace('  - name: all\n', ''))
    assert 'name: all' not in spec.read_text()
    assert 'as-is' in ok('build.sh', env) and 'name: all' not in spec.read_text()
    assert 'Generated' not in ok('build.sh', env), 'a foreign spec is never rewritten'
    # Stale and not ours: reported precisely, still never rewritten.
    (tmp / 'driver' / 'lib' / 'libcuda.so.1').unlink()
    build = ok('build.sh', env)
    assert 'not being rewritten' in build and 'libcuda.so.1' in build, 'name the missing path'
    assert 'CUTE_DSL_ARCH=sm_120a' in build, 'a stale foreign spec must not stop the build'
    assert 'name: all' not in spec.read_text(), 'still untouched'
    refused = run('start.sh', env)
    assert refused.returncode != 0 and 'not being rewritten' in refused.stderr

with tempfile.TemporaryDirectory() as folder:
    tmp = Path(folder)
    install_driver(tmp, OLD)
    env = sandbox(tmp, 'podman', OLD)
    checkpoint(env)
    spec = spec_of(env)
    ok('build.sh', env)
    before = spec.read_text()
    # A generator failure must not cost a launch that a good spec would serve.
    (tmp / 'bin' / 'nvidia-ctk').write_text('#!/bin/sh\nexit 1\n')
    (tmp / 'bin' / 'nvidia-ctk').chmod(0o755)
    assert 'continuing with the existing' in ok('build.sh', env) and spec.read_text() == before
    assert run('start.sh', env).returncode == 0, 'an intact spec still serves the GPU'
    # With nothing usable behind it, silence would be worse than failing.
    spec.unlink()
    assert run('start.sh', env).returncode != 0 and 'no usable CDI spec' in \
        run('start.sh', env).stderr
    results.append({'idempotent': True, 'driver_update_regenerates': True,
                    'added_libraries_picked_up': True, 'narrowed_spec_survives': True,
                    'foreign_spec_reported_not_rewritten': True,
                    'generator_failure_degrades_gracefully': True, 'passed': True})

for line in results:
    print(json.dumps(line))

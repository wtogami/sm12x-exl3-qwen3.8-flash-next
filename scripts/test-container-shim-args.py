#!/usr/bin/env python3
"""Check the docker-vs-podman argument split and CDI provisioning, no container launched."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
DRIVER = '615.71.09'

CDI_SCRIPT = """#!/bin/sh
for arg in "$@"; do case "$arg" in --output=*) out="${arg#--output=}";; esac; done
{
  printf 'kind: nvidia.com/gpu\\ncdiVersion: 0.7.0\\ndevices:\\n  - name: all\\n  - name: 0\\n'
  printf '    hostPath: /usr/lib64/libcuda.so.DRIVER\\n'
} > "$out"
"""


def sandbox(tmp: Path, shim: str, driver: str | None, hermetic: bool = True) -> dict:
    """Fake docker echoing its argv, plus uname and the NVIDIA toolkit binaries.

    hermetic drops the real PATH so an nvidia-ctk installed on the test host
    cannot stand in for the one under test.
    """
    bin_dir = tmp / 'bin'
    bin_dir.mkdir(parents=True, exist_ok=True)
    version = 'podman version 5.8.7' if shim == 'podman' else 'Docker version 27.5.1'
    scripts = {'docker': f'if [ "$1" = "--version" ]; then printf "{version}\\n"; exit 0; fi\nprintf "%s\\n" "$@"',
               'uname': 'printf "%s\\n" "$TEST_ARCH"'}
    if driver:
        scripts['nvidia-smi'] = f'printf "{driver}\\n"'
        # The real generator embeds host driver paths carrying the driver
        # version, which is what makes a spec stale after an upgrade.
        scripts['nvidia-ctk'] = CDI_SCRIPT.replace('DRIVER', driver)
        scripts['nvidia-cdi-hook'] = 'exit 0'
    for name, body in scripts.items():
        path = bin_dir / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)
    if hermetic:
        core = tmp / 'core'
        core.mkdir(exist_ok=True)
        for name in ('cat', 'dirname', 'grep', 'head', 'id', 'mkdir', 'rm', 'sed', 'touch'):
            try:
                os.symlink(shutil.which(name), core / name)
            except FileExistsError:
                pass  # sandbox() is called twice against one tmp to model a driver upgrade
        path = f'{bin_dir}:{core}'
    else:
        path = f'{bin_dir}:{os.environ["PATH"]}'
    home = tmp / 'home'
    home.mkdir(parents=True, exist_ok=True)
    base = {key: value for key, value in os.environ.items()
            if key not in ('XDG_CONFIG_HOME', 'IMAGE', 'QUANT', 'HF_TOKEN', 'HOME')}
    return base | {'PATH': path, 'HOME': str(home),
                   'TEST_ARCH': 'x86_64', 'HF_CACHE': str(tmp / 'hf')}


def run(script: str, env: dict) -> str:
    result = subprocess.run(['/bin/bash', str(root / script)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, (script, result.stdout, result.stderr)
    return result.stdout + result.stderr


results = []
for shim in ('docker', 'podman'):
    with tempfile.TemporaryDirectory() as folder:
        tmp = Path(folder) / shim
        env = sandbox(tmp, shim, DRIVER)
        download = run('download.sh', env).splitlines()
        assert f'{tmp}/hf:/hf:z' in download, 'the cache mount must be relabelled with :z'
        assert ('--userns=keep-id' in download) == (shim == 'podman')
        assert 'HF_TOKEN' not in download
        assert 'HF_TOKEN' in run('download.sh', env | {'HF_TOKEN': 'hf_secret'})
        build = run('build.sh', env)
        assert 'CUTE_DSL_ARCH=sm_120a' in build
        spec = tmp / 'home' / '.config' / 'cdi' / 'nvidia.yaml'
        assert spec.is_file() == (shim == 'podman'), 'CDI provisioning must follow the shim'
        results.append({'shim': shim, 'userns_keep_id': shim == 'podman',
                        'cdi_provisioned': shim == 'podman', 'passed': True})

# build.sh must still build when CDI provisioning cannot succeed.
with tempfile.TemporaryDirectory() as folder:
    env = sandbox(Path(folder), 'podman', driver=None)
    build = run('build.sh', env)
    assert 'nvidia-container-toolkit' in build, 'a missing toolkit must name the package'
    assert 'CUTE_DSL_ARCH=sm_120a' in build, 'a CDI failure must not stop the GPU-free build'
    results.append({'toolkit': 'missing', 'build_still_runs': True, 'passed': True})

with tempfile.TemporaryDirectory() as folder:
    tmp = Path(folder)
    env = sandbox(tmp, 'podman', DRIVER)
    spec = tmp / 'home' / '.config' / 'cdi' / 'nvidia.yaml'
    run('build.sh', env)
    dropins = list((tmp / 'home' / '.config' / 'containers' / 'containers.conf.d').glob('*.conf'))
    assert len(dropins) == 1 and f'"{spec.parent}"' in dropins[0].read_text(), 'spec dir must be registered'
    assert 'Generating' not in run('build.sh', env), 'an up to date spec is left alone'
    # A driver upgrade invalidates the pinned host paths, so the spec is rewritten.
    env = sandbox(tmp, 'podman', driver='620.11.01')
    assert 'Generating' in run('build.sh', env) and DRIVER not in spec.read_text()
    assert len(list(dropins[0].parent.glob('*.conf'))) == 1, 'registration must not duplicate'
    # A spec that lost the all entity is rewritten too.
    spec.write_text(spec.read_text().replace('name: all', 'name: none'))
    assert 'Generating' in run('build.sh', env) and 'name: all' in spec.read_text()
    results.append({'idempotent': True, 'stale_spec_regenerated_on_driver_change': True,
                    'missing_all_entity_regenerated': True, 'passed': True})

for line in results:
    print(json.dumps(line))

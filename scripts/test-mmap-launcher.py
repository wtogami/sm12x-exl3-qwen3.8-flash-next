#!/usr/bin/env python3
"""Exercise all profile/mmap launcher combinations with an inert Docker command."""
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile
root=Path(__file__).resolve().parents[1]
profiles={}
for name,body in re.findall(r'^  ([a-z0-9-]+)\)\n(.*?)    ;;',(root/'model-profiles.sh').read_text(),re.M|re.S):
    profiles[name]=tuple(re.search(r'^    '+k+r'=(.+)$',body,re.M)[1] for k in ('MODEL_REPO','MODEL_REVISION'))
def engram_arg(args):
    index=args.index('--engram-config')
    return json.loads(args[index+1])
with tempfile.TemporaryDirectory() as tmp:
    tmp=Path(tmp)
    fake=tmp/'bin';fake.mkdir()
    docker=fake/'docker';docker.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "inspect" ]; then exit 1; fi\n'   # no such container -> launcher proceeds
        'printf "%s\\n" "$@"\n')
    docker.chmod(0o755)
    # Deliberately NOT REPLACE=1: with a real runtime reachable, that flag would stop and
    # delete a live serving container. Making inspect fail is both safer and deterministic.
    for name,(repo,revision) in profiles.items():
        model=tmp/'hf/hub'/('models--'+repo.replace('/','--'))/'snapshots'/revision
        model.mkdir(parents=True);(model/'config.json').write_text('{}')
        for mode in (0,1):
            for strategy in ('round2','exact'):
                env=os.environ|{'PATH':str(fake)+':'+os.environ['PATH'],'HF_CACHE':str(tmp/'hf'),
                    'RUNTIME_CACHE':str(tmp/'runtime'),'QUANT':name,'PLE_MMAP':str(mode),
                    'PLE_PIN_STRATEGY':strategy}
                args=subprocess.check_output(['bash',str(root/'start.sh')],env=env,text=True).splitlines()
                engram=engram_arg(args)
                assert engram=={'cpu_offload':True,'checkpoint_mapped':bool(mode)},engram
                assert not [a for a in args if a.startswith('VLLM_PLE_MMAP=')]
                assert not [a for a in args if a.startswith('VLLM_PLE_CPU_OFFLOAD=')]
                assert 'qwen38-'+name in args
                assert '--tensor-parallel-size' in args and '--kv-cache-dtype' in args
                # The preflight sizes the pinned reservation on the host and the loader
                # performs it inside the container. If the strategy is not forwarded, the
                # gate approves the smaller footprint while torch pins the rounded one --
                # which is how the first version of this change shipped.
                assert f'PLE_PIN_STRATEGY={strategy}' in args, \
                    f'{name}/{strategy}: strategy never reached the container'
                print(json.dumps({'profile':name,'mmap':bool(mode),
                                  'pin_strategy':strategy,'passed':True}))
            # A typo must fail loudly rather than silently mean round2.
            bad=os.environ|{'PATH':str(fake)+':'+os.environ['PATH'],'HF_CACHE':str(tmp/'hf'),
                'RUNTIME_CACHE':str(tmp/'runtime'),'QUANT':name,'PLE_PIN_STRATEGY':'pow2please'}
            r=subprocess.run(['bash',str(root/'start.sh')],env=bad,capture_output=True,text=True)
            assert r.returncode==2,('invalid PLE_PIN_STRATEGY must exit 2',r.returncode,r.stderr[-120:])
    env = os.environ | {'PATH': str(fake)+':'+os.environ['PATH'],
        'HF_CACHE': str(tmp/'hf'), 'RUNTIME_CACHE': str(tmp/'runtime')}
    for key in ('QUANT', 'PLE_MMAP', 'VLLM_PLE_MMAP', 'CONTAINER_NAME'):
        env.pop(key, None)
    args = subprocess.check_output(['bash', str(root/'start.sh')], env=env, text=True).splitlines()
    assert 'qwen38-exl3' in args
    # Shipping default: mmap on unified-memory Spark (arm64), off on discrete RTX.
    expected_mmap = platform.machine() in ('aarch64', 'arm64')
    assert engram_arg(args)=={'cpu_offload':True,'checkpoint_mapped':expected_mmap}
    print(json.dumps({'profile': 'exl3', 'mmap': expected_mmap, 'shipping_defaults': True, 'passed': True}))
    env['PLE_MMAP']='1'
    args = subprocess.check_output(['bash', str(root/'start.sh')], env=env, text=True).splitlines()
    assert engram_arg(args)=={'cpu_offload':True,'checkpoint_mapped':True}
    env['PLE_MMAP']='0'
    args = subprocess.check_output(['bash', str(root/'start.sh')], env=env, text=True).splitlines()
    assert engram_arg(args)=={'cpu_offload':True,'checkpoint_mapped':False}
    print(json.dumps({'profile': 'exl3', 'explicit_override': True, 'passed': True}))

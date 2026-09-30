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
    docker=fake/'docker';docker.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n');docker.chmod(0o755)
    for name,(repo,revision) in profiles.items():
        model=tmp/'hf/hub'/('models--'+repo.replace('/','--'))/'snapshots'/revision
        model.mkdir(parents=True);(model/'config.json').write_text('{}')
        for mode in (0,1):
            env=os.environ|{'PATH':str(fake)+':'+os.environ['PATH'],'HF_CACHE':str(tmp/'hf'),
                'RUNTIME_CACHE':str(tmp/'runtime'),'QUANT':name,'PLE_MMAP':str(mode)}
            args=subprocess.check_output(['bash',str(root/'start.sh')],env=env,text=True).splitlines()
            engram=engram_arg(args)
            assert engram=={'cpu_offload':True,'checkpoint_mapped':bool(mode)},engram
            assert not [a for a in args if a.startswith('VLLM_PLE_MMAP=')]
            assert not [a for a in args if a.startswith('VLLM_PLE_CPU_OFFLOAD=')]
            assert 'qwen38-'+name in args
            assert '--tensor-parallel-size' in args and '--kv-cache-dtype' in args
            print(json.dumps({'profile':name,'mmap':bool(mode),'passed':True}))
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

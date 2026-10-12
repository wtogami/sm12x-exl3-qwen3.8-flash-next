#!/usr/bin/env python3
"""Test the run-line generator against a real container definition (read-only; starts nothing).

    python3 scripts/test-mk-cell-run.py --base-container qwen38-exl3-ple8 \
        --image localhost/qwen38-exactpin:stream

Every case is one that produced a broken container in practice or would have: podman flags after the
image (they become container argv), env not reaching the worker, --enforce-eager landing on podman's
command line instead of vllm's, and the image silently not being replaced.
"""
import argparse
import os
import shlex
import subprocess
import sys

PODMAN_ONLY = {"-e", "--env", "-d", "--detach", "--name", "-v", "--volume", "--device",
               "--network", "--ipc", "--security-opt", "--annotation", "-p", "--publish"}
HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, "mk-cell-run.py")


def gen(base, *extra):
    r = subprocess.run([sys.executable, GEN, "--base-container", base, *extra],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"generator failed: {r.stderr.strip()[:200]}")
    line = [l for l in r.stdout.splitlines() if l.startswith("exec ")][-1]
    return shlex.split(line[len("exec "):])


def image_index(toks):
    seen_pod = False
    for i, t in enumerate(toks):
        if t in ("run", "create"):
            seen_pod = True
            continue
        if not seen_pod or t.startswith("-"):
            continue
        # skip flag values by relying on the generator's own invariant: the first non-flag after the
        # subcommand that looks like an image reference
        if "/" in t or ":" in t:
            return i
    raise AssertionError("no image token found in generated line")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-container", required=True)
    ap.add_argument("--image", required=True)
    a = ap.parse_args()
    fails = 0

    def check(label, cond):
        nonlocal fails
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            fails += 1

    toks = gen(a.base_container, "--name", "ple-bench", "--image", a.image,
               "--env", "PLE_STREAM=1", "--env", "VLLM_USE_BREAKABLE_CUDAGRAPH=1",
               "--drop-env", "PLE_PIN_STRATEGY", "--arg=--enforce-eager")
    img = image_index(toks)
    pre, post = toks[:img], toks[img + 1:]

    check("image replaced with the requested one", toks[img] == a.image)
    check("container name is ple-bench", toks[toks.index("--name") + 1] == "ple-bench")
    check("no podman-only flag appears after the image", not (set(post) & PODMAN_ONLY))
    check("PLE_STREAM=1 present before the image", "PLE_STREAM=1" in pre)
    check("VLLM_USE_BREAKABLE_CUDAGRAPH=1 before the image",
          "VLLM_USE_BREAKABLE_CUDAGRAPH=1" in pre)
    check("--drop-env removed PLE_PIN_STRATEGY",
          not any(t.startswith("PLE_PIN_STRATEGY=") for t in pre))
    check("--enforce-eager is container argv (after the image)",
          "--enforce-eager" in post and toks.index("--enforce-eager") > img)

    # things the original container needs that a reconstruction tends to forget
    base_line = gen(a.base_container, "--name", "ple-bench")
    bi = image_index(base_line)
    bpre = base_line[:bi]
    flagbag = " ".join(bpre)
    check("--network preserved", "--network" in flagbag or "--network=" in flagbag)
    check("--ipc preserved", "--ipc" in flagbag)
    check("--device preserved", "--device" in flagbag)
    check("volumes preserved", "-v" in bpre or "--volume" in bpre)
    check("omitting --image keeps the base image", base_line[bi] != a.image)

    print(f"  {'ALL PASS' if fails == 0 else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

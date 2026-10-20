#!/usr/bin/env python3
"""Test the run-line generator against a real container definition (read-only; starts nothing).

    python3 scripts/test-mk-cell-run.py --base-container qwen38-exl3-ple8 \
        --image localhost/qwen38-exactpin:stream

Every case is one that produced a broken container in practice or would have: podman flags after the
image (they become container argv), env not reaching the worker, --enforce-eager landing on podman's
command line instead of vllm's, and the image silently not being replaced.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys

PODMAN_ONLY = {"-e", "--env", "-d", "--detach", "--name", "-v", "--volume", "--device",
               "--network", "--ipc", "--security-opt", "--annotation", "-p", "--publish"}
HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, "mk-cell-run.py")


def _is_json(s):
    try:
        json.loads(s)
        return True
    except Exception:
        return False


def gen(base, *extra):
    r = subprocess.run([sys.executable, GEN, "--base-container", base, *extra],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"generator failed: {r.stderr.strip()[:200]}")
    line = [l for l in r.stdout.splitlines() if l.startswith("exec ")][-1]
    return shlex.split(line[len("exec "):])


def base_image(container: str) -> str:
    r = subprocess.run(["podman", "inspect", container, "--format", "{{.Config.Image}}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"cannot read image of {container}: {r.stderr.strip()[:120]}")
    return r.stdout.strip()


def norm_ref(ref: str) -> str:
    """Compare image references the way podman does, not the way strings do.

    CreateCommand records the reference as typed (`qwen38-rtx:local`) while Config.Image is resolved
    (`localhost/qwen38-rtx:local`). Treating those as different made a correct generator look broken.
    """
    return ref[len("localhost/"):] if ref.startswith("localhost/") else ref


def image_index(toks, expected):
    """Locate the image by exact string, not by heuristic.

    "First non-flag token containing a slash" matched `nvidia.com/gpu=all`, the value of --device, and
    made this test report a correct generated line as broken. We always know the exact image string, so
    there is nothing to infer.
    """
    try:
        i = toks.index(expected)
    except ValueError:
        matches = [j for j, x in enumerate(toks) if norm_ref(x) == norm_ref(expected)]
        if len(matches) != 1:
            raise AssertionError(f"image {expected!r} not uniquely located "
                                 f"(matched {len(matches)} tokens)")
        i = matches[0]
    if toks[i - 1].startswith("-") and toks[i - 1] in ("-e", "--env", "-v", "--volume", "--device",
                                                       "--name", "--network", "--ipc"):
        raise AssertionError(f"{expected!r} matched a flag's value, not the image position")
    return i


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

    bimg = base_image(a.base_container)
    toks = gen(a.base_container, "--name", "ple-bench", "--image", a.image,
               "--env", "PLE_STREAM=1", "--env", "VLLM_USE_BREAKABLE_CUDAGRAPH=1",
               "--drop-env", "PLE_PIN_STRATEGY", "--arg=--enforce-eager")
    img = image_index(toks, a.image)
    pre, post = toks[:img], toks[img + 1:]

    check("image replaced with the requested one", norm_ref(toks[img]) == norm_ref(a.image))
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
    bi = image_index(base_line, bimg)
    bpre = base_line[:bi]
    flagbag = " ".join(bpre)
    check("--network preserved", "--network" in flagbag or "--network=" in flagbag)
    check("--ipc preserved", "--ipc" in flagbag)
    check("--device preserved", "--device" in flagbag)
    check("volumes preserved", "-v" in bpre or "--volume" in bpre)
    same = norm_ref(base_line[bi]) == norm_ref(bimg)
    swapped = norm_ref(base_line[bi]) != norm_ref(a.image)
    check("omitting --image keeps the base image", same and swapped)

    # Argument fidelity. The generator once round-tripped CreateCommand through a joined string and
    # shlex.split, which silently deleted the double quotes inside --hf-overrides' JSON value; the
    # container then died on its own argv. Flag positions were perfect, so no positional check could see
    # it. Assert the JSON survives with its quotes, on a container that actually carries one.
    full = gen(a.base_container, "--name", "ple-bench")
    joined = " ".join(full)
    if "--hf-overrides" in joined:
        i = full.index("--hf-overrides")
        val = full[i + 1]
        check("JSON-valued arg survives with quotes", '\"' in val and val.startswith("{"))
        check("JSON-valued arg still parses as JSON", _is_json(val))
    else:
        print("  SKIP  no JSON-valued arg on this container to check")

    print(f"  {'ALL PASS' if fails == 0 else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

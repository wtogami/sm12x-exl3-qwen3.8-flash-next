#!/usr/bin/env python3
"""Derive a container run line from a live container's CreateCommand, changing only what is asked.

Why CreateCommand and not reconstruction from `podman inspect` fields: reconstructing needs every flag
class enumerated, and a missed one (--network host, --device nvidia.com/gpu=all) produces a container
that starts and then fails in a way that looks like the thing under test. CreateCommand is the verbatim
original. The failure modes this guards against are ones I actually hit: appending -e/-d AFTER the image,
where podman hands them to the container command, so `vllm serve` received "-e PLE_STREAM=1" as argv.

Usage:
  scripts/mk-cell-run.py --base-container qwen38-exl3-ple8 --name ple-bench \
      --image localhost/qwen38-exactpin:stream --env PLE_STREAM=1 --arg --enforce-eager
"""
import argparse
import shlex
import subprocess
import sys

# Flags that must precede the image. Anything from this set found after it is a construction bug.
PODMAN_ONLY = {"-e", "--env", "-d", "--detach", "--name", "-v", "--volume", "--device",
               "--network", "--ipc", "--security-opt", "--annotation", "-p", "--publish"}


def create_command(container: str) -> list[str]:
    r = subprocess.run(["podman", "inspect", container, "--format",
                        "{{join .Config.CreateCommand \" \"}}"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"cannot inspect {container}: {r.stderr.strip()[:200]}")
    toks = shlex.split(r.stdout.strip())
    if not toks:
        raise SystemExit(f"{container} has no CreateCommand; cannot derive a faithful run line")
    return toks


def build(base: list[str], name: str, image: str | None, envs: list[str], args: list[str],
          drop_env: list[str]) -> list[str]:
    """Rewrite `base` (a full podman run argv) into the variant we want, before the image token."""
    # locate the image: first token after the `run` subcommand that is not a flag or a flag's value
    # `podman run -d` and `podman create` produce the same flag grammar, and CreateCommand reflects
    # whichever was used -- accepting only "run" made the generator reject a legitimately created
    # container over a difference that does not exist.
    run_i = next((i for i, tok in enumerate(base) if tok in ("run", "create")), None)
    if run_i is None:
        raise SystemExit("CreateCommand contains neither `run` nor `create`; refusing to guess")
    takes_value = {"--name", "-e", "--env", "-v", "--volume", "--device", "--network", "--ipc",
                   "--security-opt", "--annotation", "-p", "--publish", "--entrypoint", "--user",
                   "--workdir", "--shm-size", "--ulimit", "--pid", "--hostname", "--cidfile",
                   "--label", "--log-driver", "--rootfs", "--cgroup-conf", "--memory", "--cpus"}
    img_i, i, skip = None, run_i + 1, False
    while i < len(base):
        tok = base[i]
        if skip:
            skip = False
            i += 1
            continue
        if tok in takes_value:
            skip = True
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        img_i = i
        break
    if img_i is None:
        raise SystemExit("could not locate the image token in CreateCommand")

    head, tail = base[:img_i], base[img_i + 1:]
    # strip what we are replacing: name, detach, dropped envs
    out, j = [], 0
    want_drop = set(drop_env)
    while j < len(head):
        tok = head[j]
        if tok == "--name":
            j += 2
            continue
        if tok in ("-d", "--detach"):
            j += 1
            continue
        if tok in ("-e", "--env") and j + 1 < len(head) and head[j + 1].split("=", 1)[0] in want_drop:
            j += 2
            continue
        out.append(tok)
        j += 1
    new_image = image or base[img_i]
    out = out + ["--name", name]
    for kv in envs:
        out += ["-e", kv]
    cmd = out + [new_image] + tail + list(args)
    # The image position is known here; re-detecting it later is what produced a false alarm, because
    # the first non-flag token in a podman line is usually a flag's VALUE (nvidia.com/gpu=all for
    # --device), not the image.
    return cmd, len(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-container", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--image")
    ap.add_argument("--env", action="append", default=[], help="KEY=VALUE, repeatable")
    ap.add_argument("--drop-env", action="append", default=[], help="KEY to remove from the base line")
    ap.add_argument("--arg", action="append", default=[],
                    help="appended AFTER the image, i.e. container argv. Use the --arg=VALUE form for "
                         "values that start with a dash (--arg=--enforce-eager); argparse cannot tell a "
                         "bare --enforce-eager from another option")
    ap.add_argument("--emit", choices=("script", "line"), default="script")
    a = ap.parse_args()

    cmd, img_i = build(create_command(a.base_container), a.name, a.image, a.env, a.arg, a.drop_env)

    # self-check: podman-level flags must never appear after the image. Container argv legitimately
    # starts with flags (--enforce-eager, --max-model-len), so only the podman-only set is a bug.
    after = [t for t in cmd[img_i + 1:] if t in PODMAN_ONLY]
    if after:
        raise SystemExit(f"BUG: podman flags after the image ({after}); they would become container argv")

    line = " ".join(shlex.quote(t) for t in cmd)
    if a.emit == "line":
        print(line)
    else:
        print("#!/bin/bash\n# generated by mk-cell-run.py from CreateCommand of "
              f"{a.base_container}\nset -euo pipefail\nexec {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

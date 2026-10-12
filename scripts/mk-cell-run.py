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


TAKES_VALUE = {"--name", "-e", "--env", "-v", "--volume", "--device", "--network", "--ipc",
               "--security-opt", "--annotation", "-p", "--publish", "--entrypoint", "--user",
               "--workdir", "--shm-size", "--ulimit", "--pid", "--hostname", "--cidfile",
               "--label", "--log-driver", "--rootfs", "--cgroup-conf", "--memory", "--cpus"}


def find_image_index(tokens: list[str], start: int) -> int:
    """Index of the image token: first non-flag after `start` that is not some flag's value.

    One implementation on purpose. build() and the preservation check used to each carry their own, and
    the second one located the model path instead of the image -- which made a correct line fail the
    check, the exact failure mode that teaches people to delete checks.
    """
    i, skip = start, False
    while i < len(tokens):
        tok = tokens[i]
        if skip:
            skip = False
            i += 1
            continue
        if tok in TAKES_VALUE:
            skip = True
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return i
        # everything after this point is container argv, which may legitimately start with a dash
    raise SystemExit("could not locate the image token in the container command")


def create_command(container: str) -> list[str]:
    """The container's argv, token for token.

    Read it as JSON, never as a joined string. The first version asked podman to join CreateCommand
    with spaces and then shlex.split()ed the result -- but shlex.split PARSES shell quoting, and
    CreateCommand is an argv list with no quoting information left in it. So an argument that legitimately
    contains double quotes, like --hf-overrides '{"text_config":{...}}', came back with every quote
    stripped. The container then died on its own command line ("argument --hf-overrides: Value ... cannot
    be converted to <function loads>"), which looks like a podman rejection and is nothing of the kind.
    """
    import json
    r = subprocess.run(["podman", "inspect", container, "--format",
                        "{{json .Config.CreateCommand}}"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"cannot inspect {container}: {r.stderr.strip()[:200]}")
    toks = json.loads(r.stdout)
    if not isinstance(toks, list) or not toks:
        raise SystemExit(f"{container} has no CreateCommand; cannot derive a faithful run line")
    return [str(x) for x in toks]


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
    img_i = find_image_index(base, run_i + 1)

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
    return cmd, len(out), base[img_i]


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
    ap.add_argument("--detach", action="store_true",
                    help="add -d (the base line's own -d is stripped, so cells must ask for it)")
    ap.add_argument("--emit", choices=("script", "line"), default="script")
    a = ap.parse_args()

    base = create_command(a.base_container)
    cmd, img_i, base_image = build(base, a.name, a.image, a.env, a.arg, a.drop_env)
    if a.detach:
        cmd = cmd[:2] + ["-d"] + cmd[2:]
        img_i += 1        # the index build() reported is now one token later

    # --- content preservation -------------------------------------------------
    # The container argv (everything after the image) must be byte-identical to the base container's,
    # plus whatever --arg we asked for. This is the check that the join-then-shlex bug needed: flag
    # positions were perfect while --hf-overrides' JSON value had lost every quote, so only comparing
    # the tokens themselves can see it.
    sub = next(i for i, tok in enumerate(base) if tok in ("run", "create"))
    base_img_i = find_image_index(base, sub + 1)
    want_tail = base[base_img_i + 1:] + list(a.arg)
    if cmd[img_i + 1:] != want_tail:
        for i, (x, y) in enumerate(zip(cmd[img_i + 1:], want_tail)):
            if x != y:
                raise SystemExit(f"BUG: container argv token {i} changed: {x[:70]!r} != {y[:70]!r}")
        raise SystemExit("BUG: container argv length changed unexpectedly")

    # Every base podman flag/value must survive unless deliberately replaced. Values are tracked by which
    # flag they belong to, because 'localhost/qwen38-rtx:local' is a token like any other but is the one
    # we are explicitly allowed to swap.
    drop_keys = set(a.drop_env) | {kv.split("=", 1)[0] for kv in a.env}
    cur_flag, missing = None, []
    i = sub + 1
    while i < base_img_i:
        tok = base[i]
        if tok in ("--name",):
            cur_flag = tok
        elif tok in ("-e", "--env"):
            cur_flag = tok
        elif tok in ("-d", "--detach"):
            cur_flag = None            # stripped on purpose; --detach re-adds it
        elif cur_flag in ("-e", "--env"):
            if tok.split("=", 1)[0] not in drop_keys and tok not in cmd[:img_i]:
                missing.append(tok)
            cur_flag = None
        elif cur_flag == "--name":
            cur_flag = None            # replaced on purpose
        elif tok.startswith("-"):
            cur_flag = tok
        elif tok == base_image:
            pass                       # replaced on purpose when --image given
        elif tok not in cmd[:img_i]:
            missing.append(tok)
        i += 1
    if missing:
        raise SystemExit("BUG: podman arguments lost from the base container: "
                         + ", ".join(repr(x[:60]) for x in missing[:3]))

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

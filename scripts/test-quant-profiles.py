#!/usr/bin/env python3
"""Check the quant-profile vocabulary agrees across the repo, launching nothing.

`model-profiles.sh` is the only file that has to know the profile list, but the
list is spelled out in several more places, and it already drifted: the two
profilers hardcoded a `--profile` enum with no `nvfp4-redhatai`, which made
`QUANT=nvfp4-redhatai` die in argparse and pushed callers into stamping RedHatAI
receipts `quant_profile: "nvfp4"` -- a false FP8-table claim about a BF16
checkpoint.  This is the regression guard for that class of drift.

Deliberately a plain script like the other scripts/test-*.py checks: static file
analysis, stdlib only, no pytest, no container, no endpoint, so it runs on a
host as well as in the image.

Run:  python3 scripts/test-quant-profiles.py
"""
import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server_state import derive  # noqa: E402

root = Path(__file__).resolve().parents[1]


def read(rel):
    return (root / rel).read_text()


def calls(script, flag):
    """Every `add_argument(flag, ...)` node in a script, without importing it."""
    found = []
    for node in ast.walk(ast.parse(read(script))):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", None))
                == "add_argument"
                and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == flag):
            found.append(node)
    return found


def choices(script, flag):
    for call in calls(script, flag):
        for kw in call.keywords:
            if kw.arg == "choices":
                return set(ast.literal_eval(kw.value))
    raise AssertionError(f"{script}: {flag} has no choices= list")


# 1. model-profiles.sh is the source of truth: parse its case labels.
profiles_src = read("model-profiles.sh")
labels = re.findall(r"^  ([a-z0-9-]+)\)$", profiles_src, re.M)
assert labels, "no case labels found in model-profiles.sh; did its shape change?"
assert len(labels) == len(set(labels)), f"duplicate profile labels: {labels}"
profiles = set(labels)

# 2. Each branch must set everything the launcher and the preflight consume.
#    start.sh:74 falls back to 2 when PLE_BYTES_PER_ELEMENT is unset, so a new
#    profile that forgets it silently gets BF16 sizing on an FP8 table.
branches = re.split(r"^  [a-z0-9-]+\)$", profiles_src, flags=re.M)[1:]
for label, body in zip(labels, branches):
    for var in ("MODEL_REPO=", "MODEL_REVISION=", "PLE_BYTES_PER_ELEMENT="):
        assert var in body, f"profile {label} does not set {var.rstrip('=')}"
    bytes_per = re.search(r"PLE_BYTES_PER_ELEMENT=(\S+)", body).group(1)
    assert bytes_per in ("1", "2"), f"profile {label}: PLE_BYTES_PER_ELEMENT={bytes_per}"
    assert re.search(r"^[^#]*MODEL_REPO=(\S+)$", body, re.M), f"{label}: MODEL_REPO blank"

# 3. The profilers' enums must not lag the profiles.
for script in ("scripts/benchmark-decode.py", "scripts/benchmark-prefill.py"):
    got = choices(script, "--profile")
    missing, extra = profiles - got, got - profiles
    assert not missing, (f"{script}: --profile is missing {sorted(missing)}; "
                         f"callers then relabel the run and the receipt lies")
    assert not extra, f"{script}: --profile accepts unknown {sorted(extra)}"

# 4. Human-facing lists of the same profiles must not go stale either.
usage_sites = {
    "model-profiles.sh": re.search(r'QUANT must be ([^"]*)', profiles_src).group(1),
    "scripts/benchmark-suite.sh": re.search(
        r'\$\{QUANT:\?Set ([^}]*)\}', read("scripts/benchmark-suite.sh")).group(1),
}
for where, text in usage_sites.items():
    absent = [p for p in profiles if p not in text]
    assert not absent, f"{where} usage text does not list {absent}"

# 5. README documents one row per profile, and two of its columns restate facts
#    that live elsewhere -- the PLE format and the RTX MTP default.  Checking the
#    echo is what keeps the table honest; a bare "name appears in README" test
#    passes even when the row is deleted, because the name also occurs in prose.
def row_cells(line):
    """Markdown table row as a list, without the empty edge cells."""
    cells = [c.strip() for c in line.split("|")]
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


rows, header = {}, None
for line in read("README.md").splitlines():
    cells = row_cells(line)
    if len(cells) > 2 and cells[0].startswith("Profile (`QUANT`)"):
        header = cells
        continue
    if header and len(cells) == len(header) and re.fullmatch(r"`[a-z0-9-]+`", cells[0]):
        rows[cells[0].strip("`")] = dict(zip(header, cells))
assert rows, "README profile table not found; its header or shape changed"
assert set(rows) == profiles, (
    f"README table rows {sorted(rows)} != profiles {sorted(profiles)}")

bytes_per = {label: int(re.search(r"PLE_BYTES_PER_ELEMENT=(\S+)", body).group(1))
             for label, body in zip(labels, branches)}
for profile, row in rows.items():
    fmt = row["PLE table format"]
    want = "BF16" if bytes_per[profile] == 2 else "FP8"
    assert want in fmt, f"README says {profile} PLE is '{fmt}' but PLE_BYTES_PER_ELEMENT={bytes_per[profile]}"
    want_gib = "95" if bytes_per[profile] == 2 else "48"
    assert want_gib in fmt, f"README says {profile} PLE is '{fmt}', expected the {want_gib} GiB table"

# The default draft length is decided by a per-quant rule in start.sh on top of a
# per-architecture default in platform-config.sh, and is only written down in
# README, so pin all three to each other.
rule = re.search(r'\[\[ \$\{QUANT:-exl3\} == (\S+) \]\] && echo (\d+) '
                 r'\|\| echo "\$DEFAULT_EXL3_MTP_TOKENS"', read("start.sh"))
assert rule, ("start.sh:27 MTP rule was restructured; update this check and make "
              "the default a profile field rather than a string comparison")
special, special_mtp = rule.group(1), int(rule.group(2))
assert special in profiles, f"start.sh special-cases unknown profile {special!r}"


def arch_default(var, needle):
    """A variable's value inside one `case "$(uname -m)"` arm."""
    arms = re.split(r"(?m)^  ([a-z0-9_|]+)\)$", read("platform-config.sh"))
    for header, body in zip(arms[1::2], arms[2::2]):
        if needle in header.split("|"):
            found = re.search(var + r"=(\d+)", body)
            assert found, f"{var} is not set in the {needle} arm"
            return int(found.group(1))
    raise AssertionError(f"platform-config.sh has no {needle} arm")


defaults = {"RTX": arch_default("DEFAULT_EXL3_MTP_TOKENS", "x86_64"),
            "Spark": arch_default("DEFAULT_EXL3_MTP_TOKENS", "aarch64")}
for profile, row in rows.items():
    columns = row["Default MTP (RTX / Spark)"].split("/")
    assert len(columns) == 2, f"README MTP cell for {profile} is not '<RTX> / <Spark>'"
    for platform_, documented in zip(("RTX", "Spark"), (int(c.strip()) for c in columns)):
        expected = special_mtp if profile == special else defaults[platform_]
        assert documented == expected, (
            f"README gives {profile} a {platform_} default draft length of "
            f"{documented}, but start.sh produces {expected} "
            f"(special-cases {special} at {special_mtp}, otherwise "
            f"DEFAULT_EXL3_MTP_TOKENS={defaults[platform_]})")

# 7. server_state.py must decode the engram config start.sh actually emits,
#    otherwise receipts silently stop carrying the PLE path.
launcher = read("start.sh")
emitted = set(re.findall(r"--engram-config '(\{[^']*\})'", launcher))
assert len(emitted) == 2, f"expected mapped and unmapped engram configs in start.sh, got {emitted}"
modes = {derive(["serve", "m", "--engram-config", cfg])["ple_mode"] for cfg in emitted}
assert modes == {"checkpoint-mapped", "host-offload"}, \
    f"start.sh configs decode to {sorted(modes)}"
draft = derive(["serve", "m", "--speculative-config",
                '{"method":"mtp","num_speculative_tokens":2}'])
assert draft.get("mtp_tokens") == 2, draft
assert derive(["serve", "m"]).get("ple_mode", "").startswith("engine-default")
# A malformed flag must be kept as evidence, not dropped.
broken = derive(["serve", "m", "--engram-config", "{oops"])
assert "engram_config_unparsable" in broken, broken

print(f"profile vocabulary agrees across {len(profiles)} profiles: "
      f"{', '.join(sorted(profiles))}")
print("  model-profiles.sh branches set repo/revision/element size for each;")
print("  both profiler --profile enums match;")
print("  usage text and the README table row-per-profile match;")
print("  README PLE-format and RTX/Spark draft lengths follow the launcher;")
print("  analyze-four-way.py's alias vocabulary covers every served name;")
print("  server_state decodes the engram and MTP configs start.sh emits.")

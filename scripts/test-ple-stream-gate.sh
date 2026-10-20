#!/bin/bash
# Test the streaming memory preflight (qwen38_ple_stream_check) against fabricated memory states.
#
# Each case must be capable of failing, and the accept/refuse pair differs by exactly one knob so a
# refusal is attributable to the floor rather than to a typo in the case. QWEN38_CGROUP_DIR is the hook
# that makes the cgroup branch testable at all: /proc/meminfo inside a capped container reports the
# HOST's RAM, and creating real cgroups needs root.
set -uo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LIB="${LIB_OVERRIDE:-$HERE/../platform-config.sh}"   # override enables mutation testing
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
GIB=1048576

cat > "$TMP/config.json" <<'JSON'
{"text_config": {"ngram_vocab_size_base": 20000000, "ple_embed_dim": 2560,
 "vocab_size": 248320, "hidden_size": 2560, "ngram_size": 3, "heads_per_ngram": 8,
 "split_ngram_parts": 128}}
JSON
printf 'this is not json\n' > "$TMP/bad.json"
mkdir -p "$TMP/cg3" "$TMP/cgmax" "$TMP/cgv1"
echo $((3 * GIB * 1024)) > "$TMP/cg3/memory.max";  echo 0 > "$TMP/cg3/memory.current"
echo max                   > "$TMP/cgmax/memory.max"; echo 0 > "$TMP/cgmax/memory.current"
echo $((2 * GIB * 1024))   > "$TMP/cgv1/memory.limit_in_bytes"; echo 0 > "$TMP/cgv1/memory.usage_in_bytes"

pass=0; fail=0

check() {  # label want_rc total_kib avail_kib needle [ENV=VAL ...]
  local label=$1 want=$2 total=$3 avail=$4 needle=$5; shift 5
  local cfgfile="$TMP/config.json" a out rc
  local envs=()
  for a in "$@"; do
    case "$a" in
      TEST_CFG=*) cfgfile="${a#TEST_CFG=}" ;;
      *) envs+=("$a") ;;
    esac
  done
  out=$(env -u PLE_STREAM_CACHE_GIB -u PLE_STREAM_CACHE_WARN_GIB \
          CFG="$cfgfile" TOTAL="$total" AVAIL="$avail" LIB="$LIB" \
          "${envs[@]}" \
          bash -c 'source "$LIB"; qwen38_ple_stream_check "$CFG" 1 "$TOTAL" "$AVAIL"' 2>&1)
  rc=$?
  local ok=1
  [ "$rc" = "$want" ] || ok=0
  if [ -n "$needle" ]; then grep -q -- "$needle" <<<"$out" || ok=0; fi
  if [ "$ok" = 1 ]; then
    pass=$((pass + 1)); echo "  PASS  $label  [rc=$rc]"
  else
    fail=$((fail + 1)); echo "  FAIL  $label  [rc=$rc want $want needle='${needle}']"
    sed 's/^/          /' <<<"$out"
  fi
}

# 2 GiB rounded token-embedding pin is common to every case; only headroom and the knobs move.
check "roomy host accepts"                    0 $((125 * GIB)) $((60 * GIB)) "matches a fully resident table"
check "warns below the 16 GiB plateau"        0 $(( 32 * GIB)) $(( 14 * GIB)) "Measured on this table"
check "same host, floor raised to 20 refuses" 1 $(( 32 * GIB)) $(( 14 * GIB)) "Refusing to start" PLE_STREAM_CACHE_GIB=20
check "cgroup v2 clips MemAvailable"          1 $((125 * GIB)) $(( 60 * GIB)) "cgroup-clipped" QWEN38_CGROUP_DIR="$TMP/cg3"
check "cgroup v1 clips too"                   1 $((125 * GIB)) $(( 60 * GIB)) "cgroup-clipped" QWEN38_CGROUP_DIR="$TMP/cgv1"
check "unlimited cgroup leaves it alone"      0 $((125 * GIB)) $(( 60 * GIB)) "matches a fully resident table" QWEN38_CGROUP_DIR="$TMP/cgmax"
check "table sized from base x dim (47.7)"    0 $((125 * GIB)) $(( 60 * GIB)) "47.7 GiB"
check "staging arena reported in MiB"         0 $((125 * GIB)) $(( 60 * GIB)) "staging arena            5 MiB"
check "unparsable config skips loudly"        0 $((125 * GIB)) $(( 60 * GIB)) "skipping the streaming preflight" TEST_CFG="$TMP/bad.json"
check "warning names the synthetic provenance" 0 $(( 32 * GIB)) $(( 14 * GIB)) "synthetic"

echo
echo "  passed=$pass  failed=$fail"
[ "$fail" = 0 ]

#!/usr/bin/env bash
# Package a Spark qualification run for hand-back.
# Usage: bash scripts/spark-report.sh <results-dir>
set -euo pipefail
RESULTS="${1:?Existing results directory}"
[[ -d "$RESULTS" ]] || { echo "no such results dir: $RESULTS" >&2; exit 2; }
RECIPE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
host="$(hostname -s 2>/dev/null || echo unknown)"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
info="$(mktemp -d)/host-info.txt"
{
  echo "== spark qualification host info ($stamp)"
  echo "-- git"; git -C "$RECIPE_DIR" rev-parse HEAD; git -C "$RECIPE_DIR" status --short | head -5
  echo "-- uname"; uname -a
  echo "-- os"; cat /etc/os-release 2>/dev/null | head -2
  echo "-- nvidia-smi"; nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>&1
  echo "-- nvidia-smi -q (power/xid)"; nvidia-smi -q 2>/dev/null | grep -iE "power limit|serial|pci" | head -6
  echo "-- dmesg nvidia tail"; dmesg -T 2>/dev/null | grep -iE "NVRM|Xid" | tail -10 || echo "(dmesg unavailable)"
  echo "-- memory"; free -g | head -2
  echo "-- disk"; df -h / | tail -1
  echo "-- runtime"; rt="$(find "$RESULTS" -name runtime.json 2>/dev/null | head -1)"; [[ -n "$rt" ]] && head -c 2000 "$rt" || echo "(no runtime.json)"
  echo "-- image"; (docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' 2>/dev/null | grep spark-exl3 || podman images 2>/dev/null | grep spark-exl3 || echo "(image list unavailable)") | head -3
} > "$info"
tarball="spark-report-${host}-${stamp}.tar.gz"
tar -czf "$tarball" -C "$(dirname "$RESULTS")" "$(basename "$RESULTS")" -C "$(dirname "$info")" "$(basename "$info")"
echo "report: $PWD/$tarball ($(du -h "$tarball" | cut -f1))"

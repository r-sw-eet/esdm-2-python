#!/usr/bin/env bash
# Smoke gate: regenerate every examples/* app with every target into a temp dir
# and fail on any generation error or a suspiciously empty tree (< 10 files).
# Mirrors the sibling generators' scripts/examples.sh. Run generator unit tests
# with `pytest`.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
TARGETS="${TARGETS:-django-eventsourcing-postgres django-eventsourcingdb}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0
for app in examples/*/; do
  [ -f "${app}esdmgen.yaml" ] || continue
  name="$(basename "$app")"
  for target in $TARGETS; do
    out="$TMP/$name-$target"
    if ! PYTHONPATH=src "$PYTHON" -m esdm2python.cli generate "$app" --target "$target" --out "$out" >/dev/null; then
      echo "FAIL: $name ($target) did not generate"; fail=1; continue
    fi
    count="$(find "$out" -type f | wc -l)"
    if [ "$count" -lt 10 ]; then
      echo "FAIL: $name ($target) produced only $count files"; fail=1; continue
    fi
    echo "ok: $name ($target, $count files)"
  done
done

exit "$fail"

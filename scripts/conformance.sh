#!/usr/bin/env bash
# Smoke gate: regenerate every examples/* app with every target into a temp dir,
# fail on any generation error or a suspiciously empty tree (< 10 files), then
# RUN each app's emitted write-side GWT tests (in-memory, no DB) so a scenario-
# emitter regression fails here. Mirrors the sibling generators' scripts/examples.sh.
# The generator's own unit tests run separately with `pytest`.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
# a path-style PYTHON (e.g. .venv/bin/python) must survive the cd into the temp dir
case "$PYTHON" in */*) PYTHON="$(cd "$(dirname "$PYTHON")" && pwd)/$(basename "$PYTHON")";; esac
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

    # Run the emitted write-side GWT tests. The esdb target's tests are pure
    # stdlib; the postgres target's need `eventsourcing`, so skip those (with a
    # note) when it is not importable, so the gate still runs dep-free.
    ini="$(find "$out" -name pytest.ini | head -1)"
    [ -n "$ini" ] && [ -d "$(dirname "$ini")/tests" ] || continue
    if [ "$target" = "django-eventsourcing-postgres" ] && ! "$PYTHON" -c "import eventsourcing" 2>/dev/null; then
      echo "  skip-tests: $name ($target) - 'eventsourcing' not installed"
      continue
    fi
    if ( cd "$(dirname "$ini")" && "$PYTHON" -m pytest tests -q >/dev/null 2>&1 ); then
      echo "  tests: $name ($target) passed"
    else
      echo "FAIL: $name ($target) emitted write-side tests failed"; fail=1
    fi
  done
done

exit "$fail"

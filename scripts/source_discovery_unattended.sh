#!/bin/zsh
set -u

ROOT="${LAND_DATA_ROOT:-/Users/mars/Downloads/LAND-DATA}"
DB="${LAND_DATA_DB:-$ROOT/.local/land-data.db}"
LOG_DIR="${LAND_DATA_LOG_DIR:-$ROOT/.local/launchd}"
LOCK_DIR="${LAND_DATA_LOCK_DIR:-$ROOT/.local/land-data-source-discovery.lock}"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/source-discovery.runs.jsonl"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  print -r -- '{"status":"SKIPPED_OVERLAP","reason":"lock_exists"}'
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM

if [[ -n "${LAND_DATA_PYTHON:-}" ]]; then
  PYTHON="$LAND_DATA_PYTHON"
elif [[ -x "$ROOT/.venv/bin/python" ]] && "$ROOT/.venv/bin/python" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  PYTHON="$ROOT/.venv/bin/python"
elif [[ -x /opt/homebrew/bin/python3 ]]; then
  PYTHON=/opt/homebrew/bin/python3
else
  PYTHON="$(command -v python3)"
fi
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

output=$(cd "$ROOT" && "$PYTHON" -m land_data --db "$DB" discover-sources \
  --discovery-config config/source-discovery.json 2>&1)
code=$?
print -r -- "$output" | tee -a "$RUN_LOG"
exit "$code"

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$SCRIPT_DIR"

if command -v uv >/dev/null 2>&1; then
  exec uv run --script "$SCRIPT_DIR/bridge_server.py" "$@"
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "[Bridge] Python not found: $PYTHON_BIN" >&2
  echo "[Bridge] Install uv or set PYTHON_BIN to a working python3." >&2
  exit 1
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/bridge_server.py" "$@"

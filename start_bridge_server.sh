#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  SELECTED_PYTHON="$PYTHON_BIN"
elif command -v python3.11 >/dev/null 2>&1; then
  SELECTED_PYTHON="python3.11"
else
  SELECTED_PYTHON="python3"
fi

cd "$SCRIPT_DIR"

if command -v uv >/dev/null 2>&1; then
  exec uv run --script "$SCRIPT_DIR/bridge_server.py" "$@"
fi

if ! command -v "$SELECTED_PYTHON" >/dev/null 2>&1; then
  echo "[Bridge] Python not found: $SELECTED_PYTHON" >&2
  echo "[Bridge] Install uv or set PYTHON_BIN to a working Python 3.11+ interpreter." >&2
  exit 1
fi

if ! "$SELECTED_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "[Bridge] bridge_server.py requires Python 3.11+ for styly-netsync-server." >&2
  echo "[Bridge] Install uv or set PYTHON_BIN to python3.11." >&2
  exit 1
fi

exec "$SELECTED_PYTHON" "$SCRIPT_DIR/bridge_server.py" "$@"

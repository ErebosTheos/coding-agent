#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Config (override via env or edit here) ────────────────────────────────────
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-7070}"
OUTPUT_DIR="${OUTPUT_DIR:-./output}"
INBOX_DIR="${INBOX_DIR:-./inbox}"

# ── 1. Check Python version ───────────────────────────────────────────────────
PYTHON=$(command -v python3 || command -v python || true)
if [[ -z "$PYTHON" ]]; then
  echo "ERROR: python3 not found in PATH" >&2
  exit 1
fi

PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$("$PYTHON" -c "import sys; print(sys.version_info.major)")
PY_MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")
if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 10 ]]; }; then
  echo "ERROR: Python 3.10+ required (found $PY_VERSION)" >&2
  exit 1
fi

# ── 2. Activate or create virtual environment ─────────────────────────────────
VENV_DIR="$SCRIPT_DIR/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating virtual environment..."
  "$PYTHON" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

# ── 3. Install / upgrade dependencies ─────────────────────────────────────────
echo "Checking dependencies..."
pip install --quiet --prefer-binary -e "$SCRIPT_DIR" 2>&1 | grep -v "already satisfied" || true

# ── 4. Check for .env ─────────────────────────────────────────────────────────
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
  echo ""
  echo "WARNING: .env not found."
  echo "  Copy .env.example to .env and fill in your API keys / provider config."
  echo "  The server will start, but LLM calls will fail without credentials."
  echo ""
else
  set -o allexport
  source "$SCRIPT_DIR/.env"
  set +o allexport
fi

# ── 5. Create required directories ────────────────────────────────────────────
mkdir -p "$OUTPUT_DIR" "$INBOX_DIR"

# ── 6. Launch ─────────────────────────────────────────────────────────────────
echo ""
echo "Starting Coding Agent dashboard..."
echo "  Dashboard  →  http://${HOST}:${PORT}"
echo "  Output dir →  $(realpath "$OUTPUT_DIR")"
echo "  Inbox dir  →  $(realpath "$INBOX_DIR")"
echo ""
echo "Press Ctrl+C to stop."
echo ""

exec python -m codegen_agent serve \
  --host "$HOST" \
  --port "$PORT" \
  --output-dir "$OUTPUT_DIR" \
  --inbox-dir "$INBOX_DIR"

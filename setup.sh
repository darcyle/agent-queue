#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- Detect Python 3.12+ ---
PYTHON=""
for candidate in python3.13 python3.12 python3 python; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
        major="${version%%.*}"
        minor="${version##*.}"
        if [[ "$major" == "3" && "$minor" -ge 12 ]]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "Error: Python 3.12+ is required but not found."
    echo "Install it from https://www.python.org/downloads/"
    exit 1
fi

echo "Using $PYTHON ($("$PYTHON" --version))"

# --- Create venv if needed ---
if [[ ! -d ".venv" ]]; then
    echo "Creating virtual environment..."
    "$PYTHON" -m venv .venv
fi

# --- Activate venv ---
source .venv/bin/activate

# --- Install project ---
echo "Installing agent-queue and dependencies..."
pip install -e ".[dev]" --quiet

# --- Run setup wizard ---
echo ""
python setup_wizard.py

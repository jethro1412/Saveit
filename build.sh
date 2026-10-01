#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================================"
echo "   Saveit Executable Builder (Linux / macOS)"
echo "========================================================"

if ! command -v python3 &> /dev/null; then
    echo "[Error] python3 is not installed or not in PATH."
    exit 1
fi

# Use virtual environment if present
if [ -d ".venv" ]; then
    PYTHON_BIN=".venv/bin/python"
    PIP_BIN=".venv/bin/pip"
else
    PYTHON_BIN="python3"
    PIP_BIN="pip3"
fi

echo "Verifying PyInstaller and dependencies..."
$PIP_BIN install -r requirements.txt --quiet --upgrade
$PIP_BIN install pyinstaller --quiet

echo "Running build.py with arguments: $@"
$PYTHON_BIN build.py "$@"

#!/usr/bin/env bash
# Start the Holon siren status app
# Usage: ./run.sh [port]        (default port: 5000)
set -e

PORT="${1:-5000}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Kill any existing instance
pkill -f "app.py" 2>/dev/null && echo "Stopped previous instance." || true
sleep 1

echo "================================================================"
echo "  מעקב התרעות צבע אדום — חולון"
echo "  Holon Red Alert monitor"
echo "  http://localhost:$PORT"
echo "================================================================"

# Try to find a usable uv/pip
UV="$HOME/.local/bin/uv"
if command -v uv &>/dev/null; then
    UV="uv"
fi

if command -v "$UV" &>/dev/null || [[ -x "$UV" ]]; then
    echo "Starting with uv on port $PORT..."
    "$UV" run --with flask --with requests --with pytz \
        python3 "$SCRIPT_DIR/app.py"
elif command -v pip3 &>/dev/null; then
    echo "Installing dependencies with pip3..."
    pip3 install -r "$SCRIPT_DIR/requirements.txt" -q
    python3 "$SCRIPT_DIR/app.py"
elif command -v pip &>/dev/null; then
    echo "Installing dependencies with pip..."
    pip install -r "$SCRIPT_DIR/requirements.txt" -q
    python3 "$SCRIPT_DIR/app.py"
else
    echo "ERROR: No pip or uv found. Install one of:"
    echo "  uv:  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "  pip: sudo apt install python3-pip"
    exit 1
fi

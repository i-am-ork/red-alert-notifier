#!/usr/bin/env bash
# Run the test suite with all required dependencies.
# Usage: ./run_tests.sh [-v|--verbose] [pytest args...]
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

UV="$HOME/.local/bin/uv"
if command -v uv &>/dev/null; then
    UV="uv"
fi

DEPS=(flask requests pytz pytest)

if command -v "$UV" &>/dev/null || [[ -x "$UV" ]]; then
    WITH_ARGS=()
    for dep in "${DEPS[@]}"; do
        WITH_ARGS+=(--with "$dep")
    done
    "$UV" run "${WITH_ARGS[@]}" pytest "$SCRIPT_DIR/tests/" "$@"
elif command -v pip3 &>/dev/null || command -v pip &>/dev/null; then
    PIP=$(command -v pip3 || command -v pip)
    echo "Installing dependencies with $PIP..."
    "$PIP" install "${DEPS[@]}" -q
    python3 -m pytest "$SCRIPT_DIR/tests/" "$@"
else
    echo "ERROR: No pip or uv found. Install one of:"
    echo "  uv:  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "  pip: sudo apt install python3-pip"
    exit 1
fi

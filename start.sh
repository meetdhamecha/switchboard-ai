#!/usr/bin/env bash
# Start Switchboard AI on Linux / macOS. First run: creates .venv, installs the
# package, then offers to install Claude Code and the Antigravity CLI if missing.
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
    echo "Creating virtual environment..."
    if ! python3 -m venv .venv; then
        echo "Setup failed. Install Python 3.10+ with venv (Ubuntu: sudo apt install python3-venv)."
        rm -rf .venv
        exit 1
    fi
    "$PY" -m pip install -q -e ".[api]"
    "$PY" -m switchboard_ai setup || true
fi

# Open the UI once the server has had a moment to start.
(sleep 3; "$PY" -m webbrowser -t http://localhost:8000/ >/dev/null 2>&1 || true) &
exec "$PY" -m switchboard_ai server "$@"

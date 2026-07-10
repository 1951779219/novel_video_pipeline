#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="$NO_PROXY"

if [ ! -x ".venv/bin/python" ]; then
  scripts/setup_macos.sh
fi

. .venv/bin/activate
python -m app.seedance_canvas serve --host 127.0.0.1 --port "${SEEDANCE_CANVAS_PORT:-7871}"

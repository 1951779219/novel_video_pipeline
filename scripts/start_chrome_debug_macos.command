#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ ! -x "$CHROME" ]; then
  CHROME="$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
fi
if [ ! -x "$CHROME" ]; then
  if [ ! -x ".venv/bin/python" ]; then
    scripts/setup_macos.sh
  fi
  CHROME="$(.venv/bin/python - <<'PY'
from pathlib import Path
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        path = Path(pw.chromium.executable_path)
    print(path if path.exists() else "")
except Exception:
    print("")
PY
)"
fi
if [ ! -x "$CHROME" ]; then
  echo "Google Chrome or Playwright Chromium was not found. Run scripts/setup_macos.sh first."
  exit 2
fi

PROFILE="$ROOT/data/chrome_debug_profile"
mkdir -p "$PROFILE"

exec "$CHROME" \
  --remote-debugging-port=9222 \
  --remote-allow-origins='*' \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  https://studio.youtube.com

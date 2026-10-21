#!/usr/bin/env bash
# Install one matching frontend/backend checkout. Never restarts running work.
set -euo pipefail

vwb_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
vwb_ui_only=false
vwb_agent=false
vwb_browser=false

usage() {
  cat <<'EOF'
Usage: ./scripts/setup.sh [--ui-only] [--with-agent] [--with-browser]

Default: frozen Python environment, Remotion dependencies/fonts/browser, readiness check.
  --ui-only       Python + bundled web UI, without installing the video renderer.
  --with-agent    Build the pinned Codex/sandbox Docker image (Docker must be running).
  --with-browser Install the checksum-pinned shared browser driver (Chrome also required).
  --help          Show this help. No services or paid agent/model calls are started.

Requires uv. For full rendering, use Node 22 or 24 with npm, ffmpeg and ffprobe.
Installs into this checkout; existing .env files and conversation data are preserved.
EOF
}

for vwb_arg in "$@"; do
  case "$vwb_arg" in
    --ui-only) vwb_ui_only=true ;;
    --with-agent) vwb_agent=true ;;
    --with-browser) vwb_browser=true ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$vwb_arg" >&2; usage >&2; exit 2 ;;
  esac
done

cd -- "$vwb_root"
if [[ -x .venv/bin/python ]]; then
  .venv/bin/python - "$vwb_root" <<'PY'
import fcntl
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
lock = Path(os.environ.get('VWB_RUNS_DIR', root / 'runs')) / '.web-server.lock'
if lock.exists():
    with lock.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Workbench is running. Finish or stop its turns and shut down the server before setup. No dependencies were changed.')
PY
fi

vwb_uv="$(command -v uv || true)"
if [[ -z "$vwb_uv" && -x "$HOME/.local/bin/uv" ]]; then
  vwb_uv="$HOME/.local/bin/uv"
fi
if [[ -z "$vwb_uv" ]]; then
  printf 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/\n' >&2
  exit 1
fi

if [[ "$vwb_ui_only" == false ]]; then
  for vwb_tool in node npm ffmpeg ffprobe; do
    if ! command -v "$vwb_tool" >/dev/null 2>&1; then
      printf 'Missing %s. See docs/SETUP.md, or use --ui-only for the bundled web UI.\n' "$vwb_tool" >&2
      exit 1
    fi
  done
fi
if [[ "$vwb_agent" == true ]] && ! command -v docker >/dev/null 2>&1; then
  printf 'Install and start Docker before using --with-agent.\n' >&2
  exit 1
fi

printf 'Installing locked Python dependencies…\n'
"$vwb_uv" sync --frozen

if [[ "$vwb_ui_only" == false ]]; then
  printf 'Installing locked renderer dependencies and local fonts…\n'
  (
    cd generators/remotion
    npm ci --no-audit --no-fund
    npm run setup-fonts
    ./node_modules/.bin/remotion browser ensure
  )
fi
if [[ "$vwb_agent" == true ]]; then
  docker build -t vwb-codex-agent:0.162.1 docker/codex-agent
fi
if [[ "$vwb_browser" == true ]]; then
  .venv/bin/python -m vwb.browser.runtime install
fi

if [[ -z "${VWB_ENV_FILE:-}" ]] && .venv/bin/python - <<'PY'
from vwb.config import find_env_file
raise SystemExit(0 if find_env_file() is None else 1)
PY
then
  (umask 077; cp .env.example .env)
  printf 'Created a private .env template. Add provider keys there or in Models and APIs.\n'
fi

if [[ "$vwb_ui_only" == true ]]; then
  .venv/bin/vwb doctor --ui-only
else
  .venv/bin/vwb doctor
fi
printf '\nSetup complete. Start both frontend and backend with:\n  .venv/bin/vwb ui\n\nSee docs/SETUP.md for model selection, remote access, updates and rollback.\n'

#!/bin/zsh
set -euo pipefail

APP_DIR="${0:A:h}"
PYTHON_BIN="$APP_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  osascript -e 'display dialog "没有找到 Python 3。请先安装 Python 3，并按启动说明安装依赖。" buttons {"好"} default button "好" with icon caution' >/dev/null 2>&1 || true
  exit 1
fi

STATE_PATH="$APP_DIR/.workbench"
PROFILE_PATH="${WORKBENCH_SOCIAL_PROFILE_DIR:-$STATE_PATH/social_browser}"
PORT="${WORKBENCH_PORT:-5191}"
CDP_PORT="${WORKBENCH_SOCIAL_CDP_PORT:-9230}"
PROXY_PORT="${WORKBENCH_LOCAL_PROXY_PORT:-8789}"
URL="http://127.0.0.1:${PORT}/"

export WORKBENCH_STATE_DIR="$STATE_PATH"
export WORKBENCH_SOCIAL_PROFILE_DIR="$PROFILE_PATH"
export WORKBENCH_SOCIAL_CDP_PORT="$CDP_PORT"
export WORKBENCH_PORT="$PORT"
export WORKBENCH_LOCAL_PROXY_PORT="$PROXY_PORT"
export WORKBENCH_LOCAL_PROXY_URL="http://127.0.0.1:${PROXY_PORT}/v1"
cd "$APP_DIR"
exec "$PYTHON_BIN" scripts/launch_workbench.py

#!/bin/zsh
set -e

RESOURCES_DIR=${0:A:h}
APP_DIR="$RESOURCES_DIR/coursepilot"
SUPPORT_DIR="$HOME/Library/Application Support/DKU CoursePilot"
RUNTIME_DIR="$SUPPORT_DIR/runtime"
LOG_FILE="$SUPPORT_DIR/server.log"
mkdir -p "$SUPPORT_DIR"

if /usr/bin/curl -fsS http://127.0.0.1:5000/ | /usr/bin/grep -q "DKU CoursePilot"; then
  open http://127.0.0.1:5000
  exit 0
fi

if python3 -c "import flask, requests" >/dev/null 2>&1; then
  PYTHON_BIN=$(command -v python3)
else
  if [[ ! -x "$RUNTIME_DIR/bin/python" ]]; then
    python3 -m venv "$RUNTIME_DIR"
    "$RUNTIME_DIR/bin/python" -m pip install --upgrade pip >>"$LOG_FILE" 2>&1
    "$RUNTIME_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.txt" >>"$LOG_FILE" 2>&1
  fi
  PYTHON_BIN="$RUNTIME_DIR/bin/python"
fi

export DKU_PLANNER_DB_PATH="$SUPPORT_DIR/planner.db"
cd "$APP_DIR"
"$PYTHON_BIN" app.py >>"$LOG_FILE" 2>&1 &
SERVER_PID=$!

for ATTEMPT in {1..80}; do
  if /usr/bin/curl -fsS http://127.0.0.1:5000/ | /usr/bin/grep -q "DKU CoursePilot"; then
    open http://127.0.0.1:5000
    exit 0
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    osascript -e 'display dialog "DKU CoursePilot could not start. See ~/Library/Application Support/DKU CoursePilot/server.log for details." buttons {"OK"} default button "OK" with icon stop'
    exit 1
  fi
  sleep 0.25
done

osascript -e 'display dialog "DKU CoursePilot is taking longer than expected to start. Please try again in a moment." buttons {"OK"} default button "OK" with icon caution'
exit 1

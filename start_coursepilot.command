#!/bin/zsh
set -e

SCRIPT_DIR=${0:A:h}
cd "$SCRIPT_DIR"

if /usr/bin/curl -fsS http://127.0.0.1:5000/ | /usr/bin/grep -q "DKU CoursePilot"; then
  open http://127.0.0.1:5000
  exit 0
fi

if [[ ! -x .coursepilot_venv/bin/python ]]; then
  python3 -m venv .coursepilot_venv
  .coursepilot_venv/bin/python -m pip install --upgrade pip
  .coursepilot_venv/bin/python -m pip install -r requirements.txt
fi

.coursepilot_venv/bin/python app.py &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT INT TERM
for ATTEMPT in {1..60}; do
  if /usr/bin/curl -fsS http://127.0.0.1:5000/ | /usr/bin/grep -q "DKU CoursePilot"; then
    open http://127.0.0.1:5000
    wait "$SERVER_PID"
    exit $?
  fi
  sleep 0.25
done
wait "$SERVER_PID"

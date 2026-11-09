#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONTROLLER_DIR="$PROJECT_DIR/controller"
SERVER_URL="http://localhost:8080"

echo "Starting controller server..."
gnome-terminal --tab --title="Controller Server" -- bash -c "
    cd '$CONTROLLER_DIR' || exit 1
    exec uv run python server.py
"

sleep 2

echo "Opening browser at $SERVER_URL ..."
firefox "$SERVER_URL" > /dev/null 2>&1 &
disown

echo "Launching RViz2..."
gnome-terminal --tab --title="RViz" -- bash -c "
    bash '$SCRIPT_DIR/start_rviz.sh'; exec bash
"

echo "Launching nvtop"
gnome-terminal --tab --title="nvtop" -- bash -c "
    nvtop; exec bash
"

echo "Launching btop"
gnome-terminal --tab --title="btop" -- bash -c "
    btop; exec bash
"

echo "All components started. Closing this tab..."
sleep 1
kill -9 $PPID

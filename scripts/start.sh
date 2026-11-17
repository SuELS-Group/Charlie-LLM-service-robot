#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONTROLLER_DIR="$PROJECT_DIR/controller"
CONTROLLER_LOG="$CONTROLLER_DIR/config/logs/controller-startup.log"
SERVER_URL="http://localhost:8080"

echo "Starting controller server..."
mkdir -p "$(dirname "$CONTROLLER_LOG")"
gnome-terminal --tab --title="Controller Server" -- bash -c '
    set -o pipefail
    cd "$1" || exit 1
    uv run python -u server.py 2>&1 | tee -a "$2"
    server_status=${PIPESTATUS[0]}
    printf "\nController stopped (exit status %s). Log: %s\n" "$server_status" "$2"
    exec bash
' bash "$CONTROLLER_DIR" "$CONTROLLER_LOG"

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

echo "All components launched. Check the Controller Server tab for startup progress."

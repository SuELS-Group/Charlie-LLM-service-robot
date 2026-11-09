#!/usr/bin/env bash
# Use the controller's local discovery server, including full graph discovery.
set -eo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
source /opt/ros/humble/setup.bash
if [[ -f "$PROJECT_DIR/workstation/ament_ws/install/setup.bash" ]]; then
    source "$PROJECT_DIR/workstation/ament_ws/install/setup.bash"
fi
source "$SCRIPT_DIR/ros_tf2_fix.bash"
pg_source_tf2_fix "$PROJECT_DIR"
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_SUPER_CLIENT=TRUE
export ROS_LOCALHOST_ONLY=0
export FASTDDS_DEFAULT_PROFILES_FILE="$PROJECT_DIR/controller/config/fastdds_super_client_profiles.xml"
# Humble's Fast DDS 2.6 reads the legacy name; newer versions use FASTDDS.
# Override both so an inherited production profile cannot take precedence.
export FASTRTPS_DEFAULT_PROFILES_FILE="$FASTDDS_DEFAULT_PROFILES_FILE"
# The controller binds locally; do not rely on its runtime advertised LAN IP.
if [[ -z "${ROS_DISCOVERY_SERVER:-}" ]]; then
    ROS_DISCOVERY_SERVER="$(python3 - "$PROJECT_DIR/controller/config/dds_discovery_servers.json" <<'PY'
import json
import sys
with open(sys.argv[1]) as stream:
    config = json.load(stream)
print(f"127.0.0.1:{int(config['port'])}")
PY
)"
    export ROS_DISCOVERY_SERVER
fi
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-42}"
exec ros2 run rviz2 rviz2 -d "$PROJECT_DIR/workstation/rviz_configs/remote_nav.rviz" "$@"

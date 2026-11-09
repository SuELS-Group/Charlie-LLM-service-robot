#!/usr/bin/env bash
# Robot workspace build & run script
# Works both sourced (interactive) and executed directly (systemd)
#
# Usage:
#   source update_robot_ws.sh [node_name]          # legacy interactive mode
#   ./update_robot_ws.sh --build-only               # rebuild all packages
#   ./update_robot_ws.sh --build-only --packages depth_mapping_node,camera_node
#   ./update_robot_ws.sh --run --type node --command "ros2 run depth_mapping_node depth_mapping_node"
#   ./update_robot_ws.sh --run --type launch --command "ros2 launch command_node bringup_all.launch.py"
#   ./update_robot_ws.sh --build-and-run --type node --command "ros2 run ..."
#   ./update_robot_ws.sh --build-and-run --packages pkg1,pkg2 --type launch --command "ros2 launch ..."

# Resolve paths relative to this script's location, never rely on $HOME
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ROBOT_DIR="$PROJECT_ROOT/robot"
WS_PATH="$ROBOT_DIR/ros_ws"
AMENT_PATH="$HOME/ament_ws"
UV_VENV="$ROBOT_DIR/.venv"
source "$SCRIPT_DIR/ros_interface_build_helpers.bash"
source "$SCRIPT_DIR/ros_tf2_fix.bash"

# Keep $HOME sane for tools that need it (e.g. colcon, ros2)
if [[ -z "$HOME" ]]; then
  export HOME="$(getent passwd "$(id -un)" | cut -d: -f6)"
fi

if [[ $EUID -eq 0 ]]; then
  echo "ERROR: Do not run this script as root." >&2
  if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    exit 1
  else
    return 1
  fi
fi

_robot_setup_venv() {
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if ! command -v uv &> /dev/null; then
    echo "ERROR: uv is not installed." >&2
    return 1
  fi
  cd "$ROBOT_DIR" || return 1
  local uv_flags=""
  [[ ! -t 1 ]] && uv_flags="--no-progress"
  if [[ ! -d ".venv" ]]; then
    uv venv $uv_flags --python 3.10
  fi
  uv sync $uv_flags
  source "$UV_VENV/bin/activate"
}

_robot_setup_ros_env() {
  unset AMENT_PREFIX_PATH
  unset CMAKE_PREFIX_PATH
  unset COLCON_PREFIX_PATH
  source /opt/ros/humble/setup.bash
  # The upstream workspace was once built while this project overlay was
  # sourced, so its setup.bash chains back into this workspace. Source only
  # its local packages to keep selected builds free of stale self-underlays.
  if [[ -f "$AMENT_PATH/install/local_setup.bash" ]]; then
    source "$AMENT_PATH/install/local_setup.bash"
  else
    source "$AMENT_PATH/install/setup.bash"
  fi
  pg_source_tf2_fix "$PROJECT_ROOT" || return 1
}

_robot_source_project_overlay() {
  if [[ -f "$WS_PATH/install/local_setup.bash" ]]; then
    source "$WS_PATH/install/local_setup.bash"
  elif [[ -f "$WS_PATH/install/setup.bash" ]]; then
    source "$WS_PATH/install/setup.bash"
  fi
}

_robot_source_hello_env() {
  if [[ -f /etc/hello-robot/hello-robot.conf ]]; then
    source /etc/hello-robot/hello-robot.conf
  fi
  export HELLO_FLEET_PATH="${HELLO_FLEET_PATH:-$HOME/stretch_user}"
  export HELLO_FLEET_ID="${HELLO_FLEET_ID:-stretch}"
  export LRS_LOG_LEVEL="${LRS_LOG_LEVEL:-None}"
}

_robot_build() {
  local packages="$1"
  local lock_file="${TMPDIR:-/tmp}/pg_llm_service_robot_robot_ros_ws.build.lock"

  local _had_venv=""
  if [[ -n "$VIRTUAL_ENV" ]]; then
    _had_venv="$VIRTUAL_ENV"
    deactivate 2>/dev/null || true
  fi

  mkdir -p "$WS_PATH" || return 1

  {
    flock 9

    cd "$WS_PATH" || return 1

    if [[ -n "$packages" ]]; then
      IFS=',' read -ra _pkgs <<< "$packages"
      pg_prepare_interface_packages "$WS_PATH" "${_pkgs[@]}"
      colcon build --cmake-clean-cache --packages-select "${_pkgs[@]}"
      pg_record_interface_fingerprints "$WS_PATH"
    else
      rm -rf "$WS_PATH/install" "$WS_PATH/build" "$WS_PATH/log"
      colcon build --cmake-clean-cache
    fi
  } 9>"$lock_file"

  if [[ -n "$_had_venv" ]]; then
    source "$_had_venv/bin/activate"
  fi
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  set -e

  _MODE=""
  _PACKAGES=""
  _COMMAND=""
  _ROS_DOMAIN_ID=""
  _VENV=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --build-only)    _MODE="build"; shift ;;
      --run)           _MODE="run"; shift ;;
      --build-and-run) _MODE="build-and-run"; shift ;;
      --packages)      _PACKAGES="$2"; shift 2 ;;
      --command)       _COMMAND="$2"; shift 2 ;;
      --ros-domain-id) _ROS_DOMAIN_ID="$2"; shift 2 ;;
      --venv)          _VENV="$2"; shift 2 ;;
      --type)          shift 2 ;;
      *) shift ;;
    esac
  done

  case "$_MODE" in
    build)
      pg_install_tf2_fix "$PROJECT_ROOT"
      _robot_setup_ros_env
      _robot_build "$_PACKAGES"
      ;;
    run)
      _robot_source_hello_env
      if [[ -n "$_VENV" ]]; then
        source "$_VENV/bin/activate"
      fi
      _robot_setup_ros_env
      _robot_source_project_overlay || true
      pg_source_tf2_fix "$PROJECT_ROOT"
      if [[ -n "$_ROS_DOMAIN_ID" ]]; then
        export ROS_DOMAIN_ID="$_ROS_DOMAIN_ID"
      fi
      exec bash -c "$_COMMAND"
      ;;
    build-and-run)
      _robot_source_hello_env
      _robot_setup_venv
      pg_install_tf2_fix "$PROJECT_ROOT"
      _robot_setup_ros_env
      _robot_build "$_PACKAGES"
      _robot_source_project_overlay
      pg_source_tf2_fix "$PROJECT_ROOT"
      if [[ -n "$_ROS_DOMAIN_ID" ]]; then
        export ROS_DOMAIN_ID="$_ROS_DOMAIN_ID"
      fi
      exec bash -c "$_COMMAND"
      ;;
    *)
      echo "ERROR: Specify --build-only, --run, or --build-and-run" >&2
      exit 1
      ;;
  esac
else
  _ORIG_PWD="$PWD"
  _NODE_NAME="$1"

  _robot_setup_venv || return 1
  pg_install_tf2_fix "$PROJECT_ROOT" || return 1
  _robot_setup_ros_env || return 1
  _robot_build "$_NODE_NAME"
  _robot_source_project_overlay
  pg_source_tf2_fix "$PROJECT_ROOT" || return 1
  cd "$_ORIG_PWD" || return 1

  echo "==> Build complete. Virtual environment is active."

  if [[ -n "$_NODE_NAME" ]]; then
    ros2 run "$_NODE_NAME" "$_NODE_NAME"
  fi

  unset _ORIG_PWD
  unset _NODE_NAME
  unset WS_PATH
  unset PROJECT_ROOT
  unset ROBOT_DIR
  unset AMENT_PATH
  unset UV_VENV
fi

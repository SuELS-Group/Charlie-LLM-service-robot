#!/usr/bin/env bash
# Workstation workspace build & run script
# Works both sourced (interactive) and executed directly (systemd)
#
# Usage:
#   source update_workstation_ws.sh [node_name]     # legacy interactive mode
#   ./update_workstation_ws.sh --build-only          # rebuild all packages
#   ./update_workstation_ws.sh --build-only --packages vision_node,command_node
#   ./update_workstation_ws.sh --run --command "ros2 run vision_node vision_node"
#   ./update_workstation_ws.sh --run --type launch --command "ros2 launch command_node bringup_all.launch.py"
#   ./update_workstation_ws.sh --build-and-run --packages pkg1,pkg2 --command "ros2 run ..."

# Resolve paths relative to this script's location, never rely on $HOME
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKSTATION_DIR="$PROJECT_ROOT/workstation"
WS_PATH="$WORKSTATION_DIR/ament_ws"
UV_VENV="$WORKSTATION_DIR/.venv"
export PROJECT_PATH="$PROJECT_ROOT"
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

_ws_setup_venv() {
  local target_venv="${1:-$UV_VENV}"

  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if ! command -v uv &> /dev/null; then
    echo "ERROR: uv is not installed." >&2
    return 1
  fi
  cd "$WORKSTATION_DIR" || return 1
  local uv_flags=""
  [[ ! -t 1 ]] && uv_flags="--no-progress"

  if [[ ! -f "$target_venv/bin/activate" ]]; then
    if [[ "$target_venv" == "$UV_VENV" ]]; then
      uv venv $uv_flags --python 3.10
    else
      uv venv $uv_flags --python 3.10 "$target_venv"
    fi
  fi

  source "$target_venv/bin/activate"
  uv sync --active $uv_flags
}

_ws_setup_ros_env() {
  unset AMENT_PREFIX_PATH
  unset CMAKE_PREFIX_PATH
  unset COLCON_PREFIX_PATH
  source /opt/ros/humble/setup.bash
  pg_source_tf2_fix "$PROJECT_ROOT" || return 1
}

_ws_setup_runtime_libs() {
  local venv_path="${1:-$UV_VENV}"
  local onnx_capi_dir="$venv_path/lib/python3.10/site-packages/onnxruntime/capi"

  if [[ -d "$onnx_capi_dir" ]]; then
    if [[ ! -e "$onnx_capi_dir/libonnxruntime.so" ]]; then
      local versioned_lib=""
      versioned_lib="$(find "$onnx_capi_dir" -maxdepth 1 -type f -name 'libonnxruntime.so.*' | sort | head -n 1)"
      if [[ -n "$versioned_lib" ]]; then
        ln -sf "$(basename "$versioned_lib")" "$onnx_capi_dir/libonnxruntime.so"
      fi
    fi

    case ":${LD_LIBRARY_PATH:-}:" in
      *":$onnx_capi_dir:"*) ;;
      *) export LD_LIBRARY_PATH="$onnx_capi_dir${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ;;
    esac
  fi
}

_ws_build() {
  local packages="$1"
  local target_venv="${2:-$UV_VENV}"
  local lock_file="${TMPDIR:-/tmp}/pg_llm_service_robot_workstation_ament_ws.build.lock"

  local _had_venv=""
  if [[ -n "$VIRTUAL_ENV" ]]; then
    _had_venv="$VIRTUAL_ENV"
    if [[ "$VIRTUAL_ENV" != "$target_venv" ]]; then
      deactivate 2>/dev/null || true
    fi
  fi

  if [[ -f "$target_venv/bin/activate" ]]; then
    source "$target_venv/bin/activate"
  fi
  _ws_setup_runtime_libs "$target_venv"

  mkdir -p "$WS_PATH" || return 1

  {
    flock 9

    cd "$WS_PATH" || return 1

    if [[ -n "$packages" ]]; then
      IFS=',' read -ra _pkgs <<< "$packages"
      pg_prepare_interface_packages "$WS_PATH" "${_pkgs[@]}"
      colcon build --cmake-clean-cache --packages-select "${_pkgs[@]}"
      pg_record_interface_fingerprints "$WS_PATH"
      _ws_fix_ros2_exec "${_pkgs[@]}"
    else
      rm -rf "$WS_PATH/install" "$WS_PATH/build" "$WS_PATH/log"
      colcon build --cmake-clean-cache
      _ws_fix_ros2_exec
    fi
  } 9>"$lock_file"

  if [[ -n "$_had_venv" && "$_had_venv" != "$target_venv" ]]; then
    source "$_had_venv/bin/activate"
  fi
}

_ws_fix_ros2_exec() {
  local pkgs=("$@")
  if [[ ${#pkgs[@]} -eq 0 ]]; then
    pkgs=("web_bridge_node" "voice_identity_node")
  fi

  for pkg in "${pkgs[@]}"; do
    local prefix="$WS_PATH/install/$pkg"
    local bin_dir="$prefix/bin"
    local lib_exec_dir="$prefix/lib/$pkg"

    if [[ ! -d "$bin_dir" ]]; then
      continue
    fi

    mkdir -p "$lib_exec_dir"
    local bin_exec=""
    for bin_exec in "$bin_dir"/*; do
      if [[ -x "$bin_exec" && ! -d "$bin_exec" ]]; then
        ln -sf "$bin_exec" "$lib_exec_dir/$(basename "$bin_exec")"
      fi
    done
  done
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  set -e

  _MODE=""
  _PACKAGES=""
  _COMMAND=""
  _ROS_DOMAIN_ID=""
  _VENV=""
  _WORKING_DIR=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --build-only)    _MODE="build"; shift ;;
      --run)           _MODE="run"; shift ;;
      --build-and-run) _MODE="build-and-run"; shift ;;
      --packages)      _PACKAGES="$2"; shift 2 ;;
      --command)       _COMMAND="$2"; shift 2 ;;
      --ros-domain-id) _ROS_DOMAIN_ID="$2"; shift 2 ;;
      --venv)          _VENV="$2"; shift 2 ;;
      --working-dir)   _WORKING_DIR="$2"; shift 2 ;;
      --type)          shift 2 ;;
      *) shift ;;
    esac
  done

  _run_command() {
    if [[ -n "$_VENV" ]]; then
      source "$_VENV/bin/activate"
      _ws_setup_runtime_libs "$_VENV"
    else
      source "$UV_VENV/bin/activate"
      _ws_setup_runtime_libs "$UV_VENV"
    fi
    _ws_setup_ros_env
    if [[ -f "$WS_PATH/install/setup.bash" ]]; then
      source "$WS_PATH/install/setup.bash"
    fi
    pg_source_tf2_fix "$PROJECT_ROOT"
    if [[ -n "$_ROS_DOMAIN_ID" ]]; then
      export ROS_DOMAIN_ID="$_ROS_DOMAIN_ID"
    fi
    if [[ -n "$_WORKING_DIR" ]]; then
      cd "$_WORKING_DIR"
    fi
    exec bash -c "$_COMMAND"
  }

  case "$_MODE" in
    build)
      pg_install_tf2_fix "$PROJECT_ROOT"
      _ws_setup_venv "${_VENV:-$UV_VENV}"
      _ws_setup_ros_env
      _ws_build "$_PACKAGES" "${_VENV:-$UV_VENV}"
      ;;
    run)
      _run_command
      ;;
    build-and-run)
      pg_install_tf2_fix "$PROJECT_ROOT"
      _ws_setup_venv "${_VENV:-$UV_VENV}"
      _ws_setup_ros_env
      _ws_build "$_PACKAGES" "${_VENV:-$UV_VENV}"
      _run_command
      ;;
    *)
      echo "ERROR: Specify --build-only, --run, or --build-and-run" >&2
      exit 1
      ;;
  esac
else
  _ORIG_PWD="$PWD"
  _NODE_NAME="$1"

  pg_install_tf2_fix "$PROJECT_ROOT" || return 1
  _ws_setup_venv || return 1
  _ws_setup_runtime_libs "$UV_VENV"
  _ws_setup_ros_env || return 1
  _ws_build "$_NODE_NAME"
  if [[ -f "$WS_PATH/install/setup.bash" ]]; then
    source "$WS_PATH/install/setup.bash" || true
  fi
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
  unset WORKSTATION_DIR
  unset UV_VENV
fi

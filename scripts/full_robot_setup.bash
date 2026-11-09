#!/usr/bin/env bash

# =============================================================================
# full_robot_setup.bash - Stretch 3 / Ubuntu 22.04 / ROS 2 Humble setup
# =============================================================================
# Restore this robot's complete stretch-se3-XXXX backup to $HOME before running.
# The official installer needs it BEFORE installing udev rules or robot software.
# https://docs-arch.hello-robot.com/0.3/installation/robot_install/
#
# Usage: bash scripts/full_robot_setup.bash [--check-only] [--fleet-id ID]
#        bash scripts/full_robot_setup.bash --post-install [--status]
# Run as the normal Ubuntu user, outside any Python/Conda environment.
# The official installer is interactive and needs sudo, internet, and a charger.

# ROS setup files are not nounset-safe.
set -Ee -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ROBOT_DIR="$PROJECT_ROOT/robot"
AMENT_WS="$HOME/ament_ws"
ROBOT_WS="$ROBOT_DIR/ros_ws"
SYSTEM_CONFIG=/etc/hello-robot/hello-robot.conf
STATE_DIR="$HOME/.local/state/pg-llm-service-robot"
STEP_COUNT=0
TOTAL_STEPS=9
DDS_NETWORK_SKIPPED=false
CHECK_ONLY=false
FLEET_ID=""

RED='' GREEN='' YELLOW='' BLUE='' CYAN='' NC='' BOLD=''
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    RED='\033[0;31m' GREEN='\033[0;32m' YELLOW='\033[1;33m'
    BLUE='\033[0;34m' CYAN='\033[0;36m' NC='\033[0m' BOLD='\033[1m'
fi
log_info() { printf '%b[INFO]%b %s\n' "$BLUE" "$NC" "$*"; }
log_success() { printf '%b[SUCCESS]%b %s\n' "$GREEN" "$NC" "$*"; }
log_warning() { printf '%b[WARNING]%b %s\n' "$YELLOW" "$NC" "$*"; }
log_error() { printf '%b[ERROR]%b %s\n' "$RED" "$NC" "$*" >&2; }
log_substep() { printf '  %b→%b %s\n' "$BLUE" "$NC" "$*"; }
log_step() {
    STEP_COUNT=$((STEP_COUNT + 1))
    printf '\n%b[Step %s/%s]%b %b%s%b\n' "$CYAN" "$STEP_COUNT" "$TOTAL_STEPS" "$NC" "$BOLD" "$*" "$NC"
    printf '%s\n' '----------------------------------------------'
}
die() { log_error "$*"; exit 1; }
on_error() {
    local rc="$1" line="$2"
    log_error "Setup failed at line $line (exit $rc). See ${LOG_FILE:-the output above}."
    exit "$rc"
}

show_help() {
    cat <<'EOF'
Usage: bash scripts/full_robot_setup.bash [--check-only] [--fleet-id stretch-se3-XXXX]

  --check-only   Validate prerequisites without installing or changing anything.
  --fleet-id ID  Select a backup when several stretch-se3-XXXX directories exist.
  --post-install [--status]
                Run the guided hardware setup, or inspect its status without changes.
                Use this option alone (with optional --status).
  --help        Show this help.

Fresh install: copy the complete backed-up stretch-se3-XXXX directory directly
into your home directory, using THIS robot's serial number. Do not rename a
different robot's backup. Keep the original USB/off-robot backup.
Existing install: use the fleet ID in /etc/hello-robot/hello-robot.conf and the
calibration in ~/stretch_user/<fleet-id> (or the system backup if missing).
EOF
}

calibration_help() {
    log_error "Robot calibration is required BEFORE the Hello Robot installation."
    cat >&2 <<EOF
Copy this robot's complete backed-up stretch-se3-XXXX directory to:
  $HOME/stretch-se3-XXXX/
Replace XXXX with the robot's actual four-digit serial number.

Example (replace USBKEY and XXXX):
  cp -a /media/\$USER/USBKEY/stretch-se3-XXXX "$HOME/"

Required contents include:
  stretch_configuration_params.yaml, stretch_user_params.yaml
  udev/*.rules
  calibration_ros/head_calibrated*.urdf
  calibration_ros/controller_calibration_head_*.yaml
  calibration_steppers/hello-motor-*.yaml (checked before firmware updates)

Then re-run:
  bash "$SCRIPT_DIR/full_robot_setup.bash"

Keep the original backup: the upstream installer consumes the copy in HOME.
If the backup is unavailable, contact Hello Robot support for this robot's data.
Guide: https://docs-arch.hello-robot.com/0.3/installation/robot_install/
EOF
}

# Check data before sudo, apt, downloads, or any hardware-specific changes.
validate_calibration() {
    local directory="$1" file
    for file in stretch_configuration_params.yaml stretch_user_params.yaml; do
        [[ -r "$directory/$file" && -s "$directory/$file" ]] || {
            log_error "Missing, empty, or unreadable: $directory/$file"
            return 1
        }
    done
    # These are the filenames used by upstream update_with_most_recent_calibration.
    /usr/bin/python3 - "$directory" <<'PY'
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

directory = pathlib.Path(sys.argv[1])
try:
    rules = list((directory / "udev").glob("*.rules"))
    if not rules or any(not p.is_file() or not p.read_bytes() for p in rules):
        raise ValueError("missing or empty udev/*.rules")
    for pattern in ("controller_calibration_head_*.yaml", "head_calibrated*.urdf"):
        paths = list((directory / "calibration_ros").glob(pattern))
        if not paths:
            raise ValueError(f"missing calibration_ros/{pattern}")
        # Upstream parses every filename's final 14 digits as a timestamp.
        if any(not re.search(r"\d{14}$", p.stem) for p in paths):
            raise ValueError(f"invalid timestamp in calibration_ros/{pattern}")
        latest = max(paths, key=lambda p: p.stem[-14:])
        if not latest.read_bytes():
            raise ValueError(f"empty calibration: {latest}")
        if latest.suffix == ".urdf" and ET.parse(latest).getroot().tag != "robot":
            raise ValueError(f"invalid robot URDF: {latest}")
except (OSError, ValueError, ET.ParseError) as exc:
    sys.exit(f"[ERROR] Incomplete calibration backup {directory}: {exc}")
PY
}

select_calibration() {
    local backup_root="${1:-$HOME}" user_root="${2:-$HOME/stretch_user}"
    local installed_id candidate
    local -a candidates=()
    if [[ -f "$SYSTEM_CONFIG" ]]; then
        installed_id=$(bash -c 'source "$1"; printf "%s" "${HELLO_FLEET_ID:-}"' bash "$SYSTEM_CONFIG")
        [[ "$installed_id" =~ ^stretch-se3-[0-9]{4}$ ]] || die "Invalid Stretch 3 fleet ID in $SYSTEM_CONFIG"
        [[ -z "$FLEET_ID" || "$FLEET_ID" == "$installed_id" ]] || die "Requested fleet ID differs from $SYSTEM_CONFIG"
        FLEET_ID="$installed_id"
        CALIBRATION_DIR="$user_root/$FLEET_ID"
        [[ -d "$CALIBRATION_DIR" ]] || CALIBRATION_DIR="$(dirname "$SYSTEM_CONFIG")/$FLEET_ID"
    else
        if [[ -z "$FLEET_ID" ]]; then
            for candidate in "$backup_root"/stretch-se3-[0-9][0-9][0-9][0-9]; do
                [[ -d "$candidate" ]] && candidates+=("$candidate")
            done
            if (( ${#candidates[@]} > 1 )); then
                die "Multiple calibration backups found. Select this robot with --fleet-id stretch-se3-XXXX."
            elif (( ${#candidates[@]} == 1 )); then
                FLEET_ID="$(basename "${candidates[0]}")"
            else
                calibration_help
                exit 1
            fi
        fi
        CALIBRATION_DIR="$backup_root/$FLEET_ID"
    fi
    if ! validate_calibration "$CALIBRATION_DIR"; then
        calibration_help
        exit 1
    fi
    log_success "Calibration backup found: $CALIBRATION_DIR"
    log_info "Fleet ID: $FLEET_ID (verify it matches the serial number on this robot)"
}

validate_parameters() {
    /usr/bin/python3 - "$CALIBRATION_DIR" "$FLEET_ID" <<'PY'
import pathlib
import sys
import yaml

directory = pathlib.Path(sys.argv[1])
for filename in ("stretch_configuration_params.yaml", "stretch_user_params.yaml"):
    with (directory / filename).open() as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        sys.exit(f"[ERROR] {filename} must contain a YAML mapping")
    robot = data.get("robot", {})
    if robot.get("model_name", "SE3") != "SE3":
        sys.exit(f"[ERROR] {filename} belongs to a different robot model")
    if str(robot.get("serial_no", sys.argv[2])) != sys.argv[2]:
        sys.exit(f"[ERROR] Robot serial number in {filename} differs from {sys.argv[2]}")
PY
}

preserve_path() {
    local path="$1" backup
    if [[ -e "$path" || -L "$path" ]]; then
        backup="${path}.before-robot-setup.$(date +%Y%m%dT%H%M%S).$$"
        mv -- "$path" "$backup"
        log_info "Preserved existing files at $backup"
    fi
}

prepare_nodesource_install() {
    local package
    local -a conflicts=()
    # Ubuntu's Node 12 development/runtime packages own files also shipped in
    # NodeSource's nodejs package. Upstream installs NodeSource but does not remove
    # these packages, causing dpkg to fail on common.gypi or node.stp.
    for package in libnode-dev libnode72; do
        if [[ "$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null)" == 'install ok installed' ]]; then
            conflicts+=("$package")
        fi
    done
    if (( ${#conflicts[@]} )); then
        log_substep "Removing Ubuntu Node packages that conflict with the official NodeSource installation: ${conflicts[*]}"
        sudo apt-get remove -y "${conflicts[@]}"
    fi
}

ensure_download_dns() {
    if timeout 10s getent ahostsv4 files.pythonhosted.org >/dev/null; then
        return 0
    fi
    # Reapplying a static NetworkManager profile without DNS can discard an old
    # DHCP resolver. Repair only that case, and only with a tested local gateway.
    local gateway interface connection method configured_dns answers
    read -r gateway interface < <(ip -4 route show default | awk '
        $1 == "default" && $2 == "via" {
            for (i = 1; i <= NF; i++) if ($i == "dev") {print $3, $(i + 1); exit}
        }') || die "No default route; restore internet connectivity and re-run."
    connection=$(nmcli -g GENERAL.CONNECTION device show "$interface")
    method=$(nmcli -g ipv4.method connection show "$connection")
    configured_dns=$(nmcli -g ipv4.dns connection show "$connection")
    [[ "$method" == manual && -z "$configured_dns" ]] || die "DNS cannot resolve files.pythonhosted.org. Check the DNS settings of $connection and re-run."
    answers=$(dig +time=3 +tries=1 +short "@$gateway" files.pythonhosted.org A) || die "DNS failed, and gateway $gateway does not provide a working DNS service."
    grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' <<< "$answers" || die "Gateway $gateway did not resolve the package server. Configure a working DNS server and re-run."
    log_warning "Static connection $connection has no IPv4 DNS; using its verified gateway $gateway."
    sudo nmcli connection modify "$connection" ipv4.dns "$gateway"
    sudo nmcli device reapply "$interface"
    timeout 15s getent ahostsv4 files.pythonhosted.org >/dev/null || die "DNS is still unavailable after repairing $connection."
}

# Physical readiness is never inferred from an old answer or a piped "yes".
confirm_action() {
    local instructions="$1" expected="${2:-READY}" answer
    [[ -t 0 ]] || die "An operator terminal is required. Run this script directly in a terminal on the robot."
    printf '\n%s\n' "$instructions"
    while true; do
        printf 'Type %s to confirm, or STOP to exit: ' "$expected"
        if ! IFS= read -r answer; then
            log_warning "Input closed; stopping before the next command."
            exit 2
        fi
        case "$answer" in
            "$expected") return 0 ;;
            STOP|stop|q|Q) log_warning "Stopped by operator; no next command was run."; exit 2 ;;
            *) log_warning "No confirmation received. Complete the instructions, then type $expected (or STOP)." ;;
        esac
    done
}

installation_readiness() {
    confirm_action "Before installing software for $FLEET_ID:
  1. Verify that this fleet ID matches the serial number on the robot.
  2. Connect the NOCO Genius 10 charger to the robot in SUPPLY mode.
     Keep it connected throughout installation; ordinary charging mode is not SUPPLY.
  3. Attach the lift clip-clamp below the shoulder so it can support the arm.
     Placement picture: https://docs-arch.hello-robot.com/0.3/installation/robot_install/#plug-in-charger-attach-clip-clamp
  4. Stop robot applications, ROS drivers, teleoperation, and controller services.
Keep the robot powered on and do not remove the clamp during installation."
}

restore_project_calibration() {
    # The project overrides upstream stretch_core/stretch_description. Restore
    # calibration HERE as well, so a committed URDF from another robot isn't used.
    /usr/bin/python3 - "$CALIBRATION_DIR" "$ROBOT_WS/src/stretch_ros2" "$STATE_DIR" <<'PY'
import pathlib
import shutil
import sys
import tempfile

calibration, overlay, state = map(pathlib.Path, sys.argv[1:])
backup = pathlib.Path(tempfile.mkdtemp(prefix="overlay-calibration-", dir=state))
for pattern, relative in (
    ("head_calibrated*.urdf", "stretch_description/urdf/stretch.urdf"),
    ("controller_calibration_head_*.yaml", "stretch_core/config/controller_calibration_head.yaml"),
):
    source = max((calibration / "calibration_ros").glob(pattern), key=lambda p: p.stem[-14:])
    target = overlay / relative
    if target.exists() and target.read_bytes() != source.read_bytes():
        saved = backup / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, saved)
    shutil.copy2(source, target)
    print(f"Restored {source.name} -> {target}")
print(f"Previous project calibration files preserved in {backup}")
PY
}

main() {
    if [[ "${1:-}" == --post-install ]]; then
        shift
        exec bash "$SCRIPT_DIR/robot_post_install.bash" "$@"
    fi
    while (( $# )); do
        case "$1" in
            --check-only) CHECK_ONLY=true; shift ;;
            --fleet-id)
                [[ $# -ge 2 && "$2" =~ ^stretch-se3-[0-9]{4}$ ]] || die "Use --fleet-id stretch-se3-XXXX"
                FLEET_ID="$2"; shift 2 ;;
            --help|-h) show_help; return 0 ;;
            *) die "Unknown argument: $1 (see --help)" ;;
        esac
    done
    trap 'on_error "$?" "$LINENO"' ERR
    printf '\n%bPG-LLM Service Robot - Robot Full Setup%b\n' "$BOLD" "$NC"
    log_info "Project root: $PROJECT_ROOT"

    log_step "Checking Ubuntu and robot calibration"
    [[ $EUID -ne 0 ]] || die "Run as your normal user, not root or sudo bash."
    [[ -f /etc/os-release ]] || die "Ubuntu 22.04 is required."
    # shellcheck source=/dev/null
    source /etc/os-release
    [[ "$ID" == ubuntu && "$VERSION_ID" == 22.04 ]] || die "This setup requires Ubuntu 22.04 and ROS 2 Humble."
    [[ "$(uname -m)" == x86_64 ]] || die "This setup targets the Stretch 3's x86_64 computer."
    [[ -z "${VIRTUAL_ENV:-}" && -z "${CONDA_PREFIX:-}" ]] || die "Deactivate your Python/Conda environment and re-run."
    [[ -f "$ROBOT_DIR/pyproject.toml" && -f "$ROBOT_DIR/uv.lock" ]] || die "Robot project or uv.lock is missing."
    select_calibration "$HOME" "$HOME/stretch_user"
    if /usr/bin/python3 -c 'import yaml' 2>/dev/null; then
        validate_parameters
    else
        log_warning "YAML parameter validation will run after installing python3-yaml."
    fi
    if $CHECK_ONLY; then
        log_success "Calibration files are present; no changes made. Installation and hardware have not been tested."
        return 0
    fi

    mkdir -p "$STATE_DIR"
    LOG_FILE="$STATE_DIR/robot-setup-$(date +%Y%m%dT%H%M%S).log"
    exec > >(tee -a "$LOG_FILE") 2>&1
    log_info "Setup log: $LOG_FILE"
    installation_readiness
    export PATH="$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    unset AMENT_PREFIX_PATH CMAKE_PREFIX_PATH COLCON_PREFIX_PATH PYTHONPATH LD_LIBRARY_PATH
    unset ROS_DISTRO ROS_VERSION ROS_PYTHON_VERSION

    log_step "Installing bootstrap dependencies"
    # Honor an explicitly supplied local graphical askpass helper, including in
    # upstream Bash scripts, so long installs can renew sudo without chat secrets.
    if [[ -n "${SUDO_ASKPASS:-}" ]]; then
        [[ -x "$SUDO_ASKPASS" ]] || die "SUDO_ASKPASS must name an executable helper."
        sudo() { command sudo -A "$@"; }
        export -f sudo
    fi
    sudo -v
    sudo apt-get update
    prepare_nodesource_install
    sudo apt-get install -y git zip curl ca-certificates software-properties-common python3-pip python3-yaml
    validate_parameters

    log_step "Installing the official Hello Robot software stack"
    # A marker is only written after upstream returns success. A partially created
    # hello-robot.conf alone is NOT evidence of a successful installation.
    local marker="$STATE_DIR/official-install-$FLEET_ID.complete"
    if [[ ! -f "$marker" ]]; then
        if [[ ! -e "$HOME/stretch_install" ]]; then
            git clone --depth 1 https://github.com/hello-robot/stretch_install.git "$HOME/stretch_install"
        else
            [[ -d "$HOME/stretch_install/.git" ]] || die "$HOME/stretch_install is not a Git checkout."
            [[ -z "$(git -C "$HOME/stretch_install" status --porcelain)" ]] || die "$HOME/stretch_install has local changes; preserve them before updating."
            git -C "$HOME/stretch_install" pull --ff-only
        fi
        log_info "Upstream revision: $(git -C "$HOME/stretch_install" rev-parse HEAD)"
        # Upstream recreates ament_ws; preserve all existing sources and build files.
        preserve_path "$AMENT_WS"
        if [[ -f "$SYSTEM_CONFIG" ]]; then
            if [[ ! -d "$HOME/stretch_user/$FLEET_ID" ]]; then
                mkdir -p "$HOME/stretch_user"
                cp -a "$CALIBRATION_DIR" "$HOME/stretch_user/$FLEET_ID"
            fi
            log_substep "Resuming system/user setup (-u keeps existing system calibration and udev rules)"
            (cd "$HOME/stretch_install"; env HELLO_FLEET_ID="$FLEET_ID" bash ./stretch_new_robot_install.sh -u)
        else
            # Initial setup removes the HOME copy; retain another copy for recovery.
            mkdir -p "$STATE_DIR/calibration-backups"
            cp -a "$CALIBRATION_DIR" "$STATE_DIR/calibration-backups/$FLEET_ID.$(date +%Y%m%dT%H%M%S).$$"
            log_substep "Starting official fresh installation (answer its charger/clip-clamp prompt)"
            (cd "$HOME/stretch_install"; env -u HELLO_FLEET_ID -u HELLO_FLEET_PATH bash ./stretch_new_robot_install.sh -H "$FLEET_ID")
        fi
        git -C "$HOME/stretch_install" rev-parse HEAD > "$marker"
    else
        log_success "Official installation previously completed for $FLEET_ID"
    fi
    [[ -f /opt/ros/humble/setup.bash && -f "$AMENT_WS/install/local_setup.bash" ]] || die "Official ROS workspace missing. Remove $marker to retry the official installation."
    export HELLO_FLEET_ID="$FLEET_ID" HELLO_FLEET_PATH="$HOME/stretch_user" LRS_LOG_LEVEL=None
    CALIBRATION_DIR="$HELLO_FLEET_PATH/$HELLO_FLEET_ID"
    validate_calibration "$CALIBRATION_DIR"
    validate_parameters
    # shellcheck source=/dev/null
    source /opt/ros/humble/setup.bash
    # shellcheck source=/dev/null
    source "$AMENT_WS/install/local_setup.bash"

    log_step "Installing project system and ROS dependencies"
    sudo apt-get install -y \
        build-essential cmake python3-colcon-common-extensions \
        libportaudio2 portaudio19-dev libsndfile1 libusb-1.0-0-dev \
        python3.10-venv python3.10-dev screen openssh-server ffmpeg fastdds-tools dnsutils \
        ros-humble-nav2-bringup ros-humble-slam-toolbox ros-humble-control-msgs \
        ros-humble-cv-bridge ros-humble-vision-msgs ros-humble-spatio-temporal-voxel-layer \
        ros-humble-ament-cmake-gtest
    /usr/bin/python3 -m pip install --user 'numpy==1.26.4' 'setuptools==59.6.0'
    [[ -f /etc/ros/rosdep/sources.list.d/20-default.list ]] || sudo rosdep init
    rosdep update --rosdistro humble
    # The sourced underlay lets --ignore-src recognize upstream packages, without
    # scanning duplicate stretch_core/stretch_description manifests in both trees.
    rosdep install --rosdistro humble -iy --from-paths "$ROBOT_WS/src" \
        --ignore-src --skip-keys="librealsense2 realsense2_camera"

    log_step "Configuring DDS networking"
    bash "$SCRIPT_DIR/configure_dds_buffers.bash" --install
    if ip -o -4 address show | awk '{print $4}' | grep -Eq '^192\.168\.50\.(49|50|51|52)/'; then
        # A configuration failure is fatal; only an absent link is optional.
        bash "$SCRIPT_DIR/configure_dds_network.bash" --install
    else
        DDS_NETWORK_SKIPPED=true
        log_warning "Dedicated DDS link is absent; configure it before starting nodes."
    fi
    ensure_download_dns

    log_step "Setting up the robot Python environment"
    if ! command -v uv >/dev/null; then
        curl -fsSL https://astral.sh/uv/install.sh | sh
    fi
    command -v uv >/dev/null || die "uv installation failed."
    cd "$ROBOT_DIR"
    uv venv --python /usr/bin/python3.10 --system-site-packages --allow-existing
    uv sync --locked --python /usr/bin/python3.10
    mkdir -p "$HOME/.stretch_nav/maps" "$HOME/.stretch_nav/state"

    log_step "Restoring the robot's calibrated model to the project"
    restore_project_calibration

    log_step "Building and verifying the project ROS workspace"
    source "$SCRIPT_DIR/ros_tf2_fix.bash"
    pg_install_tf2_fix "$PROJECT_ROOT"
    pg_source_tf2_fix "$PROJECT_ROOT"
    # Keep the official underlay intact; allow only packages owned by this overlay.
    # Never source this overlay while building its underlay (that creates a cycle).
    cd "$ROBOT_WS"
    colcon build --cmake-clean-cache --allow-overriding stretch_core stretch_description stretch_nav2
    # shellcheck source=/dev/null
    source "$ROBOT_WS/install/local_setup.bash"
    [[ "$(ros2 pkg prefix stretch_core)" == "$ROBOT_WS/install/stretch_core" ]] || die "ROS is selecting the wrong stretch_core."
    [[ "$(ros2 pkg prefix stretch_description)" == "$ROBOT_WS/install/stretch_description" ]] || die "ROS is selecting the wrong stretch_description."
    # Import checks do not connect to or move the robot.
    "$ROBOT_DIR/.venv/bin/python" -c 'import rclpy, cv_bridge, stretch_body, sounddevice, torch'

    log_step "Configuring the shell environment"
    local env_file="$STATE_DIR/robot-env.bash" source_line
    {
        printf '# Generated by full_robot_setup.bash\n'
        printf 'export HELLO_FLEET_ID=%q\n' "$FLEET_ID"
        printf 'export HELLO_FLEET_PATH=%q\n' "$HOME/stretch_user"
        # Expand these variables when a future shell reads the generated file.
        # shellcheck disable=SC2016
        printf 'export PATH="$HOME/.local/bin:$PATH"\n'
        printf 'export LRS_LOG_LEVEL=None\nexport ROS_DOMAIN_ID=42\nexport ROS_LOCALHOST_ONLY=0\n'
        printf 'source /opt/ros/humble/setup.bash\n'
        printf 'source %q\n' "$AMENT_WS/install/local_setup.bash"
        printf 'source %q\n' "$ROBOT_WS/install/local_setup.bash"
        printf 'source %q\n' "$SCRIPT_DIR/ros_tf2_fix.bash"
        printf 'pg_source_tf2_fix %q\n' "$PROJECT_ROOT"
    } > "$env_file"
    printf -v source_line 'source %q' "$env_file"
    touch "$HOME/.bashrc"
    if ! grep -Fqx "$source_line" "$HOME/.bashrc"; then
        printf '\n# PG-LLM robot environment\n%s\n' "$source_line" >> "$HOME/.bashrc"
    fi

    log_success "Robot software setup completed. Log: $LOG_FILE"
    log_info "Next, run the guided power-cycle, firmware, tool, homing, and calibration steps:"
    log_info "bash $SCRIPT_DIR/full_robot_setup.bash --post-install"
    log_info "That guide pauses for physical preparation and confirmation before each hardware command."
    log_info "Then deploy/start robot services from the controller Web UI (scripts/start.sh on the controller)."
    log_info "To rebuild: bash $SCRIPT_DIR/update_robot_ws.sh --build-only"
    if $DDS_NETWORK_SKIPPED; then
        log_warning "DDS is still unconfigured. Connect the dedicated link (robot .51 or .52), then run:"
        log_warning "bash $SCRIPT_DIR/configure_dds_network.bash --install"
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi

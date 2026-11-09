#!/usr/bin/env bash

# Guided Stretch 3 post-install procedure. Hardware commands require an operator.
# https://docs-arch.hello-robot.com/0.3/installation/robot_install/#post-install-steps
# Share calibration checks, project paths, logging, and terminal confirmations.
# shellcheck source=full_robot_setup.bash
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/full_robot_setup.bash"

post_install_help() {
    cat <<'EOF'
Usage: bash scripts/robot_post_install.bash [--status]
   or: bash scripts/full_robot_setup.bash --post-install [--status]

  --status  Inspect files and prior records without opening hardware or changing files.
  --help    Show this help.

Run in a terminal as the normal robot user, outside Python/Conda environments.
An operator must remain beside the robot and confirm each preparation/result.
STOP or Ctrl-C stops the guide. There is no unattended/assume-yes mode.

This guide handles the full power cycle, firmware, tool configuration, homing,
arm/lift contact calibration, project model rebuild, and official system check.
After a shutdown, log back in and run this same command again.
Reruns repeat the checks and calibrations; recorded results never skip homing or
physical confirmations. Firmware recovery must follow the updater's instructions.
EOF
}

post_install_status() {
    local path
    log_info "Read-only status for $FLEET_ID; no hardware connections are opened."
    for path in \
        "$STATE_DIR/official-install-$FLEET_ID.complete" \
        /opt/ros/humble/setup.bash \
        "$AMENT_WS/install/local_setup.bash" \
        "$ROBOT_WS/install/local_setup.bash" \
        "$ROBOT_DIR/.venv/bin/python" \
        "$STATE_DIR/robot-env.bash"; do
        if [[ -f "$path" ]]; then
            log_success "Present: $path"
        else
            log_warning "Missing: $path"
        fi
    done
    log_info "Ubuntu boot time: $(uptime -s). This does not prove a trunk-switch power cycle."
    if [[ -f "$STATE_DIR/post-install-$FLEET_ID.needs-review" ]]; then
        log_warning "Post-install reached the final check but still needs review:"
        cat "$STATE_DIR/post-install-$FLEET_ID.needs-review"
    elif [[ -f "$STATE_DIR/post-install-$FLEET_ID.complete" ]]; then
        log_info "Previous operator-confirmed completion (not a current hardware health test):"
        cat "$STATE_DIR/post-install-$FLEET_ID.complete"
    else
        log_warning "No completed guided post-install run recorded. Firmware, tool, homing, contact calibration, and system check remain unverified."
    fi
    log_info "Calibration logs, if any (their presence alone does not prove the results were saved):"
    local -a results=("$HOME"/stretch_user/log/calibrate_guarded_contact_*_results_*.log)
    for path in "${results[@]}"; do
        [[ ! -f "$path" ]] || stat -c '%y %n' "$path"
    done
}

power_cycle_guide() {
    local answer boot_id request="$STATE_DIR/power-cycle-request-$FLEET_ID"
    boot_id=$(cat /proc/sys/kernel/random/boot_id)
    cat <<EOF
Before firmware, Hello Robot requires a FULL power cycle after software setup:
  1. Connect the charger in SUPPLY mode and fit the clip-clamp below the shoulder.
  2. Save your work and shut down Ubuntu. Wait until shutdown has finished.
  3. Turn OFF the power switch inside the robot's trunk.
  4. Connect a USB keyboard and monitor so you can select the new Ubuntu install.
  5. Turn the trunk switch ON, boot the new Ubuntu partition, and log in.
  6. Open a terminal and run this guide again:
     bash "$SCRIPT_DIR/robot_post_install.bash"

An Ubuntu restart alone does not complete this procedure.
Type POWER-CYCLED only if you have already completed ALL of these steps since
software installation. Type SHUTDOWN to prepare for shutdown now, or STOP to exit.
EOF
    while true; do
        printf 'Choice (POWER-CYCLED / SHUTDOWN / STOP): '
        if ! IFS= read -r answer; then exit 2; fi
        case "$answer" in
            POWER-CYCLED)
                if [[ -f "$request" && "$(cat "$request")" == "$boot_id" ]]; then
                    die "Still in the boot that requested shutdown. Complete the power cycle and rerun."
                fi
                return 0 ;;
            SHUTDOWN)
                confirm_action "Confirm the SUPPLY charger and support clamp are fitted, your work is saved,
and you are ready for Ubuntu to shut down NOW. After shutdown, complete steps
3–6 above. This terminal/remote connection will close." SHUTDOWN
                printf '%s\n' "$boot_id" > "$request"
                sudo shutdown -h now
                exit 0 ;;
            STOP|stop|q|Q) exit 2 ;;
            *) log_warning "Choose POWER-CYCLED, SHUTDOWN, or STOP; no action taken." ;;
        esac
    done
}

run_post_command() {
    local name="$1"; shift
    COMMAND_LOG="$RUN_DIR/$name.log"
    log_substep "Running: $*"
    # pipefail propagates command failures; tee retains diagnostics for tools that
    # print a failure but return zero. Preserve stdin for upstream's own prompts.
    "$@" 2>&1 | tee "$COMMAND_LOG"
}

validate_encoder_calibration() {
    # Firmware flashing also needs each motor's encoder data, not just the ROS
    # calibration. Read files directly; do not construct/connect to a motor.
    /usr/bin/python3 - "$CALIBRATION_DIR" <<'PY'
import math
import pathlib
import re
import sys
import yaml
directory = pathlib.Path(sys.argv[1])
with (directory / 'stretch_configuration_params.yaml').open() as stream:
    config = yaml.safe_load(stream)
with (directory / 'stretch_user_params.yaml').open() as stream:
    user = yaml.safe_load(stream) or {}
for joint in ('arm', 'lift', 'left-wheel', 'right-wheel'):
    device = f'hello-motor-{joint}'
    serial = user.get(device, {}).get('serial_no', config.get(device, {}).get('serial_no', ''))
    if not re.fullmatch(r'[A-Za-z0-9]+', str(serial)):
        sys.exit(f'[ERROR] Missing/invalid serial_no for {device} in the calibration parameters.')
    path = directory / 'calibration_steppers' / f'{device}_{serial}.yaml'
    try:
        with path.open() as stream:
            data = yaml.safe_load(stream)
        if not isinstance(data, list) or not data or not all(
            isinstance(value, (int, float)) and math.isfinite(value) for value in data
        ):
            raise ValueError('expected a nonempty numeric encoder calibration')
    except (OSError, ValueError, yaml.YAMLError) as exc:
        sys.exit(f'[ERROR] Firmware requires this motor calibration: {path}: {exc}\n'
                 'Restore calibration_steppers from THIS robot\'s complete backup before continuing. '
                 'If unavailable, contact Hello Robot support; do not use another robot\'s data.')
print('[SUCCESS] Encoder calibration files present for all four motors.')
PY
}

check_contact_overrides() {
    /usr/bin/python3 - "$CALIBRATION_DIR/stretch_user_params.yaml" "$1" <<'PY'
import sys
import yaml
with open(sys.argv[1]) as stream:
    params = yaml.safe_load(stream) or {}
contact = params.get(sys.argv[2], {}).get('contact_models', {}).get('effort_pct', {})
if any(key in contact for key in ('contact_thresh_default', 'contact_thresh_homing')):
    sys.exit(f"[ERROR] {sys.argv[1]} overrides {sys.argv[2]} contact thresholds. "
             "Review/remove these overrides before calibrating, otherwise the saved calibration is masked.")
PY
}

calibrate_contact() {
    local joint="$1" before after
    check_contact_overrides "$joint"
    before=$(stat -c '%y:%s' "$CALIBRATION_DIR/stretch_configuration_params.yaml")
    run_post_command "calibrate-$joint" "$HOME/.local/bin/REx_calibrate_guarded_contact.py" "--$joint"
    after=$(stat -c '%y:%s' "$CALIBRATION_DIR/stretch_configuration_params.yaml")
    [[ "$after" != "$before" ]] || die "No saved configuration detected for $joint. Did you answer yes to 'Save results?'? Review $COMMAND_LOG and rerun."
    confirm_action "Review the $joint calibration output. Confirm that all sweeps completed normally,
there were no mechanical/contact-threshold warnings, and you answered yes to
'Save results?'. If warnings/errors occurred, type STOP and investigate them
with Hello Robot support before continuing. Log: $COMMAND_LOG" SAVED
}

sync_project_meshes() {
    # Tool configuration updates the official workspace. Our overlay must also
    # have the mesh assets referenced by the freshly calibrated URDF.
    # package://stretch_description resolves to this overlay; missing files do
    # not fall back to the official package. These local copies are gitignored.
    /usr/bin/python3 - "$ROBOT_WS/src/stretch_ros2/stretch_description" \
        "$AMENT_WS/src/stretch_ros2/stretch_description" "$RUN_DIR/model-backup" <<'PY'
import pathlib
import shutil
import sys
import xml.etree.ElementTree as ET
overlay, upstream, backup = map(pathlib.Path, sys.argv[1:])
for mesh in ET.parse(overlay / 'urdf/stretch.urdf').iter('mesh'):
    uri = mesh.get('filename', '')
    prefix = 'package://stretch_description/'
    if not uri.startswith(prefix):
        continue
    relative = pathlib.Path(uri[len(prefix):])
    if relative.is_absolute() or '..' in relative.parts:
        sys.exit(f'Invalid mesh path: {uri}')
    source, target = upstream / relative, overlay / relative
    if source.is_file() and (not target.is_file() or target.read_bytes() != source.read_bytes()):
        if target.exists():
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    if not target.is_file():
        sys.exit(f'Missing model mesh: {target}')
PY
}

check_system_report() {
    SYSTEM_CHECK_NOTES=""
    # stretch_system_check.py may print [Fail] and still exit zero.
    if grep -Eiq '\[Fail\]|\[Error\]|Traceback|(^|[[:space:]])ERROR([[:space:]:]|$)' "$1"; then
        die "System check reported a failure. Review $1 and contact Hello Robot support before using the robot."
    fi
    # Upstream can suppress software-check exceptions. Require its final ROS
    # report as well as the hardware report before considering it complete.
    grep -q 'Battery voltage' "$1" || die "System check hardware report is incomplete: $1"
    grep -Eq '\[(Pass|Warn)\].*ROS' "$1" || die "System check software report is incomplete: $1"
    # Old Body Tools releases still expect the retired Diagnostics package.
    # Keep that explanation and the operator-requested non-blocking IMU AZ
    # warning in the completion record. Other warnings still stop completion.
    SYSTEM_CHECK_NOTES=$(/usr/bin/python3 - "$1" <<'PY'
import pathlib
import re
import sys
report = re.sub(r'\x1b\[[0-9;]*m', '', pathlib.Path(sys.argv[1]).read_text())
retired = '[Warn] Python pkgs not up-to-date (run pip3 install -U hello-robot-stretch-diagnostics)'
number = r'-?\d+(?:\.\d+)?'
imu_az = re.compile(
    rf'\[Warn\] Sensors not ready \(IMU AZ = {number} out of range {number} to {number}\)'
)
unresolved = []
for line in report.splitlines():
    line = line.strip()
    if line.strip() == retired:
        print('[INFO] The checker expects the retired Stretch Diagnostics package. '
              'Hello Robot replaced it with stretch_system_check.py; see '
              'https://github.com/hello-robot/stretch_diagnostics#deprecation-notice')
    elif imu_az.fullmatch(line):
        print('[WARNING] ' + line.removeprefix('[Warn] ')
              + ' — recorded without blocking setup; IMU calibration is unchanged.')
    elif '[Warn]' in line:
        unresolved.append(line)
if unresolved:
    sys.exit('\n'.join(unresolved))
PY
    ) || die "System check has unresolved warnings. Review $1 before completing setup."
    if [[ -n "$SYSTEM_CHECK_NOTES" ]]; then
        printf '%s\n' "$SYSTEM_CHECK_NOTES"
    fi
}

# Keep the final software refresh/report sequence callable after a reviewed
# interrupted session, without repeating firmware flashing or calibration.
finish_post_install() {
    local receipt="$STATE_DIR/post-install-$FLEET_ID.complete"
    log_step "Refresh the project's calibrated model"
    validate_calibration "$CALIBRATION_DIR"
    validate_parameters
    restore_project_calibration
    sync_project_meshes
    (cd "$ROBOT_WS"; colcon build --packages-select stretch_core stretch_description \
        --allow-overriding stretch_core stretch_description)
    log_success "Project model and controller calibration rebuilt using the current robot calibration."

    log_step "Run the official system check"
    confirm_action "Keep the clamp removed and the workspace clear. Leave the SUPPLY charger attached
and other robot applications stopped. The next command accesses motors and sensors
to check communications, homing, battery readings, firmware, and software.
Review every reported warning or failure before declaring setup complete."
    {
        printf 'Final system check pending review: %s\n' "$(date -Is)"
        printf 'Report: %s/system-check.log\n' "$RUN_DIR"
        printf 'Log: %s\n' "$LOG_FILE"
    } > "$STATE_DIR/post-install-$FLEET_ID.needs-review"
    run_post_command system-check "$HOME/.local/bin/stretch_system_check.py" --verbose
    check_system_report "$COMMAND_LOG"
    confirm_action "Confirm you have reviewed the system report and are ready to finish setup.
An IMU AZ warning is non-blocking and will remain in the completion record.
Other warnings, failures, and incomplete reports must be resolved before this step." COMPLETE
    {
        printf 'Operator-confirmed post-install completed: %s\n' "$(date -Is)"
        if [[ "$SYSTEM_CHECK_NOTES" == *'[WARNING]'* ]]; then
            printf 'Result: completed with warnings\n'
        fi
        printf 'Boot ID: %s\n' "$(cat /proc/sys/kernel/random/boot_id)"
        printf 'Log: %s\n' "$LOG_FILE"
        if [[ -n "$SYSTEM_CHECK_NOTES" ]]; then
            printf '%s\n' "$SYSTEM_CHECK_NOTES"
        fi
    } > "$receipt"
    mv "$STATE_DIR/post-install-$FLEET_ID.needs-review" "$RUN_DIR/system-check-reviewed.txt"
    if [[ "$SYSTEM_CHECK_NOTES" == *'[WARNING]'* ]]; then
        log_warning "Guided robot post-install completed with warnings. Log: $LOG_FILE"
        printf '%s\n' "$SYSTEM_CHECK_NOTES"
    else
        log_success "Guided robot post-install completed. Log: $LOG_FILE"
    fi
    log_info "Back up the updated $CALIBRATION_DIR to another device."
    log_info "Gamepad autostart is optional (Startup Applications -> hello_robot_gamepad_teleop)."
    log_info "Leave it disabled when the project's controller owns the robot, to avoid competing commands."
    log_info "Before normal mobile operation, disconnect the charger, confirm the clamp is removed, and clear the workspace."
    log_info "Open a new terminal for the project environment. Deploy/start services from the controller Web UI."
}

post_install_main() {
    local status_only=false command device receipt
    case "${1:-}" in
        --help|-h) post_install_help; return 0 ;;
        --status) status_only=true; shift ;;
        '') ;;
        *) die "Unknown argument: $1 (see --help)" ;;
    esac
    (( $# == 0 )) || die "Unexpected arguments (see --help)."
    trap 'on_error "$?" "$LINENO"' ERR
    select_calibration
    validate_parameters
    validate_encoder_calibration
    if $status_only; then post_install_status; return 0; fi

    [[ $EUID -ne 0 ]] || die "Run as the normal robot user, not root."
    [[ -z "${VIRTUAL_ENV:-}" && -z "${CONDA_PREFIX:-}" ]] || die "Deactivate your Python/Conda environment."
    [[ -t 0 ]] || die "Run this guide in an operator terminal on the robot."
    [[ -f "$STATE_DIR/official-install-$FLEET_ID.complete" ]] || die "Complete full_robot_setup.bash first."
    [[ "$CALIBRATION_DIR" == "$HOME/stretch_user/$FLEET_ID" ]] || die "User calibration is missing. Complete full_robot_setup.bash first."
    for command in REx_firmware_updater.py stretch_configure_tool.py stretch_robot_home.py \
        REx_calibrate_guarded_contact.py stretch_system_check.py; do
        [[ -x "$HOME/.local/bin/$command" ]] || die "Missing $command; complete software setup first."
    done
    for command in /opt/ros/humble/setup.bash "$AMENT_WS/install/local_setup.bash" \
        "$ROBOT_WS/install/local_setup.bash"; do
        [[ -f "$command" ]] || die "Missing $command; complete software setup first."
    done
    check_contact_overrides arm
    check_contact_overrides lift

    RUN_DIR="$STATE_DIR/post-install-$FLEET_ID-$(date +%Y%m%dT%H%M%S).$$"
    mkdir -p "$RUN_DIR"
    LOG_FILE="$RUN_DIR/guide.log"
    exec > >(tee -a "$LOG_FILE") 2>&1
    TOTAL_STEPS=8
    log_info "Guided post-install for $FLEET_ID. Log: $LOG_FILE"
    log_info "Every run repeats the procedure. STOP exits; upstream prompts must also be answered."

    log_step "Full robot power cycle"
    power_cycle_guide

    log_step "Prepare and update firmware"
    installation_readiness
    for device in hello-pimu hello-wacc hello-motor-arm hello-motor-lift \
        hello-motor-left-wheel hello-motor-right-wheel hello-dynamixel-head hello-dynamixel-wrist; do
        [[ -r "/dev/$device" && -w "/dev/$device" ]] || die "Missing or inaccessible /dev/$device. Check USB connections/permissions and power cycle before continuing."
    done
    # Use only the official ROS underlay for the official configuration tools.
    # The inherited project overlay would otherwise send URDF updates to the wrong tree.
    export PATH="$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    unset AMENT_PREFIX_PATH CMAKE_PREFIX_PATH COLCON_PREFIX_PATH PYTHONPATH LD_LIBRARY_PATH
    unset ROS_DISTRO ROS_VERSION ROS_PYTHON_VERSION
    export HELLO_FLEET_ID="$FLEET_ID" HELLO_FLEET_PATH="$HOME/stretch_user" LRS_LOG_LEVEL=None
    # shellcheck source=/dev/null
    source /opt/ros/humble/setup.bash
    # shellcheck source=/dev/null
    source "$AMENT_WS/install/local_setup.bash"
    cp -a "$CALIBRATION_DIR" "$RUN_DIR/calibration-before"
    receipt="$STATE_DIR/post-install-$FLEET_ID.complete"
    if [[ -f "$receipt" ]]; then mv "$receipt" "$RUN_DIR/previous-completion"; fi
    # Authenticate in this terminal before launching the updater, whose Python
    # subprocesses invoke sudo directly. An explicit local askpass helper keeps
    # the password on the robot desktop when the guide is run remotely.
    log_info "Authenticating for firmware tools (enter the Ubuntu password locally)."
    if [[ -n "${SUDO_ASKPASS:-}" ]]; then
        [[ -x "$SUDO_ASKPASS" ]] || die "SUDO_ASKPASS must name an executable local helper."
        sudo -A -v
    else
        sudo -v
    fi
    log_warning "Do not interrupt power or USB during flashing. Follow updater recovery instructions if it fails."
    run_post_command firmware "$HOME/.local/bin/REx_firmware_updater.py" --install
    confirm_action "Confirm the updater completed successfully or reported that no update is needed.
If it reported failed/missing boards, interrupted flashing, or recovery instructions,
type STOP and resolve that first. Do not proceed on a partial update." DONE

    log_step "Check the attached tool"
    confirm_action "Keep the SUPPLY charger and clamp fitted, with other robot applications stopped.
Check that the end-of-arm tool is securely attached and clear of obstructions.
The next command detects it and may update robot parameters and the calibrated model.
Read its tool selection prompts; stop if the detected tool does not match the hardware."
    run_post_command tool "$HOME/.local/bin/stretch_configure_tool.py"
    confirm_action "Confirm that the reported tool matches the attached hardware and configuration
completed without errors (or reported that the current configuration is correct)." DONE

    log_step "Home the robot"
    confirm_action "The next command moves the head, lift, arm, wrist, and gripper to find their zeros.
Keep the SUPPLY charger attached, with its cable clear of all moving parts.
Leave the clamp below the shoulder for now, as in the archived installation guide.
Clear the full arm reach, full lift height, wrist/head sweep, and area around the base.
Remove objects held by the gripper; keep people, pets, and hands out of this space.
Remain beside the robot, ready to use its runstop; release the runstop only once clear.
No ROS drivers, teleop, or project services may be running."
    run_post_command home "$HOME/.local/bin/stretch_robot_home.py"
    confirm_action "Confirm all joints homed normally, without errors, collisions, or a runstop.
If anything failed, type STOP. Homing must be repeated after every robot power cycle." HOMED

    log_step "Calibrate arm contact thresholds"
    confirm_action "Keep the clamp fitted and charger in SUPPLY mode, with cables clear.
The calibration first homes the lift and wrist yaw, then sweeps the arm through
its FULL extension/retraction range four times. Guarded contact is disabled during
measurement, so it must not encounter obstacles or people. Keep the gripper empty.
Remain at the runstop, clear the entire swept volume, and answer the tool's prompts.
Use Ctrl-C to abort; some upstream preparation prompts ignore a 'no' answer.
Inspect the results and answer yes to 'Save results?' only if there are no warnings."
    calibrate_contact arm

    log_step "Remove the clamp and calibrate lift contact thresholds"
    confirm_action "Wait for all motion to stop, then REMOVE the lift clip-clamp completely.
Leave the charger in SUPPLY mode and keep its cable outside the moving workspace.
The next command homes the arm and wrist yaw, then sweeps the lift through its FULL
height four times. Clear everything above/below the shoulder and around the wrist.
Guarded contact is disabled during measurement. Keep hands away and remain at the
runstop. Confirm the clamp is removed and the workspace is clear before continuing.
Use Ctrl-C to abort; some upstream preparation prompts ignore a 'no' answer.
Inspect the results and answer yes to 'Save results?' only if there are no warnings." CLAMP-REMOVED
    calibrate_contact lift

    finish_post_install

}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    post_install_main "$@"
fi

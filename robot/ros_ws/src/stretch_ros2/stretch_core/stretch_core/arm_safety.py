"""Small, hardware-independent guards around the blocking body services."""
import math
import time


def _near_zero(value, tolerance):
    return (isinstance(value, (float, int)) and not isinstance(value, bool)
            and math.isfinite(value) and abs(value) <= tolerance)


def trajectory_active(node):
    try:
        goal = getattr(node.joint_trajectory_action, '_goal_handle', None)
        return goal is not None and bool(goal.is_active)
    except Exception:
        # rclpy may invalidate a handle concurrently with timer publication.
        # Unknown completion must not crash the driver or establish idle.
        return True


def measured_stationary(status, head_joints, wrist_joints):
    """Require every configured actuator and the base to report low velocity."""
    try:
        values = [(status['arm']['vel'], .003), (status['lift']['vel'], .003),
                  (status['base']['x_vel'], .005), (status['base']['y_vel'], .005),
                  (status['base']['theta_vel'], .01)]
        values.extend((status['head'][joint]['vel'], .03) for joint in head_joints)
        values.extend((status['end_of_arm'][joint]['vel'], .03) for joint in wrist_joints)
        return all(_near_zero(value, tolerance) for value, tolerance in values)
    except (KeyError, TypeError):
        return False


def driver_state_snapshot(node, status, now, *, base_command_active=False):
    """One coherent diagnostic snapshot; never changes a motor or stop state."""
    runstop = status.get('pimu', {}).get('runstop_event')
    # Firmware versions expose this bit as either bool or 0/1.
    runstop = bool(runstop) if type(runstop) in (bool, int) and runstop in (0, 1) else None
    goal_active = trajectory_active(node)
    busy = node._arm_service_lock.locked()
    previous_sample = getattr(node, '_motion_last_sample', None)
    if previous_sample is None or not 0 < now - previous_sample <= 1.:
        node._motion_idle_since = None
    node._motion_last_sample = now
    idle = (node.robot_mode in ('navigation', 'position') and runstop is False
            and not busy and not node.streaming_position_activated and not goal_active
            and not base_command_active and not node.dirty_command and measured_stationary(
                status, node.robot.head.joints, node.robot.end_of_arm.joints))
    if not idle:
        node._motion_idle_since = None
    elif node._motion_idle_since is None:
        node._motion_idle_since = now
    return {'sampled_at': now, 'driver_started_at': node._driver_started_at,
            'runstop_active': runstop, 'mode': node.robot_mode,
            'arm_service_active': busy,
            'streaming_position_active': bool(node.streaming_position_activated),
            'motion_idle': bool(idle and now - node._motion_idle_since >= .5)}


def _runstopped(node):
    return node.robot_mode == 'runstopped' or bool(node.robot.pimu.status.get('runstop_event', True))


def _stow_confirmed(robot):
    """Check the actual travel joints against the manufacturer's stow targets."""
    status = robot.get_status()
    checks = [(status['arm']['pos'], robot.get_stow_pos('arm'), .02),
              (status['lift']['pos'], robot.get_stow_pos('lift'), .03)]
    for joint in ('wrist_yaw', 'wrist_pitch', 'wrist_roll'):
        if joint in robot.end_of_arm.joints:
            checks.append((status['end_of_arm'][joint]['pos'], robot.get_stow_pos(joint), .15))
    return all(_near_zero(actual - target, tolerance) for actual, target, tolerance in checks)


def update_runstop_mode(node, runstopped):
    """Atomically choose and set the stop/release mode against service cleanup."""
    node.robot_mode_rwlock.acquire_write()
    try:
        if runstopped:
            if node.robot_mode == 'runstopped':
                return False
            node._arm_stop_generation += 1
            node.prerunstop_mode = (node._arm_previous_mode if node._arm_service_mode
                                   else node.robot_mode)
            mode = 'runstopped'
        else:
            if node.robot_mode != 'runstopped':
                return False
            mode = node._arm_service_mode or node.prerunstop_mode
        node._cleanup_before_changing_mode()
        node.robot_mode = mode
        node.get_logger().info(f'Changed to mode = {mode}')
        return True
    finally:
        node.robot_mode_rwlock.release_write()


def run_arm_service(node, operation):
    """Run one requested service and leave its mode usable after it returns.

    A body function returning None is normal. Success requires the corresponding
    hardware state as well. Interruption/exception remains an uncertain physical
    result, not permission to retry automatically.
    """
    assert operation in ('home', 'stow')
    active_mode = 'homing' if operation == 'home' else 'stowing'
    if not node._arm_service_lock.acquire(blocking=False):
        return False, 'DRIVER_BUSY: another arm service is still running.'
    entered = False
    previous_mode = None
    generation = node._arm_stop_generation
    try:
        # The trajectory executor holds a mode read lock while working. Refuse
        # before waiting for its write lock, otherwise a timed-out Trigger could
        # begin homing much later when that unrelated trajectory finally ends.
        if node.streaming_position_activated or trajectory_active(node):
            return False, 'DRIVER_BUSY: another controller is still active.'
        node.robot_mode_rwlock.acquire_write()
        try:
            previous_mode = node.robot_mode
            if _runstopped(node):
                return False, f'Cannot {operation} while in mode=runstopped.'
            if previous_mode not in node.control_modes:
                return False, f'Cannot {operation} while in mode={previous_mode}.'
            if operation == 'stow' and not node.robot.is_homed():
                return False, 'Cannot stow: robot is not homed.'
            if node.streaming_position_activated or trajectory_active(node):
                return False, 'DRIVER_BUSY: another controller is still active.'
            node._cleanup_before_changing_mode()
            node.robot_mode = active_mode
            node._arm_previous_mode = previous_mode
            node._arm_service_mode = active_mode
            entered = True
        finally:
            node.robot_mode_rwlock.release_write()
        node.get_logger().info(f'Changed to mode = {active_mode}')
        getattr(node.robot, operation)()
        if _runstopped(node) or generation != node._arm_stop_generation:
            return False, 'ARM_OPERATION_INTERRUPTED: safety stop occurred during arm motion.'
        if operation == 'home' and not node.robot.is_homed():
            return False, 'ARM_OPERATION_FAILED: calibration was not confirmed by the joint controllers.'
        if operation == 'stow':
            # The manufacturer waits for motor setpoints, but the ROS/body
            # feedback snapshot can trail the final command by one timer tick.
            deadline = time.monotonic() + 1.
            while not _stow_confirmed(node.robot):
                if _runstopped(node) or generation != node._arm_stop_generation:
                    return False, 'ARM_OPERATION_INTERRUPTED: safety stop occurred during arm motion.'
                if time.monotonic() >= deadline:
                    return False, 'ARM_OPERATION_FAILED: the arm has not reached its stow position.'
                time.sleep(.05)
        if _runstopped(node) or generation != node._arm_stop_generation:
            return False, 'ARM_OPERATION_INTERRUPTED: safety stop occurred during arm motion.'
        return True, 'Homed.' if operation == 'home' else 'Stowed.'
    except Exception as exc:
        node.get_logger().error(f'{operation} failed: {exc}')
        return False, f'ARM_OPERATION_FAILED: {operation} did not complete: {exc}'
    finally:
        cleanup_error = None
        if entered:
            # Manufacturer routines temporarily disable collision management.
            # Restore it even when an exception bypasses their normal cleanup.
            try:
                node.robot.enable_collision_mgmt()
            except Exception as exc:
                cleanup_error = exc
                node.get_logger().error(f'Could not restore collision management: {exc}')
            node.robot_mode_rwlock.acquire_write()
            try:
                if _runstopped(node):
                    node.robot_mode = 'runstopped'
                    # Releasing a stop must restore a control mode, never the
                    # completed homing/stowing operation's temporary mode.
                    node.prerunstop_mode = previous_mode
                elif node.robot_mode == active_mode:
                    node.robot_mode = previous_mode
                node._arm_service_mode = None
            finally:
                node.robot_mode_rwlock.release_write()
        node._arm_service_lock.release()
        if cleanup_error is not None:
            return False, 'ARM_OPERATION_FAILED: collision-management cleanup could not be confirmed.'

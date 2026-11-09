from pathlib import Path

from action_service.common.handoff import HandoffActionBase
from robot_common import get_config

config = get_config('action_service', Path(__file__).stem)


class TakeFromHandAction(HandoffActionBase):

    def __init__(self, node):
        super().__init__(node, config, log_prefix='handoff', action_key='take_from_hand')
        self.handoff_active = False
        self._arm_motion_started = False
        self._arm_retracted = False
        self._arm_cleanup_attempted = False
        self._object_secured = False
        self._grasp_attempted = False

    def stop(self):
        with self._lock:
            if not self.handoff_active:
                return
            self.handoff_active = False
        self.log.warning('TakeFromHandAction: external stop requested')
        self._stop_evt.set()

    def execute(self, request, response, parameters=None):
        del request
        timeout_s = self._timeout_from_parameters(parameters)
        self.log.info(f'take_from_hand: starting with timeout={timeout_s:.1f}s')
        with self._lock:
            if self.handoff_active:
                return self.node.set_failure(response, 'Hand pickup already in progress')
            self.handoff_active = True
            self._begin_handoff(parameters)
        self._arm_motion_started = False
        self._arm_retracted = False
        self._arm_cleanup_attempted = False
        self._object_secured = False
        self._grasp_attempted = False
        self.motion.last_gripper_completion = None
        try:
            if not self.motion.wait_for_traj_server(timeout_s=float(config.timeouts.trajectory_server_seconds)):
                if self._stop_evt.is_set():
                    return self.node.set_stopped(response, 'Hand pickup stopped')
                return self.node.set_failure(response, 'Hand pickup failed: trajectory controller not ready')
            if not self._wait_for_joints(float(config.timeouts.joints_seconds)):
                if self._stop_evt.is_set():
                    return self.node.set_stopped(response, 'Hand pickup stopped')
                return self.node.set_failure(response, 'Hand pickup failed: joints not ready')
            if not self.motion.switch_to_navigation_mode(timeout_s=3.0):
                if self._stop_evt.is_set():
                    return self.node.set_stopped(response, 'Hand pickup stopped')
                return self.node.set_failure(response, 'Hand pickup failed: unable to prepare base rotation')
            self._handoff_motion_started = True
            if not self._turn_base_for_handoff():
                if self._stop_evt.is_set():
                    return self.node.set_stopped(response, 'Hand pickup stopped')
                return self.node.set_failure(response, 'Hand pickup failed: unable to turn base for handoff')
            self._base_turned_for_handoff = True
            if not self.motion.switch_to_position_mode(timeout_s=3.0):
                if self._stop_evt.is_set():
                    return self.node.set_stopped(response, 'Hand pickup stopped')
                return self.node.set_failure(response, 'Hand pickup failed: unable to enter position mode')

            if self._stop_evt.is_set():
                return self.node.set_stopped(response, 'Hand pickup stopped')
            self._head_moved_for_handoff = True
            if not self._aim_head_for_handoff():
                if self._stop_evt.is_set():
                    return self.node.set_stopped(response, 'Hand pickup stopped')
                return self.node.set_failure(response, 'Hand pickup failed: unable to aim head camera')
            if self._stop_evt.is_set():
                return self.node.set_stopped(response, 'Hand pickup stopped')
            self._arm_motion_started = True
            if not self._move_to_handoff_pose():
                if self._stop_evt.is_set():
                    return self.node.set_stopped(response, 'Hand pickup stopped')
                return self.node.set_failure(response, 'Hand pickup failed: unable to move to handoff pose')
            if self._stop_evt.is_set():
                return self.node.set_stopped(response, 'Hand pickup stopped')

            # Only accept a gesture made after the gripper is ready.
            self._clear_gesture()
            if not self._arm_gesture_detection():
                self._abort_open_and_retract()
                return self.node.set_failure(response, 'Hand pickup failed: gesture detector unavailable')
            self.node.announce_handoff_ready('take_from_hand')
            gesture = self._wait_for_gesture(timeout_s)
            if gesture is None:
                if self._stop_evt.is_set():
                    return self.node.set_stopped(response, 'Hand pickup stopped')
                self._abort_open_and_retract()
                return self.node.set_timeout(response, f'Hand pickup timed out after {timeout_s:.1f}s')

            self.log.info(f'take_from_hand: gesture accepted '
                          f'confidence={gesture["confidence"]:.2f}')
            if not self._close_gripper():
                if self._stop_evt.is_set():
                    return self.node.set_stopped(response, 'Hand pickup stopped')
                return self.node.set_failure(response, 'Hand pickup failed: unable to close gripper')
            self._object_secured = True
            self.motion.sleep(float(config.handoff.post_close_hold_seconds))
            if self._stop_evt.is_set():
                return self.node.set_stopped(response, 'Hand pickup stopped')
            if bool(getattr(config.handoff, 'stow_after_pickup', False)):
                if not self._stow_with_object():
                    if self._stop_evt.is_set():
                        return self.node.set_stopped(response, 'Hand pickup stopped')
                    return self.node.set_failure(response, 'Gripper close accepted, but arm retraction was not confirmed')
            else:
                self.log.info('take_from_hand: grasp secured, skipping post-pickup stow')
            if self._stop_evt.is_set():
                return self.node.set_stopped(response, 'Hand pickup stopped')
            return self.node.set_success(response, 'Gripper close completed; possession is assumed')
        finally:
            self._disarm_gesture_detection()
            self._cleanup_arm()
            safe_to_restore = (not self._arm_motion_started or self._arm_retracted)
            if safe_to_restore:
                try:
                    self._orientation_restored = self._restore_handoff_orientation() is True
                except Exception as exc:
                    self.log.warning('take_from_hand: failed to restore handoff orientation: '
                                     f'{exc}')
                try:
                    if bool(config.handoff.switch_to_navigation_mode_after):
                        self._navigation_mode_restored = self.motion.switch_to_navigation_mode(timeout_s=3.0) is True
                except Exception as exc:
                    self.log.warning('take_from_hand: failed to restore navigation mode: '
                                     f'{exc}')
            elif not self._stop_evt.is_set():
                self.log.warning('take_from_hand: arm retraction was not confirmed; '
                                 'leaving the base side-on')
            with self._lock:
                self.handoff_active = False

    def _move_to_handoff_pose(self) -> bool:
        return self._send_handoff_pose(float(config.gripper.open.max))

    def _close_gripper(self) -> bool:
        started_at = self._clock_seconds()
        feedback = getattr(getattr(self.node, 'k2_runtime', None), 'gripper_feedback', None)
        start_position = feedback.latest_position(started_at) if feedback is not None else None
        self._grasp_attempted = True
        sent = self.motion.send_named_trajectory([config.gripper.joint],
            [float(config.gripper.closed)], duration=float(config.durations.close_seconds), wait=False)
        if not sent:
            return False
        contact_check = (lambda: feedback.contact_indicated(now=self._clock_seconds(),
            started_at=started_at, target=float(config.gripper.closed), start_position=start_position)) if feedback is not None else None
        return not self._stop_evt.is_set() and self.motion.finish_gripper_close(
            timeout_s=float(config.handoff.close_timeout_seconds), contact_check=contact_check)

    def _move_to_carry_pose(self, *, closed: bool) -> bool:
        # The preceding close/open owns the gripper. Do not issue it again as
        # part of arm cleanup, especially when an object blocks full closure.
        del closed
        pose = config.handoff.carry_pose
        positions = {
            'joint_lift': float(pose.joint_lift),
            'wrist_extension': float(pose.wrist_extension),
            'joint_wrist_yaw': float(pose.joint_wrist_yaw),
            'joint_wrist_pitch': float(pose.joint_wrist_pitch),
            'joint_wrist_roll': float(pose.joint_wrist_roll), }
        return self.motion.send_full_trajectory(positions, duration=float(config.durations.carry_seconds), wait=True,
                                                omit_joints=(config.gripper.joint,))

    def _stow_with_object(self) -> bool:
        self.log.info('take_from_hand: stowing arm with object')
        self._arm_cleanup_attempted = True
        result = self.motion.safe_stow_arm(gripper_pos=float(config.gripper.closed), safe_lift_height=float(config.handoff.object_stow.safe_lift_height), preserve_gripper=True, )
        self._arm_retracted = bool(result)
        return bool(result)

    def _abort_open_and_retract(self) -> bool:
        self.log.info('take_from_hand: opening and retracting without an object')
        self._arm_cleanup_attempted = True
        try:
            if not self.motion.open_gripper(duration_s=float(config.durations.open_seconds), wait=True):
                return False
            result = self._move_to_carry_pose(closed=False)
            self._arm_retracted = bool(result)
            return bool(result)
        except Exception as exc:
            self.log.warning(f'take_from_hand: retract after timeout failed: {exc}')
            return False

    def _cleanup_arm(self):
        if self._grasp_attempted and not self._object_secured:
            # An interrupted close may already contain the object. Do not open
            # or retract it automatically; leave the physical check to staff.
            return
        if (
                self._stop_evt.is_set() or not self._arm_motion_started or self._arm_retracted or self._arm_cleanup_attempted):
            return
        try:
            if self._object_secured:
                if bool(getattr(config.handoff, 'stow_after_pickup', False)):
                    self._stow_with_object()
                else:
                    self._arm_cleanup_attempted = True
                    self._arm_retracted = bool(self._move_to_carry_pose(closed=True))
            else:
                self._abort_open_and_retract()
        except Exception as exc:
            self.log.warning(f'take_from_hand: cleanup retraction failed: {exc}')

from pathlib import Path

from action_service.common.handoff import HandoffActionBase
from robot_common import get_config

config = get_config('action_service', Path(__file__).stem)


class GiveToPersonAction(HandoffActionBase):

    def __init__(self, node):
        super().__init__(node, config, log_prefix='give_handoff', action_key='give_to_person')
        self.give_active = False
        self._arm_motion_started = False
        self._arm_retracted = False
        self._arm_cleanup_attempted = False
        self._release_attempted = False
        self._gripper_opened = False

    def stop(self):
        with self._lock:
            if not self.give_active:
                return
        self.log.warning('GiveToPersonAction: external stop requested')
        self._stop_evt.set()

    def execute(self, request, response, parameters=None):
        del request
        timeout_s = self._timeout_from_parameters(parameters)
        self.log.info(f'give_to_person: starting with timeout={timeout_s:.1f}s')
        with self._lock:
            if self.give_active:
                return self.node.set_failure(response, 'Item handoff already in progress')
            self.give_active = True
            self._begin_handoff(parameters)
        self._arm_motion_started = False
        self._arm_retracted = False
        self._arm_cleanup_attempted = False
        self._release_attempted = False
        self._gripper_opened = False
        try:
            if not self.motion.wait_for_traj_server(timeout_s=float(config.timeouts.trajectory_server_seconds)):
                return self._stopped_or_failure(response, 'Item handoff failed: trajectory controller not ready')
            if not self._wait_for_joints(float(config.timeouts.joints_seconds)):
                return self._stopped_or_failure(response, 'Item handoff failed: joints not ready')
            if not self.motion.switch_to_navigation_mode(timeout_s=3.0):
                return self._stopped_or_failure(response, 'Item handoff failed: unable to prepare base rotation')
            self._handoff_motion_started = True
            if not self._turn_base_for_handoff():
                return self._stopped_or_failure(response, 'Item handoff failed: unable to turn base for handoff')
            self._base_turned_for_handoff = True
            if not self.motion.switch_to_position_mode(timeout_s=3.0):
                return self._stopped_or_failure(response, 'Item handoff failed: unable to enter position mode')

            if self._stop_evt.is_set():
                return self.node.set_stopped(response, 'Item handoff stopped')
            self._head_moved_for_handoff = True
            if not self._aim_head_for_handoff():
                return self._stopped_or_failure(response, 'Item handoff failed: unable to aim head camera')
            if self._stop_evt.is_set():
                return self.node.set_stopped(response, 'Item handoff stopped')

            self._arm_motion_started = True
            if not self._move_to_handoff_pose():
                return self._stopped_or_failure(response, 'Item handoff failed: unable to move to presentation pose')
            if self._stop_evt.is_set():
                return self.node.set_stopped(response, 'Item handoff stopped')

            # A gesture made while the arm was moving must never release an item.
            self._clear_gesture()
            if not self._arm_gesture_detection():
                return self._stopped_or_failure(response, 'Item handoff failed: gesture detector unavailable')
            self.node.announce_handoff_ready('give_to_person')
            gesture = self._wait_for_gesture(timeout_s)
            if gesture is None:
                if self._stop_evt.is_set():
                    return self.node.set_stopped(response, 'Item handoff stopped')
                self._retract_with_object()
                return self.node.set_timeout(response, f'Item handoff timed out after {timeout_s:.1f}s; item retained')

            self.log.info(f'give_to_person: gesture accepted '
                          f'confidence={gesture["confidence"]:.2f}')
            if not self._open_gripper():
                return self._stopped_or_failure(response, 'Item handoff failed: unable to open gripper')
            self._gripper_opened = True
            self.motion.sleep(float(config.handoff.post_open_hold_seconds))
            if self._stop_evt.is_set():
                return self.node.set_stopped(response, 'Item handoff stopped')

            if not self._stow_after_release():
                if self._stop_evt.is_set():
                    return self.node.set_stopped(response, 'Item handoff stopped')
                return self.node.set_failure(response, 'Object released, but resting posture was not confirmed')
            if self._stop_evt.is_set():
                return self.node.set_stopped(response, 'Item handoff stopped')
            return self.node.set_success(response, 'Item handed to person')
        finally:
            self._disarm_gesture_detection()
            self._cleanup_arm()
            safe_to_restore = (not self._arm_motion_started or self._arm_retracted)
            if safe_to_restore:
                try:
                    self._orientation_restored = self._restore_handoff_orientation() is True
                except Exception as exc:
                    self.log.warning('give_to_person: failed to restore handoff orientation: '
                                     f'{exc}')
                try:
                    if bool(config.handoff.switch_to_navigation_mode_after):
                        self._navigation_mode_restored = self.motion.switch_to_navigation_mode(timeout_s=3.0) is True
                except Exception as exc:
                    self.log.warning('give_to_person: failed to restore navigation mode: '
                                     f'{exc}')
            elif not self._stop_evt.is_set():
                self.log.warning('give_to_person: arm retraction was not confirmed; '
                                 'leaving the base side-on')
            with self._lock:
                self.give_active = False

    def _stopped_or_failure(self, response, failure_message: str):
        if self._stop_evt.is_set():
            return self.node.set_stopped(response, 'Item handoff stopped')
        return self.node.set_failure(response, failure_message)

    def _move_to_handoff_pose(self) -> bool:
        return self._send_handoff_pose(preserve_gripper=True)

    def _open_gripper(self) -> bool:
        self._release_attempted = True
        return self.motion.open_gripper(duration_s=float(config.durations.open_seconds), wait=True)

    def _retract_with_object(self) -> bool:
        self.log.info('give_to_person: retracting while retaining item')
        self._arm_cleanup_attempted = True
        result = self.motion.safe_stow_arm(gripper_pos=float(config.gripper.closed), safe_lift_height=float(config.handoff.object_stow.safe_lift_height), preserve_gripper=True, )
        self._arm_retracted = bool(result)
        if not result:
            self.log.warning('give_to_person: unable to confirm retraction with item')
        return bool(result)

    def _stow_after_release(self) -> bool:
        self.log.info('give_to_person: stowing arm after release')
        self._arm_cleanup_attempted = True
        result = self.motion.stow_empty_gripper(safe_lift_height=float(config.handoff.release_stow.safe_lift_height))
        self._arm_retracted = bool(result)
        return bool(result)

    def _cleanup_arm(self):
        if self._release_attempted and not self._gripper_opened:
            return  # Unknown opening must not be replayed as part of cleanup.
        if (
                self._stop_evt.is_set() or not self._arm_motion_started or self._arm_retracted or self._arm_cleanup_attempted):
            return
        try:
            if self._release_attempted:
                self._stow_after_release()
            else:
                self._retract_with_object()
        except Exception as exc:
            self.log.warning(f'give_to_person: cleanup retraction failed: {exc}')

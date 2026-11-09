import json
import threading
from pathlib import Path

from robot_common import get_config

from action_service.common.motion import MotionHelper

config = get_config('action_service', Path(__file__).stem)


class WaveHandAction:
    """Simple arm/gripper waving gesture action."""

    def __init__(self, node):
        self.node = node
        self.log = node.log.sub('wave')
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self.wave_active = False
        self.motion = MotionHelper(node, config=config, stop_evt=self._stop_evt, log_prefix='wave')

    def stop(self):
        with self._lock:
            if not self.wave_active:
                return
            self.wave_active = False
        self.log.warning('WaveHandAction: external stop requested')
        self._stop_evt.set()

    def execute(self, request, response, parameters=None):
        del request
        params = self._parse_params(parameters)
        with self._lock:
            if self.wave_active:
                return self.node.set_success(response, 'Wave already in progress')
            self.wave_active = True
            self._stop_evt.clear()
        self.log.info(f'wave_hand: starting with params={params}')
        try:
            if not self.motion.wait_for_traj_server(timeout_s=float(config.timeouts.trajectory_server_seconds)):
                if self._stop_evt.is_set():
                    return self.node.set_stopped(response, 'Wave operation was stopped')
                return self.node.set_failure(response, 'Wave failed: trajectory controller not ready')
            if not self.motion.switch_to_position_mode(timeout_s=float(config.timeouts.position_mode_seconds)):
                self.log.warning('wave_hand: could not switch to position mode, trying gesture anyway')
            if self._stop_evt.is_set():
                return self.node.set_stopped(response, 'Wave operation was stopped')
            lift_height = self._float_param(params, 'height', float(config.pose.lift_height_m))
            lift_height = self._clamp(lift_height, float(config.lift.min), float(config.lift.max))
            extension = self._float_param(params, 'extension', float(config.pose.wrist_extension_m))
            extension = self._clamp(extension, float(config.arm.min), float(config.arm.max))
            repetitions = int(config.wave.repetitions)

            if not self._move_to_wave_pose(lift_height, extension):
                return self.node.set_stopped(response, 'Wave operation was stopped') if self._stop_evt.is_set() else self.node.set_failure(response, 'Wave failed: unable to reach wave pose')

            if not self._perform_wave(repetitions):
                return self.node.set_stopped(response, 'Wave operation was stopped') if self._stop_evt.is_set() else self.node.set_failure(response, 'Wave failed: trajectory command was rejected')

            if bool(config.pose.return_to_center):
                self._return_to_center(lift_height, extension)
            if self._stop_evt.is_set():
                return self.node.set_stopped(response, 'Wave operation was stopped')
            return self.node.set_success(response, 'Waved hand')
        finally:
            try:
                self.motion.switch_to_navigation_mode(timeout_s=float(config.timeouts.navigation_mode_seconds))
            except Exception as exc:
                self.log.warning(f'wave_hand: navigation mode switch failed: {exc}')
            with self._lock:
                self.wave_active = False

    def _move_to_wave_pose(self, lift_height: float, extension: float) -> bool:
        self.log.info(f'wave_hand: moving to lift={lift_height:.3f} extension={extension:.3f}')
        ok = self.motion.send_full_trajectory({
            'joint_lift': lift_height,
            'wrist_extension': extension,
            'joint_wrist_yaw': float(config.pose.yaw_center),
            'joint_wrist_pitch': float(config.pose.wrist_pitch),
            'joint_wrist_roll': float(config.pose.wrist_roll),
            config.gripper.joint: float(config.gripper.open), }, duration=float(config.pose.move_duration_s), wait=True)
        self.motion.sleep(float(config.pose.settle_seconds))
        return ok and not self._stop_evt.is_set()

    def _perform_wave(self, repetitions: int) -> bool:
        yaw_center = float(config.pose.yaw_center)

        yaw_left = yaw_center - 0.08
        yaw_right = yaw_center + 0.08

        wrist_pitch_upright = 1.57
        wrist_roll_upright = 0.0

        wave_duration = 0.28
        pose_duration = 0.6

        upright_pose = {
            'joint_wrist_yaw': yaw_center,
            'joint_wrist_pitch': wrist_pitch_upright,
            'joint_wrist_roll': wrist_roll_upright, }

        if not self.motion.send_full_trajectory(upright_pose, duration=pose_duration, wait=True):
            return False

        if self._stop_evt.is_set():
            return False

        for _ in range(repetitions):
            if not self.motion.send_full_trajectory({
                'joint_wrist_yaw': yaw_left,
                'joint_wrist_pitch': wrist_pitch_upright,
                'joint_wrist_roll': wrist_roll_upright, }, duration=wave_duration, wait=True):
                return False

            if self._stop_evt.is_set():
                return False

            if not self.motion.send_full_trajectory({
                'joint_wrist_yaw': yaw_right,
                'joint_wrist_pitch': wrist_pitch_upright,
                'joint_wrist_roll': wrist_roll_upright, }, duration=wave_duration, wait=True):
                return False

            if self._stop_evt.is_set():
                return False

        return True

    def _return_to_center(self, lift_height: float, extension: float) -> bool:
        del lift_height, extension

        yaw_center = float(config.pose.yaw_center)
        wrist_pitch_lowered = 0.0
        wrist_roll_upright = 0.0

        self.log.info(f'wave_hand: returning to lowered final pose '
                      f'yaw={yaw_center:.3f} pitch={wrist_pitch_lowered:.3f} roll={wrist_roll_upright:.3f}')

        return self.motion.send_full_trajectory({
            'joint_wrist_yaw': yaw_center,
            'joint_wrist_pitch': wrist_pitch_lowered,
            'joint_wrist_roll': wrist_roll_upright, }, duration=0.8, wait=True)

    @staticmethod
    def _parse_params(parameters) -> dict:
        if isinstance(parameters, dict):
            return parameters
        if isinstance(parameters, str) and parameters.strip():
            try:
                return json.loads(parameters)
            except json.JSONDecodeError:
                pass
        return {}

    @staticmethod
    def _float_param(params: dict, key: str, default: float) -> float:
        try:
            value = params.get(key, default)
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(float(low), min(float(value), float(high)))

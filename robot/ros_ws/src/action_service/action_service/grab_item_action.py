import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rclpy
from action_service.common.tf_utils import transform_point
from rclpy.duration import Duration
from robot_common import get_config
from sensor_msgs.msg import JointState

from action_service.common.math_utils import GrabberState, MedianFilter, clamp, rot_yxz, soft_step
from action_service.common.motion import MotionHelper

config = get_config('action_service', Path(__file__).stem)


@dataclass
class GrabCandidate:
    center: np.ndarray
    label: str
    score: float
    grasp_w: float
    distance_from_previous: float
    shape_profile: dict


class GrabItemAction:

    def __init__(self, node):
        self.node = node
        self.log = node.log.sub('grab')
        self.lock = threading.Lock()
        self._op_thread = None
        self._stop_evt = threading.Event()
        self._done_evt = threading.Event()
        self.motion = MotionHelper(node, config=config, stop_evt=self._stop_evt, joint_positions_getter=self._joint_positions_snapshot, log_prefix='grab', )
        self.current_joint_pos = {}
        self.filt_target = MedianFilter(n=5)
        self.filt_between = MedianFilter(n=5)
        self.target_object_type = ''
        self.grab_active = False
        self.grab_result = None
        self._reset_state_locked()

    def _parse_execute_parameters(self, parameters):
        params_dict = {}
        if isinstance(parameters, dict):
            params_dict = parameters
        elif isinstance(parameters, str) and parameters.strip():
            try:
                params_dict = json.loads(parameters)
            except json.JSONDecodeError:
                self.log.warning(f'Failed to parse parameters JSON: {parameters}')
        return (params_dict.get('object_type') or params_dict.get('item') or '').lower().strip()

    def execute(self, request, response, parameters):
        obj = self._parse_execute_parameters(parameters)
        if not obj:
            return self.node.set_failure(response, 'Grab failed: missing required item/object_type parameter')
        with self.lock:
            if self.grab_active:
                return self.node.set_failure(response, 'Grab already in progress')
            self._reset_state_locked()
            self.target_object_type = obj
            self.grab_active = True
            self.grab_result = None
            self._stop_evt.clear()
            self._done_evt.clear()
        self.log.info(f'grab_item: starting for {obj!r}')
        self._op_thread = threading.Thread(target=self._run_grab_sequence, args=(float(config.timeouts.grab_seconds),), daemon=False, )
        self._op_thread.start()
        finished = self._done_evt.wait(timeout=float(config.timeouts.grab_seconds) + 25.0)
        if not finished:
            self._stop_evt.set()
            if self._op_thread is not None:
                self._op_thread.join(timeout=5.0)
            with self.lock:
                self.grab_active = False
                self.grab_result = self.grab_result or 'timeout'
                self.state = GrabberState.FINISHED
        with self.lock:
            result = self.grab_result
            final_obj = self.target_object_type
        details = json.dumps({'grasp_completed': self._grasp_completed,
            'arm_stowed': self._arm_stowed,
            'navigation_mode_restored': self._navigation_mode_restored,
            'cleanup_error': self._cleanup_error})
        if result == 'success':
            return self.node.set_success(response, f'Successfully grabbed {final_obj}', details)
        elif result == 'cleanup_failed':
            return self.node.set_failure(response,
                f'Grasp completed, but {self._cleanup_error}', details)
        elif result == 'timeout':
            return self.node.set_timeout(response, f'Grab operation timed out after {config.timeouts.grab_seconds}s')
        elif result == 'not_ready':
            return self.node.set_failure(response, 'Grab failed: controller/joints not ready')
        elif result == 'stopped':
            return self.node.set_stopped(response, 'Grab operation was stopped')
        return self.node.set_failure(response, f'Grab operation failed for {final_obj}')

    def stop(self):
        with self.lock:
            if not self.grab_active:
                return
        self.log.warning('GrabItemAction: external stop requested')
        self._stop_evt.set()
        if self._op_thread is not None:
            self._op_thread.join(timeout=3.0)
        with self.lock:
            self.grab_active = False
            self.grab_result = 'stopped'
            self.state = GrabberState.FINISHED
        self._done_evt.set()

    def handle_joint_state(self, msg: JointState):
        with self.lock:
            for i, name in enumerate(msg.name):
                if i < len(msg.position):
                    self.current_joint_pos[name] = float(msg.position[i])

    def handle_detection(self, msg):
        with self.lock:
            if not self.grab_active:
                return
            target_type = self.target_object_type
        try:
            envelope = json.loads(msg.data)
        except (json.JSONDecodeError, AttributeError):
            return
        detections = envelope.get('detections', [])
        now = self.node.get_clock().now()
        with self.lock:
            prev_tgt = None if self.target_base is None else self.target_base.copy()
        candidates = []
        for det in detections:
            label, score = self._parse_detection(det)
            is_match = self._is_target(label, target_type)
            if not is_match:
                continue
            center, _size_xy, grasp_w, shape_profile = self._estimate_object_center_base(det)
            d = float(np.linalg.norm(center - prev_tgt)) if prev_tgt is not None else 0.0
            candidates.append(GrabCandidate(center=center, label=label, score=float(score), grasp_w=float(grasp_w), distance_from_previous=d, shape_profile=shape_profile, ))
        if not candidates:
            return
        best_val = -1e9
        chosen = None
        for c in candidates:
            if prev_tgt is not None and c.distance_from_previous > 0.55:
                continue
            profile_bonus = 0.04 if c.shape_profile.get('has_grip_point') else 0.0
            val = float(c.score) - 0.8 * float(c.distance_from_previous) + profile_bonus
            if val > best_val:
                best_val = val
                chosen = c
        if chosen is None:
            return
        center = chosen.center
        label = chosen.label
        grasp_w = chosen.grasp_w
        shape_profile = chosen.shape_profile
        if prev_tgt is not None and float(np.linalg.norm(center - prev_tgt)) > 0.65:
            return
        center_f = self.filt_target.update(center)
        with self.lock:
            prev_center = self.target_base
            err_to_prev = 0.0
            if prev_center is not None:
                err_to_prev = float(np.linalg.norm(center_f - prev_center))
            close_to_target = False
            if self.state == GrabberState.SERVOING and self.last_err_norm is not None:
                close_to_target = self.last_err_norm < 0.08
            if close_to_target and err_to_prev < 0.04:
                blend = 0.15
                center_f = (blend * center_f + (1.0 - blend) * prev_center).astype(np.float32)
            self.last_detection_time = now
            self.target_base = center_f
            self.last_grasp_w = float(grasp_w)
            self.last_shape_profile = dict(shape_profile)
            if self.state == GrabberState.SEARCHING:
                if len(self.filt_target.buf) >= 3:
                    self.log.info(f"Detected '{label}' (score={score:.2f}) with "
                                  f'{len(self.filt_target.buf)} confirmations -> SERVOING; '
                                  f'{self._profile_summary(shape_profile)}')
                    self._enter_state_locked(GrabberState.SERVOING)

    def _run_grab_sequence(self, timeout_s: float):
        t0 = time.monotonic()
        result = 'failed'
        try:
            if not self._wait_for_traj_server(timeout_s=6.0):
                result = 'stopped' if self._stop_evt.is_set() else 'not_ready'
                return
            if not self._wait_for_joints(timeout_s=4.0):
                result = 'stopped' if self._stop_evt.is_set() else 'not_ready'
                return
            if self._stop_evt.is_set():
                result = 'stopped'
                return
            self.log.info('grab_item: switching to position mode...')
            if not self._switch_to_position_mode():
                self.log.warning('grab_item: position mode switch may have failed, continuing anyway')
            self._sleep_with_stop(0.5)
            if self._stop_evt.is_set():
                result = 'stopped'
                return
            self.log.info('grab_item: stowing arm...')
            self._stow_arm(close_gripper=True)
            if self._stop_evt.is_set():
                result = 'stopped'
                return
            self._move_arm_up()
            if self._stop_evt.is_set():
                result = 'stopped'
                return
            self._initialize_grab_pose()
            with self.lock:
                self._enter_state_locked(GrabberState.SEARCHING)
            while not self._stop_evt.is_set():
                if (time.monotonic() - t0) >= float(timeout_s):
                    result = 'timeout'
                    break
                self._control_loop()
                with self.lock:
                    if self.state == GrabberState.FINISHED and self.grab_result == 'success':
                        self._grasp_completed = True
                        result = 'success'
                        break
                self._sleep_with_stop(1.0 / float(config.control.rate_hz))
            if self._stop_evt.is_set() and result == 'failed':
                with self.lock:
                    result = self.grab_result or 'stopped'
        except Exception as e:
            self.log.error(f'grab_item: exception: {e}')
            result = 'failed'
        finally:
            if self._stop_evt.is_set():
                result = 'stopped'
            try:
                if result == 'success':
                    self.log.info('grab_item: grab successful, stowing with object...')
                if result != 'stopped':
                    self._arm_stowed = self._safe_stow_arm(close_gripper=True)
                    if not self._arm_stowed:
                        self._cleanup_error = 'arm stow was not confirmed'
                # A mode change needs the driver's write lock. An object-blocked
                # close trajectory can still own its read lock after retraction.
                # Stow first, then await all results before changing mode.
                if not self.motion.wait_for_motion_completion(timeout_s=1.5):
                    self._cleanup_error = self._cleanup_error or 'controller completion was not confirmed'
                else:
                    self._navigation_mode_restored = self._switch_to_navigation_mode()
                    if not self._navigation_mode_restored:
                        self._cleanup_error = self._cleanup_error or 'navigation mode restoration was not confirmed'
            except Exception as exc:
                self._cleanup_error = f'arm cleanup failed: {exc}'
                self.log.exception(self._cleanup_error)
            if result == 'success' and self._cleanup_error:
                result = 'cleanup_failed'
            with self.lock:
                if self.grab_result == 'stopped' or self._stop_evt.is_set():
                    result = 'stopped'
                self.grab_result = result
                self.grab_active = False
                self.state = GrabberState.FINISHED
            self._done_evt.set()
            self.log.info(f'grab_item: done result={result}')

    def _reset_state_locked(self):
        self.state = GrabberState.IDLE
        self.state_enter_time = None
        self.target_base = None
        self.last_err_cam = np.zeros(3, dtype=np.float32)
        self.filt_target.reset()
        self.filt_between.reset()
        self.grip_cmd = float(config.gripper.open.max)
        self.last_grasp_w = 0.06
        self.last_shape_profile = self._default_shape_profile()
        self.stable_frames = 0
        self.last_err_norm = None
        self.last_send_time = 0.0
        self.grasp_sent = False
        self._grasp_attempted = False
        self._grasp_completed = False
        self._arm_stowed = False
        self._navigation_mode_restored = False
        self._cleanup_error = None
        self.grasp_close_until = 0.0
        self.grasp_hold_started_at = 0.0
        self.grip_prev = None
        self.grip_settle = 0
        self.last_detection_time = None
        self._base_align_count = 0
        self._base_align_cooldown = 0.0

    def _enter_state_locked(self, st):
        self.state = st
        self.state_enter_time = self.node.get_clock().now()
        self.stable_frames = 0
        self.last_err_norm = None
        if st == GrabberState.GRASPING:
            self.grasp_sent = False
            self.grasp_close_until = 0.0
            self.grasp_hold_started_at = 0.0
            self.grip_prev = None
            self.grip_settle = 0
        self.log.info(f'GrabItem -> {st.name}')

    def _sleep_with_stop(self, seconds: float):
        self.motion.sleep(seconds)

    def _joint_positions_snapshot(self):
        with self.lock:
            return dict(self.current_joint_pos)

    def _wait_for_traj_server(self, timeout_s: float) -> bool:
        return self.motion.wait_for_traj_server(timeout_s)

    def _wait_for_joints(self, timeout_s: float) -> bool:
        need = set(config.joints.control)
        t0 = time.monotonic()
        while not self._stop_evt.is_set() and (time.monotonic() - t0) < float(timeout_s):
            with self.lock:
                have = need.issubset(set(self.current_joint_pos.keys()))
            if have:
                return True
            time.sleep(0.02)
        return False

    def _switch_to_position_mode(self) -> bool:
        return self.motion.switch_to_position_mode(timeout_s=3.0)

    def _switch_to_navigation_mode(self) -> bool:
        success = self.motion.switch_to_navigation_mode(timeout_s=3.0)
        if success:
            self.log.info('grab_item: switched to navigation mode')
        else:
            self.log.warning('grab_item: failed to switch to navigation mode')
        return success

    def _safe_stow_arm(self, close_gripper: bool = True):
        gripper_pos = config.gripper.closed if close_gripper else config.gripper.open.max
        self.log.info('grab_item: safe stow — retract, lift+rotate, then lower...')
        return self.motion.safe_stow_arm(gripper_pos=gripper_pos, preserve_gripper=close_gripper)

    def _align_base_to_target(self, target_base):
        EXPECTED_BEARING = -math.pi / 2
        MAX_CORRECTION = math.radians(45)
        BEARING_TOLERANCE = math.radians(8)
        ANGULAR_VEL = 0.3
        bearing = math.atan2(float(target_base[1]), float(target_base[0]))
        bearing_err = bearing - EXPECTED_BEARING
        while bearing_err > math.pi:
            bearing_err -= 2 * math.pi
        while bearing_err < -math.pi:
            bearing_err += 2 * math.pi
        if abs(bearing_err) < BEARING_TOLERANCE:
            return False
        if abs(bearing_err) > MAX_CORRECTION:
            bearing_err = math.copysign(MAX_CORRECTION, bearing_err)
        self.log.info(f'grab_item: target at bearing {math.degrees(bearing):.1f}° '
                      f'(expected {math.degrees(EXPECTED_BEARING):.1f}°, '
                      f'error {math.degrees(bearing_err):.1f}°), rotating base to align')
        self._send_trajectory({
            'joint_wrist_yaw': 0.0,
            'joint_wrist_pitch': config.wrist.pitch.search,
            'joint_wrist_roll': 0.0,
            'wrist_extension': config.arm.min,
            config.gripper.joint: config.gripper.open.max, }, duration=1.0, wait=True, )
        self._sleep_with_stop(0.2)
        if not self._switch_to_navigation_mode():
            self.log.warning('grab_item: failed to switch to nav mode for base alignment')
            self._base_align_cooldown = time.time() + 5.0
            return False
        self._sleep_with_stop(0.3)
        self.motion.rotate_base(bearing_err, ANGULAR_VEL)
        self._sleep_with_stop(0.3)
        if not self._switch_to_position_mode():
            self.log.warning('grab_item: failed to switch back to position mode after alignment')
        self._send_trajectory({
            'joint_wrist_yaw': 0.0,
            'joint_wrist_pitch': config.wrist.pitch.search,
            'joint_wrist_roll': 0.0,
            'wrist_extension': config.arm.min,
            config.gripper.joint: config.gripper.open.max, }, duration=1.0, wait=True, )
        self._sleep_with_stop(0.3)
        self._base_align_count += 1
        self._base_align_cooldown = time.time() + 3.0
        with self.lock:
            self.target_base = None
            self.filt_target.reset()
            self.filt_between.reset()
            self.stable_frames = 0
            self.last_err_norm = None
            self._enter_state_locked(GrabberState.SEARCHING)
        return True

    def _stow_arm(self, close_gripper: bool = True):
        stow_client = getattr(self.node, 'stow_client', None)
        if stow_client is None:
            self.log.warning('grab_item: stow_client not available, skipping stow')
            return
        if close_gripper:
            self._send_trajectory({config.gripper.joint: config.gripper.closed}, duration=1.2, wait=True)
        if not self.motion.call_trigger(stow_client, timeout_s=8.0, warn_name='stow'):
            self.log.warning('grab_item: stow service failed or timed out')
            return
        self.log.info('grab_item: stow_the_robot completed')
        self._sleep_with_stop(0.5)

    def _move_arm_up(self):
        self.log.info('grab_item: lifting arm, then rotating wrist...')
        lift_target = config.lift.max - 0.03
        self._send_trajectory({'joint_lift': lift_target}, duration=3.0, wait=True)
        if self._stop_evt.is_set():
            return
        self._send_trajectory({
            'joint_lift': lift_target,
            'joint_wrist_yaw': 0.0,
            'joint_wrist_pitch': config.wrist.pitch.search,
            'joint_wrist_roll': 0.0, }, duration=2.5, wait=True)
        self._sleep_with_stop(0.3)

    def _initialize_grab_pose(self):
        self.log.info('grab_item: opening gripper for grab pose...')
        lift_target = config.lift.max
        self._send_trajectory({
            'joint_lift': lift_target,
            'joint_wrist_yaw': 0.0,
            'joint_wrist_pitch': config.wrist.pitch.search,
            'joint_wrist_roll': 0.0,
            'wrist_extension': config.arm.min,
            config.gripper.joint: config.gripper.open.max, }, duration=2.0, wait=True, )
        self._sleep_with_stop(0.3)

    def _send_trajectory(self, positions, duration=None, wait=False):
        if duration is None:
            duration = config.control.trajectory_dt
        return self.motion.send_full_trajectory(positions, duration=float(duration), wait=wait)

    def _rate_limit_send(self, cmd, duration, force=False):
        t = time.time()
        if (not force) and (t - self.last_send_time) < (1.0 / config.control.send_rate_hz):
            return
        self.last_send_time = t
        scaled_duration = duration / max(0.1, float(config.control.speed_factor))
        self._send_trajectory(cmd, duration=scaled_duration)

    def _default_shape_profile(self):
        return {
            'has_grip_point': False,
            'elongation_score': 0.0,
            'depth_fraction': 0.5,
            'target_pitch': float(config.wrist.pitch.down),
            'search_pitch': float(config.wrist.pitch.search),
            'push_scale': 1.0,
            'lift_lower_m': 0.0,
            'target_z_bias_m': 0.0, }

    @staticmethod
    def _lerp(a, b, t):
        t = float(np.clip(t, 0.0, 1.0))
        return float(a + (b - a) * t)

    def _motion_axes(self, yaw, pitch, roll):
        R = rot_yxz(float(yaw), float(-pitch), float(-roll))
        axis_lift = (R @ np.array([0.0, -1.0, 0.0], dtype=np.float32)).astype(np.float32)
        axis_arm = (R @ np.array([0.0, 0.0, 1.0], dtype=np.float32)).astype(np.float32)
        return axis_lift, axis_arm

    def _carry_pitch(self, shape_profile):
        grasp_pitch = float(shape_profile.get('target_pitch', config.wrist.pitch.down))
        return max(grasp_pitch, float(config.wrist.pitch.down))

    def _parse_detection(self, det):
        label = (det.get('label') or '').lower().strip()
        score = float(det.get('confidence', 0.0))
        return label, score

    @staticmethod
    def _is_ball_label(label: str) -> bool:
        words = set((label or '').lower().replace('_', ' ').replace('-', ' ').split())
        return bool(words & {'ball'})

    @staticmethod
    def _profile_summary(shape_profile: dict) -> str:
        return (f"shape={shape_profile.get('shape', 'object')} "
                f"topdown={float(shape_profile.get('topdown_score', 0.0)):.2f} "
                f"elongation={float(shape_profile.get('elongation_score', 0.0)):.2f} "
                f"z_bias={float(shape_profile.get('target_z_bias_m', 0.0)):.3f}m "
                f"lift_lower={float(shape_profile.get('lift_lower_m', 0.0)):.3f}m "
                f"push_scale={float(shape_profile.get('push_scale', 1.0)):.2f}")

    def _is_target(self, label: str, target_type: str):
        s = (label or '').lower().strip()
        t = (target_type or '').lower().strip()
        if not s:
            return False
        if not t:
            return True
        if s == t:
            return True
        if t in s or s in t:
            return True
        t_words = set(t.replace('_', ' ').replace('-', ' ').split())
        s_words = set(s.replace('_', ' ').replace('-', ' ').split())
        if t_words & s_words:
            return True
        return False

    def _shape_profile_from_detection(self, det, sx, sy, grip_point_valid=False):
        profile = self._default_shape_profile()
        profile['has_grip_point'] = bool(grip_point_valid)
        label, _score = self._parse_detection(det)
        is_ball = self._is_ball_label(label)
        dims = [float(abs(sx)), float(abs(sy))]
        size = det.get('size_3d') or {}
        sz = float(abs(size.get('z', 0.0)))
        if sz > 1e-6:
            dims.append(sz)
        nonzero_dims = [d for d in dims if d > 1e-6]
        volumetric_ratio = 0.0
        elongation_ratio = 1.0
        if nonzero_dims:
            volumetric_ratio = float(min(nonzero_dims) / max(nonzero_dims))
            elongation_ratio = float(max(nonzero_dims) / max(min(nonzero_dims), 1e-6))
        planar_ratio = float(max(float(sx), float(sy)) / max(min(float(sx), float(sy)), 1e-6))
        elongation_ratio = max(elongation_ratio, planar_ratio)
        profile['elongation_score'] = float(np.clip((elongation_ratio - 1.8) / 1.7, 0.0, 1.0))
        planar_compact = float(np.clip((1.55 - planar_ratio) / 0.55, 0.0, 1.0))
        volumetric = float(np.clip((volumetric_ratio - 0.25) / 0.50, 0.0, 1.0))
        fallback_topdown = float(np.clip(0.60 * volumetric + 0.40 * planar_compact, 0.0, 1.0))

        def _apply_profile(topdown_score):
            if is_ball:
                topdown_score = max(float(topdown_score), float(config.grasp.ball.topdown_min_score))
            topdown_score = float(np.clip(topdown_score, 0.0, 1.0))
            grasp_span = float(np.clip(min(nonzero_dims) if nonzero_dims else min(sx, sy), 0.02, 0.12))
            depth_fraction = self._lerp(0.62, 0.36, topdown_score)
            lower_m = float(config.grasp.ball.lower_m) if is_ball else float(config.grasp.topdown_lower_m)
            downward_bias = float(np.clip(0.18 * grasp_span * topdown_score, 0.0, 0.018))
            target_z_bias_m = float(config.grasp.ball.target_z_bias_m) if is_ball else self._lerp(-0.006, downward_bias, topdown_score)
            shallow_grasp_score = max(float(topdown_score), float(profile.get('elongation_score', 0.0)))
            push_scale = self._lerp(1.0, 0.12, shallow_grasp_score)
            if is_ball:
                push_scale = float(np.clip(push_scale, float(config.grasp.ball.push_scale_min), float(config.grasp.ball.push_scale_max)))
            target_pitch = self._lerp(config.wrist.pitch.down, config.wrist.pitch.down_steep, topdown_score)
            search_pitch = self._lerp(config.wrist.pitch.search, config.wrist.pitch.search_steep, topdown_score)
            if is_ball:
                target_pitch = float(config.wrist.pitch.down)
                search_pitch = float(config.wrist.pitch.search)
            profile.update({
                'shape': 'ball' if is_ball else 'object',
                'topdown_score': float(topdown_score),
                'depth_fraction': depth_fraction,
                'target_pitch': target_pitch,
                'search_pitch': search_pitch,
                'push_scale': push_scale,
                'lift_lower_m': lower_m * topdown_score,
                'target_z_bias_m': target_z_bias_m, })
            return profile

        seg = det.get('segmentation_2d') or {}
        points = seg.get('points') or []
        if len(points) < 3:
            return _apply_profile(0.55 * fallback_topdown)
        pts = np.asarray(points, dtype=np.float32)
        if pts.ndim != 2 or pts.shape[0] < 3:
            return _apply_profile(0.55 * fallback_topdown)
        bbox = seg.get('bbox') or det.get('bbox_2d') or {}
        bw = float(abs(bbox.get('width', 0.0)))
        bh = float(abs(bbox.get('height', 0.0)))
        if bw < 1e-6 or bh < 1e-6:
            return _apply_profile(0.55 * fallback_topdown)
        rolled = np.roll(pts, -1, axis=0)
        cross = pts[:, 0] * rolled[:, 1] - rolled[:, 0] * pts[:, 1]
        area = 0.5 * abs(float(np.sum(cross)))
        perimeter = float(np.sum(np.linalg.norm(rolled - pts, axis=1)))
        bbox_area = max(bw * bh, 1e-6)
        aspect_ratio = float(max(bw, bh) / max(min(bw, bh), 1e-6))
        fill_ratio = float(np.clip(area / bbox_area, 0.0, 1.5))
        circularity = 0.0
        if perimeter > 1e-6:
            circularity = float(np.clip((4.0 * math.pi * area) / (perimeter * perimeter), 0.0, 1.2))
        compact_2d = float(np.clip((1.65 - aspect_ratio) / 0.65, 0.0, 1.0))
        round_2d = float(np.clip((circularity - 0.45) / 0.35, 0.0, 1.0))
        full_2d = float(np.clip((fill_ratio - 0.45) / 0.35, 0.0, 1.0))
        seg_score = 0.45 * compact_2d + 0.35 * round_2d + 0.20 * full_2d
        elongated_gate = float(np.clip((1.75 - aspect_ratio) / 0.55, 0.0, 1.0))
        topdown_score = float(np.clip((0.65 * seg_score + 0.35 * volumetric) * elongated_gate, 0.0, 1.0))
        return _apply_profile(topdown_score)

    def _lookup_transform(self, target, source, timeout_s=0.08):
        try:
            return self.node.tf_buffer.lookup_transform(target, source, rclpy.time.Time(), timeout=Duration(seconds=float(timeout_s)), )
        except Exception:
            return None

    def _point_to_frame(self, p_xyz, from_frame, to_frame):
        return transform_point(self.node, p_xyz, from_frame, to_frame, config.timeouts.tf_timeout_seconds)

    def _camera_forward_in_base(self):
        tf_cam = self._lookup_transform(config.frames.base, config.frames.camera, timeout_s=config.timeouts.tf_timeout_seconds)
        if tf_cam is None:
            return None, None
        q = tf_cam.transform.rotation
        x, y, z, w = float(q.x), float(q.y), float(q.z), float(q.w)
        xx, yy, zz = x * x, y * y, z * z
        xy, xz, yz = x * y, x * z, y * z
        wx, wy, wz = w * x, w * y, w * z
        R = np.array([[1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy)],
                      [2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx)],
                      [2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy)], ], dtype=np.float32, )
        f = R @ np.array([0.0, 0.0, 1.0], dtype=np.float32)
        n = float(np.linalg.norm(f))
        if n < 1e-6:
            return None, None
        return (f / n).astype(np.float32), tf_cam

    def _get_between_fingertips_base(self):
        tfl = self._lookup_transform(config.frames.base, config.frames.fingertip_left, timeout_s=config.timeouts.tf_timeout_seconds)
        tfr = self._lookup_transform(config.frames.base, config.frames.fingertip_right, timeout_s=config.timeouts.tf_timeout_seconds)
        if tfl is None or tfr is None:
            self.log.warning(f'grab_item: fingertip TF missing - left={tfl is not None}, right={tfr is not None}, using camera fallback')
            tf_cam = self._lookup_transform(config.frames.base, config.frames.camera, timeout_s=config.timeouts.tf_timeout_seconds)
            if tf_cam is None:
                return None
            tc = tf_cam.transform.translation
            cam_pos = np.array([float(tc.x), float(tc.y), float(tc.z)], dtype=np.float32)
            fwd, _ = self._camera_forward_in_base()
            if fwd is not None:
                cam_pos = cam_pos + 0.12 * fwd
            return self.filt_between.update(cam_pos)
        pl = tfl.transform.translation
        pr = tfr.transform.translation
        mid = np.array([(pl.x + pr.x) * 0.5, (pl.y + pr.y) * 0.5, (pl.z + pr.z) * 0.5], dtype=np.float32)
        fwd, _ = self._camera_forward_in_base()
        if fwd is not None:
            mid = mid + float(config.grasp.marker_to_tip_forward_m) * fwd
        return self.filt_between.update(mid)

    def _estimate_object_center_base(self, det):
        pos = det.get('position_3d', {})
        surf = np.array([float(pos.get('x', 0)), float(pos.get('y', 0)), float(pos.get('z', 0))], dtype=np.float32)
        size = det.get('size_3d') or {}
        sx = float(abs(size.get('x', 0.0)))
        sy = float(abs(size.get('y', 0.0)))
        sx = float(np.clip(sx, 0.02, 0.40))
        sy = float(np.clip(sy, 0.02, 0.40))
        grasp_w = float(np.clip(min(sx, sy), 0.03, 0.11))
        grip_point = det.get('grip_point') or {}
        grip_xyz = np.array([float(grip_point.get('x', 0.0)),
                             float(grip_point.get('y', 0.0)),
                             float(grip_point.get('z', 0.0)), ], dtype=np.float32, )
        grip_valid = bool(np.isfinite(grip_xyz).all() and float(np.linalg.norm(grip_xyz)) > 1e-6)
        shape_profile = self._shape_profile_from_detection(det, sx, sy, grip_point_valid=grip_valid)
        half_depth = float(np.clip(float(shape_profile.get('depth_fraction', 0.5)) * grasp_w, 0.012, 0.065, ))
        if np.isfinite(grip_xyz).all() and float(np.linalg.norm(grip_xyz)) > 1e-6:
            return grip_xyz, (sx, sy), grasp_w, shape_profile
        tf_cam = self._lookup_transform(config.frames.base, config.frames.camera, timeout_s=config.timeouts.tf_timeout_seconds)
        if tf_cam is None:
            center = surf.copy()
            center[2] -= float(shape_profile.get('target_z_bias_m', 0.0))
            return center, (sx, sy), grasp_w, shape_profile
        tc = tf_cam.transform.translation
        cam_o = np.array([float(tc.x), float(tc.y), float(tc.z)], dtype=np.float32)
        v = surf - cam_o
        n = float(np.linalg.norm(v))
        if n < 1e-6:
            center = surf.copy()
            center[2] -= float(shape_profile.get('target_z_bias_m', 0.0))
            return center, (sx, sy), grasp_w, shape_profile
        ray = v / n
        center = surf + half_depth * ray
        center = center.astype(np.float32)
        center[2] -= float(shape_profile.get('target_z_bias_m', 0.0))
        return center, (sx, sy), grasp_w, shape_profile

    def _max_steps(self, err_norm):
        d = float(np.clip(float(err_norm), 0.0, 0.90))
        s = float(np.clip(d / 0.45, 0.0, 1.0))
        s = 1.0 - (1.0 - s) * (1.0 - s)
        yaw = 0.004 + s * (0.06 - 0.004)
        pit = 0.004 + s * (0.055 - 0.004)
        lif = 0.002 + s * (0.035 - 0.002)
        arm = 0.002 + s * (0.045 - 0.002)
        rol = 0.003 + s * (0.02 - 0.003)
        k = float(config.control.speed_factor)
        return float(yaw * k), float(pit * k), float(lif * k), float(arm * k), float(rol * k)

    def _desired_gripper_open(self, grasp_w_m, err_norm):
        w = float(np.clip(float(grasp_w_m), 0.03, 0.11))
        base_open = config.gripper.open.min + config.gripper.open.per_meter * (
                w - config.gripper.open.reference_width_m)
        base_open = float(np.clip(base_open, config.gripper.open.min, config.gripper.open.max))
        safety_margin = 0.08
        safe_open = min(base_open + safety_margin, config.gripper.open.max)
        if float(err_norm) > 0.06:
            return safe_open
        if float(err_norm) > 0.03:
            return min(safe_open, config.gripper.open.near_cap_1)
        return min(safe_open, config.gripper.open.near_cap_2)

    def _control_loop(self):
        with self.lock:
            st = self.state
            tgt_base = None if self.target_base is None else self.target_base.copy()
            last_det = self.last_detection_time
            js = dict(self.current_joint_pos)
            grasp_w = float(self.last_grasp_w)
            shape_profile = dict(self.last_shape_profile)
        if st in (GrabberState.IDLE, GrabberState.FINISHED):
            return
        now = self.node.get_clock().now()
        wall = time.time()
        yaw = float(js.get('joint_wrist_yaw', 0.0))
        pitch = float(js.get('joint_wrist_pitch', 0.0))
        roll = float(js.get('joint_wrist_roll', 0.0))
        lift = float(js.get('joint_lift', 0.6))
        arm = float(js.get('wrist_extension', 0.05))
        grip_pos = js.get(config.gripper.joint, None)
        if st == GrabberState.SEARCHING:
            t = wall
            base_pitch = float(shape_profile.get('search_pitch', config.wrist.pitch.search))
            yaw_cmd = 0.0 + 0.30 * np.sin(2.0 * np.pi * 0.16 * t)
            pitch_cmd = base_pitch + 0.06 * np.sin(2.0 * np.pi * 0.09 * t + 0.8)
            self.grip_cmd = float(config.gripper.open.max)
            self._rate_limit_send({
                'joint_wrist_yaw': clamp(yaw_cmd, config.wrist.yaw.min, config.wrist.yaw.max),
                'joint_wrist_pitch': clamp(pitch_cmd, config.wrist.pitch.min, config.wrist.pitch.max),
                'joint_wrist_roll': 0.0,
                'wrist_extension': config.arm.min,
                config.gripper.joint: self.grip_cmd, }, duration=config.control.trajectory_dt, )
            return
        if st == GrabberState.SERVOING:
            with self.lock:
                close_to_target = self.last_err_norm is not None and self.last_err_norm < 0.10
            if tgt_base is None:
                with self.lock:
                    self._enter_state_locked(GrabberState.SEARCHING)
                return
            if tgt_base is not None and self._base_align_count < 3 and wall > self._base_align_cooldown:
                if not close_to_target and self._align_base_to_target(tgt_base):
                    return
            if last_det is not None:
                age_det = (now - last_det).nanoseconds / 1e9
                if age_det > config.control.no_detection_timeout_seconds:
                    with self.lock:
                        self.target_base = None
                        self.filt_target.reset()
                        self._enter_state_locked(GrabberState.SEARCHING)
                    return
            between_base = self._get_between_fingertips_base()
            if between_base is None:
                self.log.warning('grab_item: fingertip TF lookup failed (between_base is None)')
                return
            bet_cam = self._point_to_frame(between_base, config.frames.base, config.frames.camera)
            tgt_cam = self._point_to_frame(tgt_base, config.frames.base, config.frames.camera)
            if tgt_cam is None or bet_cam is None:
                self.log.warning(f'grab_item: TF transform failed (tgt_cam={tgt_cam is not None}, bet_cam={bet_cam is not None})')
                return
            err_cam = (tgt_cam - bet_cam).astype(np.float32)
            ex = float(err_cam[0])
            ey = float(err_cam[1])
            ez = float(err_cam[2])
            ey_ctrl = float(ey - config.grasp.y_target_bias_m)
            ez_ctrl = float(ez - config.grasp.z_target_m)
            deadband_z = config.alignment.deadband.z
            if self.last_err_norm is not None and self.last_err_norm < 0.08:
                deadband_z = config.alignment.deadband.z * 2.5
            if abs(ex) < config.alignment.deadband.x:
                ex = 0.0
            if abs(ey_ctrl) < config.alignment.deadband.y:
                ey_ctrl = 0.0
            if abs(ez_ctrl) < deadband_z:
                ez_ctrl = 0.0
            err_norm = float(np.linalg.norm(np.array([ex, ey_ctrl, ez_ctrl], dtype=np.float32)))
            max_yaw, max_pit, max_lif, max_arm, max_rol = self._max_steps(err_norm)
            yaw_step = soft_step(-ex, config.alignment.gains.yaw, max_yaw)
            roll_step = soft_step(-roll, config.alignment.gains.roll, max_rol)
            axis_lift, axis_arm = self._motion_axes(yaw, pitch, roll)
            err_trans = np.array([0.0, float(ey_ctrl), float(ez_ctrl)], dtype=np.float32)
            g = float(np.tanh(err_norm / 0.12))
            raw_lift = float(np.dot(axis_lift, err_trans) * g)
            raw_arm = float(np.dot(axis_arm, err_trans) * g)
            scale = 1.0
            al = abs(raw_lift)
            aa = abs(raw_arm)
            if al > 1e-9:
                scale = min(scale, float(max_lif / al))
            if aa > 1e-9:
                scale = min(scale, float(max_arm / aa))
            scale = float(np.clip(scale, 0.0, 1.0))
            lift_step = float(raw_lift * scale)
            arm_step = float(raw_arm * scale)
            if err_norm < 0.06:
                arm_step = arm_step * 0.4
                lift_step = lift_step * 0.5
            if ez_ctrl < -0.015:
                back_need = float(np.clip(abs(ez_ctrl) * 2.4, 0.015, 0.11))
                arm_step = min(float(arm_step), -back_need)
            target_pitch = float(shape_profile.get('target_pitch', config.wrist.pitch.down))
            pitch_bias_step = soft_step((target_pitch - pitch), config.alignment.gains.pitch_bias, max_pit * 0.9)
            view_scale = float(np.clip((0.18 - err_norm) / 0.18, 0.0, 1.0))
            pitch_view_step = soft_step(-ey_ctrl, config.alignment.gains.pitch_view, max_pit) * float(0.35 * view_scale)
            pitch_step = clamp(pitch_bias_step + pitch_view_step, -max_pit, max_pit)
            aligned = ((abs(ex) <= config.alignment.thresholds.x) and (
                    abs(ey_ctrl) <= config.alignment.thresholds.y) and (
                               abs(ez_ctrl) <= config.alignment.thresholds.z) and (
                               abs(pitch - target_pitch) <= config.alignment.thresholds.pitch_error))
            self.last_err_norm = err_norm
            if aligned:
                self.stable_frames += 1
            elif self.stable_frames > 0:
                self.stable_frames = max(0, self.stable_frames - 1)
            with self.lock:
                self.last_err_cam = np.array([ex, ey, ez_ctrl], dtype=np.float32)
            desired_open = self._desired_gripper_open(grasp_w, err_norm)
            self.grip_cmd = float(0.30 * desired_open + 0.70 * float(self.grip_cmd))
            if self.stable_frames >= config.alignment.stable_frames:
                self.log.info('grab_item: alignment stable, proceeding to grasp')
                with self.lock:
                    self._enter_state_locked(GrabberState.GRASPING)
                return
            cmd = {
                'joint_wrist_yaw': clamp(yaw + yaw_step, config.wrist.yaw.min, config.wrist.yaw.max),
                'joint_wrist_pitch': clamp(pitch + pitch_step, config.wrist.pitch.min, config.wrist.pitch.max),
                'joint_wrist_roll': clamp(roll + roll_step, config.wrist.roll.min, config.wrist.roll.max),
                'joint_lift': clamp(lift + lift_step, config.lift.min, config.lift.max),
                'wrist_extension': clamp(arm + arm_step, config.arm.min, config.arm.max),
                config.gripper.joint: self.grip_cmd, }
            self._rate_limit_send(cmd, duration=config.control.trajectory_dt)
            return
        if st == GrabberState.GRASPING:
            elapsed = (now - self.state_enter_time).nanoseconds / 1e9
            if (not self.grasp_sent) and elapsed >= 0.02:
                self.grasp_sent = True
                self._grasp_attempted = True
                close_duration = 1.4
                self.grasp_close_until = elapsed + close_duration
                self.grasp_hold_started_at = 0.0
                with self.lock:
                    ex, ey, ez_ctrl = float(self.last_err_cam[0]), float(self.last_err_cam[1]), float(
                        self.last_err_cam[2])
                ey_ctrl = float(ey - config.grasp.y_target_bias_m)
                y_nudge = float(np.clip(max(0.0, ey_ctrl), 0.0, config.grasp.y_nudge_max_m))
                axis_lift, axis_arm = self._motion_axes(yaw, pitch, roll)
                err_y = np.array([0.0, y_nudge, 0.0], dtype=np.float32)
                y_lift = float(np.dot(axis_lift, err_y))
                y_arm = float(np.dot(axis_arm, err_y))
                y_lift = clamp(0.9 * y_lift, -0.012, 0.012)
                y_arm = clamp(0.35 * y_arm, -0.010, 0.010)
                extra_push = config.grasp.push.extra_m
                max_push = config.grasp.push.max_m
                push_scale = float(shape_profile.get('push_scale', 1.0))
                grasp_pitch_target = float(shape_profile.get('target_pitch', config.wrist.pitch.down))
                grasp_lift_delta = y_lift - float(shape_profile.get('lift_lower_m', 0.0))
                elongation_score = float(shape_profile.get('elongation_score', 0.0))
                if elongation_score > 0.0 and shape_profile.get('shape') != 'ball':
                    extra_push = self._lerp(float(extra_push), min(float(extra_push), 0.015), elongation_score)
                    max_push = self._lerp(float(max_push), min(float(max_push), 0.025), elongation_score)
                    pitch_floor = float(config.wrist.pitch.down)
                    grasp_pitch_target = self._lerp(grasp_pitch_target, max(grasp_pitch_target, pitch_floor), elongation_score)
                    min_lift_delta = self._lerp(-float(config.grasp.topdown_lower_m), -0.006, elongation_score)
                    grasp_lift_delta = max(grasp_lift_delta, min_lift_delta)
                push = float(np.clip((extra_push + max(0.0, ez_ctrl)) * push_scale, config.grasp.push.min_m, max_push))
                self.log.info(f'grab_item: closing grasp with {self._profile_summary(shape_profile)} '
                              f'push={push:.3f}m lift_delta={grasp_lift_delta:.3f}m '
                              f'pitch={grasp_pitch_target:.2f}')
                self._send_trajectory({
                    'joint_wrist_pitch': clamp(grasp_pitch_target, config.wrist.pitch.min, config.wrist.pitch.max),
                    'joint_lift': clamp(lift + grasp_lift_delta, config.lift.min, config.lift.max),
                    'wrist_extension': clamp(arm + push + y_arm, config.arm.min, config.arm.max),
                    config.gripper.joint: config.gripper.closed, }, duration=close_duration, )
                return
            if grip_pos is not None:
                gp = float(grip_pos)
                if self.grip_prev is None:
                    self.grip_prev = gp
                    self.grip_settle = 0
                else:
                    if abs(gp - float(self.grip_prev)) <= float(config.gripper.settle.epsilon):
                        self.grip_settle += 1
                    else:
                        self.grip_settle = 0
                    self.grip_prev = gp
            close_done = self.grasp_close_until > 0.0 and elapsed >= self.grasp_close_until
            if close_done:
                if self.grasp_hold_started_at <= 0.0:
                    self.grasp_hold_started_at = elapsed
                    self.grip_prev = None
                    self.grip_settle = 0
                hold_pitch = self._carry_pitch(shape_profile)
                self._rate_limit_send({
                    'joint_wrist_pitch': clamp(hold_pitch, config.wrist.pitch.min, config.wrist.pitch.max),
                    config.gripper.joint: config.gripper.closed, }, duration=config.control.trajectory_dt, force=True, )
            hold_elapsed = max(0.0, elapsed - float(self.grasp_hold_started_at))
            if close_done and ((
                                       hold_elapsed >= config.grasp.min_hold_seconds and self.grip_settle >= config.gripper.settle.count) or (
                                       hold_elapsed >= config.grasp.max_hold_seconds)):
                with self.lock:
                    self._enter_state_locked(GrabberState.RETRACTING)
            return
        if st == GrabberState.RETRACTING:
            new_arm = max(config.arm.min, arm - config.arm.retract_step)
            new_lift = min(config.lift.max, lift + config.lift.retract_step)
            retract_pitch = self._carry_pitch(shape_profile)
            self._rate_limit_send({
                'wrist_extension': new_arm,
                'joint_lift': new_lift,
                'joint_wrist_pitch': clamp(retract_pitch, config.wrist.pitch.min, config.wrist.pitch.max),
                config.gripper.joint: config.gripper.closed, }, duration=config.control.trajectory_dt, force=True, )
            if new_arm <= config.arm.min + 0.02:
                with self.lock:
                    self.log.info('Grab successful - keeping object, going to FINISHED')
                    self.grab_result = 'success'
                    self.state = GrabberState.FINISHED
            return

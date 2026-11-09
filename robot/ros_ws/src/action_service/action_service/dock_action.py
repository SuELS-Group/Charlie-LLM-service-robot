import json
import math
import threading
import time
from enum import Enum, auto
from pathlib import Path

import numpy as np
import rclpy
from geometry_msgs.msg import PointStamped
from rclpy.duration import Duration
from robot_common import get_config
from sensor_msgs.msg import BatteryState
from std_msgs.msg import String
from std_srvs.srv import Trigger
from tf2_geometry_msgs import do_transform_point

from action_service.common.motion import MotionHelper

config = get_config('action_service', Path(__file__).stem)


class DockState(Enum):
    IDLE = auto()
    STOWING = auto()
    SEARCHING = auto()
    APPROACHING = auto()
    ALIGNING = auto()
    BACKING_IN = auto()
    VERIFYING = auto()
    FINISHED = auto()


class DockCableConnected(RuntimeError):
    pass


class DockAction:

    def __init__(self, node):
        self.node = node
        self.log = node.log.sub('dock')
        self.lock = threading.Lock()
        self._op_thread = None
        self._stop_evt = threading.Event()
        self._done_evt = threading.Event()
        self.motion = MotionHelper(node, config=config, stop_evt=self._stop_evt, log_prefix='dock')
        self.state = DockState.IDLE
        self.state_enter_time = None
        self.dock_marker_pos = None
        self.dock_marker_pos_odom = None
        self.dock_marker_orientation = None
        self.last_marker_time = None
        self.is_charging = False
        self.battery_full = False
        self.last_battery_monotonic = None
        self.charge_outcome = None
        self._deadline = None
        self._timed_out = False
        self.charger_connected = False
        self.battery_voltage = 0.0
        self.battery_sub = None
        self.dock_active = False
        self.dock_result = None
        self._setup_battery_subscription()

    def _setup_battery_subscription(self):
        try:
            self.battery_sub = self.node.create_subscription(BatteryState, '/battery', self._battery_callback, 10, callback_group=self.node.cb_sub)
            self.log.info('DockAction: Battery subscription created')
        except Exception as e:
            self.log.warning(f'DockAction: Failed to create battery subscription: {e}')

    def _battery_callback(self, msg: BatteryState):
        with self.lock:
            self.battery_voltage = msg.voltage
            self.is_charging = msg.power_supply_status == BatteryState.POWER_SUPPLY_STATUS_CHARGING
            self.battery_full = msg.power_supply_status == BatteryState.POWER_SUPPLY_STATUS_FULL
            self.charger_connected = msg.present
            self.last_battery_monotonic = time.monotonic()

    def _charging_evidence(self):
        with self.lock:
            if (self.last_battery_monotonic is None or
                    time.monotonic() - self.last_battery_monotonic > float(config.charging.max_sample_age_seconds)):
                return 'unavailable'
            if not self.charger_connected:
                return 'disconnected'
            if self.is_charging:
                return 'charging'
            return 'full' if self.battery_full else 'connected'

    def execute(self, request, response):
        if self._charging_evidence() == 'unavailable':
            return self.node.set_failure(response, 'Docking unavailable: fresh battery state is missing')
        with self.lock:
            if self.dock_active:
                return self.node.set_failure(response, 'Docking already in progress')
            self._reset_state_locked()
            self.dock_active = True
            self.dock_result = None
            self._stop_evt.clear()
            self._done_evt.clear()
        self.log.info('dock: Starting autonomous docking sequence')
        self._op_thread = threading.Thread(target=self._run_dock_sequence, args=(float(config.timeouts.dock_seconds),), daemon=False, )
        self._op_thread.start()
        finished = self._done_evt.wait(timeout=float(config.timeouts.dock_seconds) + 5.0)
        if not finished:
            self._timed_out = True
            self._stop_evt.set()
            if self._op_thread is not None:
                self._op_thread.join(timeout=3.0)
            with self.lock:
                self.dock_active = False
                self.dock_result = self.dock_result or 'timeout'
                self.state = DockState.FINISHED
        with self.lock:
            result = self.dock_result
        if result == 'success':
            outcome = self._charging_evidence()
            if outcome not in {'charging', 'full'}:
                return self.node.set_failure(response, 'Docking finished but charging state could not be confirmed')
            return self.node.set_success(response,
                'Docked and charging' if outcome == 'charging' else 'Docked; battery already full',
                json.dumps({'charger_connected': True,
                    'charging': outcome == 'charging', 'battery_full': outcome == 'full'}))
        elif result == 'timeout':
            return self.node.set_timeout(response, f'Docking timed out after {config.timeouts.dock_seconds}s')
        elif result == 'marker_not_found':
            return self.node.set_failure(response, 'Could not find docking station marker')
        elif result == 'stopped':
            return self.node.set_stopped(response, 'Docking stopped')
        return self.node.set_failure(response, f"Docking failed: {result or 'unknown error'}")

    def stop(self):
        with self.lock:
            if not self.dock_active:
                return
        self.log.warning('DockAction: External stop requested')
        self._stop_evt.set()
        self._stop_base()
        if self._op_thread is not None:
            self._op_thread.join(timeout=3.0)
        with self.lock:
            self.dock_active = False
            self.dock_result = 'stopped'
            self.state = DockState.FINISHED
        self._done_evt.set()

    def handle_detection(self, msg: String):
        with self.lock:
            if not self.dock_active:
                return
            if self.state not in (DockState.SEARCHING, DockState.APPROACHING, DockState.ALIGNING, DockState.BACKING_IN):
                return
        try:
            data = json.loads(msg.data)
        except (json.JSONDecodeError, AttributeError):
            return
        frame_id = data.get('header', {}).get('frame_id', 'base_link')
        detections = data.get('detections', [])
        for det in detections:
            label = det.get('label', '').lower().strip()
            if label == config.marker.id or label == 'aruco_245':
                pos = det.get('position_3d', {})
                pos_arr = np.array([pos.get('x', 0.0), pos.get('y', 0.0), pos.get('z', 0.0)])
                marker_pos = self._transform_to_base(pos_arr, frame_id, rclpy.time.Time().to_msg())
                if marker_pos is not None:
                    marker_pos_odom = self._transform_point_to_frame(pos_arr, frame_id, 'odom', rclpy.time.Time().to_msg())
                    with self.lock:
                        self.dock_marker_pos = marker_pos
                        if marker_pos_odom is not None:
                            self.dock_marker_pos_odom = marker_pos_odom
                        self.last_marker_time = time.time()
                        self.dock_marker_orientation = math.atan2(-marker_pos[1], -marker_pos[0])
                return

    def _transform_to_base(self, point_xyz, from_frame, stamp):
        return self._transform_point_to_frame(point_xyz, from_frame, 'base_link', stamp)

    def _transform_point_to_frame(self, point_xyz, from_frame, to_frame, stamp):
        try:
            tfm = self.node.tf_buffer.lookup_transform(to_frame, from_frame, rclpy.time.Time.from_msg(stamp), timeout=Duration(seconds=0.1))
            ps = PointStamped()
            ps.header.frame_id = from_frame
            ps.header.stamp = stamp
            ps.point.x = float(point_xyz[0])
            ps.point.y = float(point_xyz[1])
            ps.point.z = float(point_xyz[2])
            out = do_transform_point(ps, tfm)
            return np.array([out.point.x, out.point.y, out.point.z], dtype=np.float32)
        except Exception:
            return None

    def _get_marker_pos_in_base(self):
        with self.lock:
            if self.dock_marker_pos is not None and self.last_marker_time is not None:
                if (time.time() - self.last_marker_time) < config.marker.lost_timeout_seconds:
                    return self.dock_marker_pos.copy(), True
            if self.dock_marker_pos_odom is not None:
                try:
                    tfm = self.node.tf_buffer.lookup_transform('base_link', 'odom', rclpy.time.Time(), timeout=Duration(seconds=0.1))
                    ps = PointStamped()
                    ps.header.frame_id = 'odom'
                    ps.point.x = float(self.dock_marker_pos_odom[0])
                    ps.point.y = float(self.dock_marker_pos_odom[1])
                    ps.point.z = float(self.dock_marker_pos_odom[2])
                    out = do_transform_point(ps, tfm)
                    return np.array([out.point.x, out.point.y, out.point.z], dtype=np.float32), False
                except Exception:
                    pass
        return None, False

    def _run_dock_sequence(self, timeout_s: float):
        self._deadline = time.monotonic() + timeout_s
        result = 'failed'
        try:
            if self._charging_evidence() in {'connected','charging','full'}:
                self._stop_base()
                result='success' if self._verify_charging() else 'charging_not_detected'
                return
            self.log.info('dock: Phase 1 - Stowing robot')
            with self.lock:
                self._enter_state_locked(DockState.STOWING)
            if not self._stow_robot():
                result = 'stow_failed'
                return
            if self._stop_evt.is_set():
                result = 'stopped'
                return
            self._set_head_for_search()
            self.log.info('dock: Phase 2 - Searching for docking station')
            with self.lock:
                self._enter_state_locked(DockState.SEARCHING)
            if not self._search_for_dock():
                if self._stop_evt.is_set():
                    result = 'stopped'
                    return
                result = 'marker_not_found'
                return
            if self._stop_evt.is_set():
                result = 'stopped'
                return
            self.log.info('dock: Phase 3 - Approaching docking station')
            with self.lock:
                self._enter_state_locked(DockState.APPROACHING)
            if not self._approach_dock():
                result = 'approach_failed'
                return
            if self._stop_evt.is_set():
                result = 'stopped'
                return
            self.log.info('dock: Phase 4 - Aligning with docking station')
            with self.lock:
                self._enter_state_locked(DockState.ALIGNING)
            if not self._align_with_dock():
                result = 'alignment_failed'
                return
            if self._stop_evt.is_set():
                result = 'stopped'
                return
            self.log.info('dock: Phase 5 - Backing into docking station')
            with self.lock:
                self._enter_state_locked(DockState.BACKING_IN)
            if not self._back_into_dock():
                result = 'docking_failed'
                return
            if self._stop_evt.is_set():
                result = 'stopped'
                return
            self.log.info('dock: Phase 6 - Verifying charging connection')
            with self.lock:
                self._enter_state_locked(DockState.VERIFYING)
            if self._verify_charging():
                result = 'success'
                self.log.info(f'dock: Verified {self.charge_outcome}')
            else:
                result = 'charging_not_detected'
                self.log.warning('dock: Docked but charging not detected')
        except DockCableConnected:
            self._stop_base()
            result='success' if self._verify_charging() else 'charging_not_detected'
        except Exception as e:
            self.log.exception(f'dock: Exception during docking: {e}')
            result = 'failed'
        finally:
            if self._stop_evt.is_set():
                result = 'timeout' if self._timed_out else 'stopped'
            self._stop_base(num_commands=15, delay=0.01)
            time.sleep(0.15)
            if result not in {'stopped', 'timeout', 'stow_failed'}:
                try:
                    head_restored = self.motion.move_head(0.0, 0.0, duration=1.0, wait=True)
                except Exception:
                    head_restored = False
                if not head_restored and result == 'success':
                    result = 'cleanup_failed'
            try:
                mode_restored = self._switch_to_navigation_mode()
            except Exception:
                mode_restored = False
            if not mode_restored and result == 'success':
                result = 'cleanup_failed'
            with self.lock:
                if self._stop_evt.is_set():
                    result = 'timeout' if self._timed_out else 'stopped'
                self.dock_result = result
                self.dock_active = False
                self.state = DockState.FINISHED
            self._done_evt.set()
            self.log.info(f'dock: Sequence complete, result={result}')

    def _reset_state_locked(self):
        self.state = DockState.IDLE
        self.state_enter_time = None
        self.dock_marker_pos = None
        self.dock_marker_pos_odom = None
        self.dock_marker_orientation = None
        self.last_marker_time = None
        self.charge_outcome = None
        self._deadline = None
        self._timed_out = False

    def _enter_state_locked(self, st):
        self.state = st
        self.state_enter_time = time.time()
        self.log.info(f'DockAction -> {st.name}')

    def _sleep_with_stop(self, seconds: float):
        end = time.monotonic() + float(seconds)
        while time.monotonic() < end:
            if self._deadline is not None and time.monotonic() >= self._deadline:
                self._timed_out = True
                self._stop_evt.set()
            if self._stop_evt.is_set():
                return
            time.sleep(0.02)

    def _stow_robot(self) -> bool:
        stow_client = getattr(self.node, 'stow_client', None)
        if stow_client is None:
            self.log.warning('dock: stow_client not available')
            return False
        t0 = time.monotonic()
        while not self._stop_evt.is_set() and (time.monotonic() - t0) < 3.0:
            try:
                if stow_client.wait_for_service(timeout_sec=0.2):
                    break
            except Exception:
                pass
            time.sleep(0.05)
        if not stow_client.service_is_ready():
            return False
        try:
            fut = stow_client.call_async(Trigger.Request())
        except Exception:
            return False
        t1 = time.monotonic()
        while not self._stop_evt.is_set() and (time.monotonic() - t1) < 10.0:
            if fut.done():
                try:
                    res = fut.result()
                    return bool(res and res.success)
                except Exception:
                    return False
            time.sleep(0.05)
        return False

    def _set_head_for_search(self):
        self.motion.move_head(0.0, config.search.head.tilt, duration=1.0, wait=False)
        self._sleep_with_stop(0.5)

    def _search_for_dock(self) -> bool:
        t0 = time.monotonic()
        angles = np.linspace(config.search.head.pan_start, config.search.head.pan_end, config.search.head.steps)
        for angle in angles:
            if self._stop_evt.is_set():
                return False
            if (time.monotonic() - t0) > config.timeouts.search_seconds:
                self.log.warning('dock: Search timeout')
                return False
            self.motion.move_head(float(angle), config.search.head.tilt, duration=0.5, wait=False)
            self._sleep_with_stop(config.search.dwell_seconds)
            with self.lock:
                if self.dock_marker_pos is not None:
                    self.log.info(f'dock: Found dock marker at ({self.dock_marker_pos[0]:.2f}, '
                                  f'{self.dock_marker_pos[1]:.2f}, {self.dock_marker_pos[2]:.2f})')
                    return True
        for angle in reversed(angles):
            if self._stop_evt.is_set():
                return False
            if (time.monotonic() - t0) > config.timeouts.search_seconds:
                return False
            self.motion.move_head(float(angle), config.search.head.tilt, duration=0.4, wait=False)
            self._sleep_with_stop(config.search.dwell_seconds * 0.7)
            with self.lock:
                if self.dock_marker_pos is not None:
                    return True
        return False

    def _approach_dock(self) -> bool:
        t0 = time.monotonic()
        FACING_THRESHOLD = 0.05
        DRIFT_THRESHOLD = 0.15
        MAX_REALIGNS = 5
        realign_count = 0
        phase = 'rotate'
        self.log.info('dock: Approach - rotating to face marker')
        while not self._stop_evt.is_set():
            if (time.monotonic() - t0) > config.timeouts.approach_seconds:
                self.log.warning(f'dock: Approach timeout during {phase}')
                self._stop_base()
                return False
            marker_pos, is_live = self._get_marker_pos_in_base()
            if marker_pos is None:
                self._stop_base()
                self._sleep_with_stop(0.1)
                continue
            angle_to_marker = math.atan2(marker_pos[1], marker_pos[0])
            marker_dist = math.sqrt(marker_pos[0] ** 2 + marker_pos[1] ** 2)
            target_dist = marker_dist - config.approach.pre_dock_distance_m
            if phase == 'rotate':
                if abs(angle_to_marker) < FACING_THRESHOLD:
                    self.log.info(f'dock: Now facing marker (angle={math.degrees(angle_to_marker):.1f}°), driving')
                    self._stop_base()
                    self._sleep_with_stop(0.3)
                    phase = 'drive'
                    continue
                raw_angular_vel = config.approach.angular_gain * angle_to_marker
                if abs(raw_angular_vel) < config.limits.min_angular_velocity:
                    raw_angular_vel = config.limits.min_angular_velocity if angle_to_marker > 0 else -config.limits.min_angular_velocity
                angular_vel = np.clip(raw_angular_vel, -config.limits.max_angular_velocity, config.limits.max_angular_velocity)
                self._send_velocity(0.0, angular_vel)
                self._track_marker_with_head()
            elif phase == 'drive':
                if abs(target_dist) < config.approach.distance_threshold_m:
                    self.log.info('dock: Reached pre-dock position')
                    self._stop_base()
                    return True
                if abs(angle_to_marker) > DRIFT_THRESHOLD:
                    realign_count += 1
                    if realign_count > MAX_REALIGNS:
                        self.log.warning('dock: Too many re-alignments during approach')
                        self._stop_base()
                        return False
                    self.log.info(f'dock: Drifted off course ({math.degrees(angle_to_marker):.1f}°), '
                                  f're-aligning ({realign_count}/{MAX_REALIGNS})')
                    self._stop_base()
                    self._sleep_with_stop(0.2)
                    phase = 'rotate'
                    continue
                linear_vel = np.clip(config.approach.linear_gain * target_dist, -config.limits.max_linear_velocity, config.limits.max_linear_velocity)
                raw_correction = 0.7 * config.approach.angular_gain * angle_to_marker
                if abs(raw_correction) < config.limits.min_angular_velocity * 0.5 and abs(angle_to_marker) > 0.02:
                    raw_correction = (config.limits.min_angular_velocity * 0.5) * (1.0 if angle_to_marker > 0 else -1.0)
                angular_vel = np.clip(raw_correction, -0.3, 0.3)
                self._send_velocity(linear_vel, angular_vel)
                self._track_marker_with_head()
            self._sleep_with_stop(1.0 / config.control.rate_hz)
        self._stop_base()
        return False

    def _align_with_dock(self) -> bool:
        t0 = time.monotonic()
        initial_direction = None
        alignment_attempts = 0
        max_alignment_attempts = 3
        while not self._stop_evt.is_set():
            if (time.monotonic() - t0) > config.timeouts.align_seconds:
                self.log.warning('dock: Alignment timeout')
                self._stop_base()
                return False
            marker_pos, is_live = self._get_marker_pos_in_base()
            if marker_pos is None:
                self._stop_base()
                self._sleep_with_stop(0.1)
                continue
            angle_to_marker = math.atan2(marker_pos[1], marker_pos[0])
            behind_error = math.pi - abs(angle_to_marker)
            effective_threshold = config.align.angle_threshold_rad - config.align.overshoot_rad
            if effective_threshold < 0.02:
                effective_threshold = 0.02
            if behind_error < effective_threshold:
                self.log.info(f'dock: Alignment reached target (error={math.degrees(behind_error):.1f}°), settling...')
                self._stop_base()
                self._sleep_with_stop(1.0)
                marker_pos_verify, _ = self._get_marker_pos_in_base()
                if marker_pos_verify is not None:
                    angle_verify = math.atan2(marker_pos_verify[1], marker_pos_verify[0])
                    behind_error_verify = math.pi - abs(angle_verify)
                    if behind_error_verify > config.align.angle_threshold_rad:
                        alignment_attempts += 1
                        if alignment_attempts >= max_alignment_attempts:
                            self.log.warning(f'dock: Max alignment attempts reached, accepting '
                                             f'error of {math.degrees(behind_error_verify):.1f}°')
                            return True
                        self.log.info(f'dock: Post-settle drift detected ({math.degrees(behind_error_verify):.1f}°), '
                                      f'attempt {alignment_attempts}/{max_alignment_attempts}')
                        initial_direction = None
                        continue
                self.log.info(f'dock: Alignment verified (final error={math.degrees(behind_error_verify):.1f}°)')
                return True
            if initial_direction is None:
                initial_direction = -1.0
                self.log.info(f'dock: Aligning, marker at {math.degrees(angle_to_marker):.1f}°, '
                              f'error={math.degrees(behind_error):.1f}°, '
                              f'turning right (CW)')
            raw_angular_vel = initial_direction * config.align.angular_gain * behind_error
            if abs(raw_angular_vel) < config.limits.min_angular_velocity and behind_error > effective_threshold:
                raw_angular_vel = initial_direction * config.limits.min_angular_velocity
            angular_vel = np.clip(raw_angular_vel, -config.limits.max_angular_velocity, config.limits.max_angular_velocity)
            self._send_velocity(0.0, angular_vel)
            head_pan = np.clip(angle_to_marker, -1.5, 1.5)
            self.motion.move_head(head_pan, config.search.head.tilt, duration=0.1, wait=False)
            self._sleep_with_stop(1.0 / config.control.rate_hz)
        self._stop_base()
        return False

    def _back_into_dock(self) -> bool:
        t0 = time.monotonic()
        blind_backing_start = None
        while not self._stop_evt.is_set():
            elapsed = time.monotonic() - t0
            if elapsed > config.timeouts.backing_seconds:
                self.log.warning('dock: Backing timeout')
                self._stop_base()
                return False
            evidence = self._charging_evidence()
            if evidence == 'unavailable':
                self._stop_base()
                return False
            if evidence in {'connected', 'charging', 'full'}:
                self._stop_base(num_commands=10, delay=0.01)
                time.sleep(0.1)
                return True
            marker_pos, is_live = self._get_marker_pos_in_base()
            if marker_pos is not None and is_live:
                blind_backing_start = None
                last_known_lateral_error = None
                lateral_offset = marker_pos[1]
                angle_to_marker = math.atan2(marker_pos[1], marker_pos[0])
                lateral_correction = -config.backing.lateral_gain * lateral_offset
                angular_vel = np.clip(lateral_correction + config.backing.lateral_drift_bias, -0.15, 0.15)
                last_known_lateral_error = lateral_offset
                if abs(lateral_offset) > 0.02:
                    self.log.debug(f'dock: Backing lateral offset={lateral_offset:.3f}m, correction={angular_vel:.3f}rad/s')
                linear_vel = -config.backing.linear_velocity
                self._send_velocity(linear_vel, angular_vel)
            else:
                if blind_backing_start is None:
                    blind_backing_start = time.monotonic()
                    self.log.info('dock: Marker occluded, backing blind with drift compensation')
                blind_backing_time = time.monotonic() - blind_backing_start
                if blind_backing_time > config.backing.max_blind_time_seconds:
                    self.log.info(f'dock: Reached max blind backing time ({config.backing.max_blind_time_seconds}s), stopping')
                    self._stop_base()
                    return True
                self._send_velocity(-config.backing.linear_velocity, config.backing.lateral_drift_bias)
            self._sleep_with_stop(1.0 / config.control.rate_hz)
        self._stop_base()
        return False

    def _verify_charging(self) -> bool:
        t0 = time.monotonic()
        self._stop_base(num_commands=10, delay=0.01)
        self._sleep_with_stop(2.0)
        while not self._stop_evt.is_set():
            if (time.monotonic() - t0) > config.timeouts.verify_seconds:
                with self.lock:
                    voltage = self.battery_voltage
                self.log.warning(f'dock: Charging verification timeout (voltage: {voltage:.2f}V; '
                                 'fresh charging/full-battery status required)')
                return False
            evidence = self._charging_evidence()
            if evidence in {'charging', 'full'}:
                self.charge_outcome = evidence
                return True
            self._sleep_with_stop(0.5)
        return False

    def _track_marker_with_head(self):
        with self.lock:
            if self.dock_marker_pos is None:
                return
            marker_pos = self.dock_marker_pos.copy()
        angle_to_marker = math.atan2(marker_pos[1], marker_pos[0])
        head_pan = np.clip(angle_to_marker, -1.5, 1.5)
        self.motion.move_head(float(head_pan), config.search.head.tilt, duration=0.1, wait=False)

    def _send_velocity(self, linear: float, angular: float):
        if linear or angular:
            evidence=self._charging_evidence()
            if evidence in {'connected','charging','full'}:
                raise DockCableConnected('Charging contact reached')
            if evidence=='unavailable':
                raise RuntimeError('Fresh battery evidence is required for docking motion')
            runtime=getattr(self.node,'k2_runtime',None)
            if runtime is not None:
                error=runtime.posture_error(self.node.get_clock().now().nanoseconds/1e9)
                if error:raise RuntimeError(error)
        self.motion.publish_base_velocity(linear, angular)

    def _stop_base(self, num_commands: int = 5, delay: float = 0.02):
        self.motion.stop_base(num_commands=num_commands, delay=delay)

    def _switch_to_navigation_mode(self) -> bool:
        return self.motion.switch_to_navigation_mode(timeout_s=2.0)

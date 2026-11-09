import json
import math
import threading
import time
from pathlib import Path

import numpy as np
from action_service.common.tf_utils import transform_point
from robot_common import get_config

from action_service.common.detection import collect_transformed_detection_records, fresh_detection_payload
from action_service.common.motion import MotionHelper

config = get_config('action_service', Path(__file__).stem)


class PlacementTargetError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class PlaceItemAction:

    def __init__(self, node):
        self.node = node
        self.log = node.log.sub('place')
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self.place_active = False
        self._release_attempted = False
        self._gripper_opened = False
        self._arm_motion_failed = False
        self.motion = MotionHelper(node, config=config, stop_evt=self._stop_evt, log_prefix='place')
        self._place_hint_map = None
        self._scan_lock = threading.Lock()
        self._scan_active = False
        self._scan_detections = []
        self._block_detections_until = 0.0
        self._gripper_scan_active = False
        self._gripper_detections = []
        self._scan_surface = ''

    def localize_trash(self, request, response):
        """Observe the configured bin before the tool's guarded approach."""
        with self._lock:
            if self.place_active:
                return self.node.set_failure(response, 'Place already in progress')
            self.place_active = True
            self._stop_evt.clear()
            self._place_hint_map = None
        try:
            height = self._look_at_surface_and_refresh_hint('trash_can')
            if self._stop_evt.is_set():
                return self._stopped_response(response)
            hint = self._hint_in_base_frame()
            if hint is None or height is None:
                raise PlacementTargetError('TRASH_TARGET_NOT_FOUND',
                    'Could not localize the nearby trash can; gripper kept closed')
            distance = math.hypot(float(hint[0]), float(hint[1]))
            lift = height + float(config.place.release_clearance_m)
            if (distance > float(config.trash.max_local_distance_m) or
                    not float(config.lift.min) <= lift <= float(config.lift.max)):
                raise PlacementTargetError('TRASH_TARGET_UNREACHABLE',
                    'Trash marker is outside the local approach or lift range; gripper kept closed')
            positioned = True
            try:
                self._validate_trash_target(hint, height)
            except PlacementTargetError:
                positioned = False
            return self.node.set_success(response, 'Localized the configured trash marker', json.dumps({
                'surface': 'trash_can', 'source': 'aruco', 'marker_id': int(config.trash.marker_id),
                'x': float(self._place_hint_map[0]), 'y': float(self._place_hint_map[1]),
                'z': height, 'distance': distance, 'positioned': positioned,
                'approach_distance': float(config.trash.approach_distance_m)}))
        except PlacementTargetError as exc:
            return self.node.set_failure(response, str(exc), json.dumps({
                'error_code': exc.code, 'arm_motion_started': False,
                'gripper_opened': False, 'release_attempted': False}))
        finally:
            with self._lock:
                self.place_active = False

    def stop(self):
        with self._lock:
            if not self.place_active:
                return
        self.log.warning('PlaceItemAction: external stop requested')
        self._stop_evt.set()
        with self._lock:
            self.place_active = False

    def execute(self, request, response, parameters=None):
        self.log.info('============== PLACE_ITEM EXECUTE CALLED ==============')
        self.log.info(f'place_item: starting with parameters={parameters}')
        with self._lock:
            if self.place_active:
                return self.node.set_failure(response, 'Place already in progress')
            self.place_active = True
            self._stop_evt.clear()
            self._release_attempted = False
            self._gripper_opened = False
            self._arm_motion_failed = False
        params = self._parse_params(parameters)
        surface = params.get('surface', 'surface')
        target_z = params.get('z')
        if target_z is not None:
            lift_height = self._z_to_lift(float(target_z))
        else:
            lift_height = self._z_to_lift(config.place.default_height_m)
        self.log.info(f'place_item: surface={surface!r}, target_z={target_z}, lift_height={lift_height:.3f}')
        if not self.motion.wait_for_traj_server(timeout_s=4.0):
            with self._lock:
                self.place_active = False
            if self._stop_evt.is_set():
                return self.node.set_stopped(response, 'Place operation was stopped')
            return self.node.set_failure(response, 'Place failed: trajectory controller not ready')
        switched = self.motion.switch_to_position_mode()
        if not switched:
            with self._lock:
                self.place_active = False
            return self.node.set_failure(response, 'Place failed: position mode not confirmed')
        ox = params.get('original_x')
        oy = params.get('original_y')
        if ox is not None and oy is not None and target_z is not None:
            self._place_hint_map = np.array([float(ox), float(oy), float(target_z)], dtype=np.float32)
        else:
            self._place_hint_map = None
        success = False
        stopped = False
        arm_started = False
        target_error = None
        localized_z = None
        try:
            if self._stop_evt.is_set():
                stopped = True
                return self._stopped_response(response)
            alignment_passes = int(config.alignment.passes)
            for attempt in range(alignment_passes):
                self.log.info(f'============== ALIGNMENT PASS {attempt + 1}/{alignment_passes} for {surface} ==============')
                fresh_z = self._look_at_surface_and_refresh_hint(surface)
                self.log.info(f'============== ALIGNMENT PASS {attempt + 1} fresh_z: {fresh_z} ==============')
                if self._stop_evt.is_set():
                    stopped = True
                    return self._stopped_response(response)
                if fresh_z is not None:
                    localized_z = fresh_z
                    lift_height = self._z_to_lift(fresh_z)
                    self.log.info(f'place_item: updated lift_height={lift_height:.3f} from close-range scan')
                hint_base = self._hint_in_base_frame()
                self.log.info(f'============== HINT IN BASE FRAME: {hint_base} ==============')
                if hint_base is None:
                    self.log.info('place_item: no fresh target hint available for alignment pass')
                    break
                bearing_err = self._bearing_error_rad(hint_base)
                self.log.info(f'place_item: alignment pass {attempt + 1} bearing error '
                              f'{math.degrees(bearing_err):.2f}°')
                if abs(bearing_err) <= math.radians(float(config.alignment.bearing_tolerance_deg)):
                    self.log.info('place_item: target is within precise alignment tolerance')
                    break
                rotated = self._align_base_to_target(hint_base)
                if self._stop_evt.is_set():
                    stopped = True
                    return self._stopped_response(response)
                if not rotated:
                    self.log.info('place_item: no further base correction applied')
                    break
                self.motion.sleep(0.35)
            arm_extension = float(config.place.arm_extension_m)
            hint_post = self._hint_in_base_frame()
            if surface == 'trash_can':
                # A waypoint gets us near the bin; this action owns its local
                # localization. Neither a default tabletop pose nor external
                # coordinates establish a current, reachable disposal target.
                self._validate_trash_target(hint_post, localized_z)
            if hint_post is not None:
                planar_dist = math.hypot(float(hint_post[0]), float(hint_post[1]))
                computed_extension = planar_dist - float(config.place.arm_body_offset_m)
                arm_extension = max(float(config.arm.min), min(computed_extension, float(config.arm.max)))
                self.log.info(f'place_item: refreshed target gives planar_dist={planar_dist:.3f}m '
                              f'-> arm_extension={arm_extension:.3f}m')
            else:
                self.log.info(f'place_item: no refreshed base-frame hint, using default arm_extension={arm_extension:.3f}m')
            self.log.info('============== LIFTING ARM ==============')
            arm_started = True
            self._lift_arm()
            self.log.info('============== ARM LIFTED ==============')
            if self._stop_evt.is_set():
                stopped = True
                return self._stopped_response(response)
            self.log.info('============== ORIENTING WRIST ==============')
            self._orient_wrist_for_place()
            self.log.info('============== WRIST ORIENTED ==============')
            if self._stop_evt.is_set():
                stopped = True
                return self._stopped_response(response)
            self.log.info('============== LOWERING TO TARGET ==============')
            self._lower_to_target(lift_height, arm_extension)
            self.log.info('============== LOWERED TO TARGET ==============')
            if self._stop_evt.is_set():
                stopped = True
                return self._stopped_response(response)
            self.log.info('============== FINAL GRIPPER-CAMERA ALIGNMENT ==============')
            arm_extension = self._final_gripper_alignment(surface, lift_height, arm_extension)
            self.log.info(f'============== FINAL ARM EXTENSION={arm_extension:.3f} ==============')
            if self._stop_evt.is_set():
                stopped = True
                return self._stopped_response(response)
            self.log.info('============== OPENING GRIPPER ==============')
            self._release_attempted = True
            success = self.motion.open_gripper(duration_s=1.2, wait=True)
            self._gripper_opened = bool(success)
            if success:
                self.log.info('============== GRIPPER OPENED SUCCESSFULLY ==============')
            else:
                self.log.warning('============== GRIPPER OPEN COMMAND FAILED ==============')
            if success:
                self.motion.sleep(0.5)
                if self._stop_evt.is_set():
                    stopped = True
                    return self._stopped_response(response)
                self.log.info('============== RETRACTING ARM ==============')
                self._retract_arm(lift_height)
                self.log.info('============== ARM RETRACTED ==============')
            if self._stop_evt.is_set():
                stopped = True
                return self._stopped_response(response)
        except PlacementTargetError as e:
            target_error = e
            self.log.warning(str(e))
        except Exception as e:
            self.log.error(f'============== EXCEPTION DURING PLACEMENT: {e} ==============')
            success = False
        finally:
            if self._stop_evt.is_set():
                stopped = True
            uncertain_release = self._release_attempted and not self._gripper_opened
            if arm_started and not stopped and not uncertain_release and not self._arm_motion_failed:
                try:
                    self.log.info('============== STOWING ARM ==============')
                    stowed = (self.motion.stow_empty_gripper() if self._gripper_opened else
                              self.motion.safe_stow_arm(preserve_gripper=True))
                    if not stowed:
                        success = False
                    self.log.info('============== ARM STOWED ==============')
                except Exception as e:
                    success = False
                    self.log.warning(f'============== STOW ERROR: {e} ==============')
            self.log.info('============== SWITCHING TO NAVIGATION MODE ==============')
            try:
                if not self.motion.switch_to_navigation_mode():
                    success = False
            finally:
                with self._lock:
                    self.place_active = False
        self.log.info(f'============== PLACE FINISHED, SUCCESS={success}, STOPPED={stopped} ==============')
        if stopped or self._stop_evt.is_set():
            return self._stopped_response(response)
        if target_error is not None:
            return self.node.set_failure(response, str(target_error), json.dumps({
                'error_code': target_error.code, 'phase': 'trash_localization',
                'arm_motion_started': False, 'gripper_opened': False, 'release_attempted': False}))
        if success:
            return self.node.set_success(response, f'Placed item on {surface}')
        if self._arm_motion_failed:
            controller_error = self.motion.last_trajectory_error
            controller_error = controller_error if isinstance(controller_error, dict) else {}
            reason = controller_error.get('message', 'Arm trajectory did not complete')
            message = ('Object released, but arm cleanup failed: ' if self._gripper_opened else
                       'Place failed before opening the gripper: ') + reason
            return self.node.set_failure(response, message, json.dumps({
                'phase': 'arm_motion', 'controller_error': controller_error,
                'error_code': controller_error.get('code', 'PLACE_FAILED')}))
        return self.node.set_failure(response, 'Object released, but resting posture was not confirmed'
            if self._gripper_opened else 'Place failed: unable to release object')

    def _stopped_response(self, response):
        return self.node.set_stopped(response, 'Place operation was stopped')

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
    def _z_to_lift(z: float) -> float:
        lift = float(z) + config.place.release_clearance_m
        return max(config.lift.min, min(lift, config.lift.max))

    def _validate_trash_target(self, hint, localized_z):
        if (hint is None or localized_z is None or not math.isfinite(float(localized_z))
                or len(hint) != 3 or not all(math.isfinite(float(v)) for v in hint)):
            raise PlacementTargetError('TRASH_TARGET_NOT_FOUND',
                'Could not localize the nearby trash can; gripper kept closed')
        extension = math.hypot(float(hint[0]), float(hint[1])) - float(config.place.arm_body_offset_m)
        lift = float(localized_z) + float(config.place.release_clearance_m)
        if (not float(config.arm.min) <= extension <= float(config.arm.max)
                or not float(config.lift.min) <= lift <= float(config.lift.max)
                or abs(self._bearing_error_rad(hint)) > math.radians(float(config.alignment.bearing_tolerance_deg))):
            raise PlacementTargetError('TRASH_TARGET_UNREACHABLE',
                'Trash can is outside the reachable aligned placement area; gripper kept closed')

    def _move_arm_for_place(self, positions, duration):
        if not self.motion.send_full_trajectory(positions, duration=duration, wait=True,
                omit_joints=(config.gripper.joint,)):
            self._arm_motion_failed = True
            raise RuntimeError('Placement arm trajectory did not complete')
        self.motion.sleep(0.3)

    def _lift_arm(self):
        self.log.info('place_item: lifting arm to safe height...')
        self._move_arm_for_place({
            'joint_lift': config.lift.max,
            'wrist_extension': config.arm.min, }, duration=2.5)

    def _orient_wrist_for_place(self):
        self.log.info('place_item: orienting wrist for placement...')
        self._move_arm_for_place({
            'joint_lift': config.lift.max,
            'wrist_extension': config.arm.min,
            'joint_wrist_yaw': 0.0,
            'joint_wrist_pitch': config.wrist.pitch.down,
            'joint_wrist_roll': 0.0, }, duration=2.0)

    def _lower_to_target(self, lift_height: float, arm_extension: float = None):
        if arm_extension is None:
            arm_extension = float(config.place.arm_extension_m)
        self.log.info(f'place_item: lowering to lift={lift_height:.3f} and extending arm to {arm_extension:.3f}m...')
        self._move_arm_for_place({
            'joint_lift': lift_height,
            'wrist_extension': float(arm_extension),
            'joint_wrist_yaw': 0.0,
            'joint_wrist_pitch': config.wrist.pitch.down,
            'joint_wrist_roll': 0.0, }, duration=3.0)

    def _retract_arm(self, current_lift: float):
        self.log.info('place_item: retracting arm...')
        self._move_arm_for_place({
            'joint_lift': current_lift,
            'wrist_extension': config.arm.min, }, duration=2.0)

    def _hint_in_base_frame(self):
        if self._place_hint_map is None:
            return None
        return transform_point(self.node, self._place_hint_map, config.frames.map, config.frames.base, timeout_s=1.0)

    def _bearing_error_rad(self, target_base):
        bearing = math.atan2(float(target_base[1]), float(target_base[0]))
        bearing_err = bearing - math.radians(float(config.alignment.expected_bearing_deg))
        while bearing_err > math.pi:
            bearing_err -= 2 * math.pi
        while bearing_err < -math.pi:
            bearing_err += 2 * math.pi
        return bearing_err

    def _align_base_to_target(self, target_base, tolerance_rad=None, max_correction_rad=None, angular_vel=None):
        if tolerance_rad is None:
            tolerance_rad = math.radians(float(config.alignment.bearing_tolerance_deg))
        if max_correction_rad is None:
            max_correction_rad = math.radians(float(config.alignment.max_correction_deg))
        if angular_vel is None:
            angular_vel = float(config.alignment.angular_vel)
        bearing = math.atan2(float(target_base[1]), float(target_base[0]))
        bearing_err = self._bearing_error_rad(target_base)
        if abs(bearing_err) < tolerance_rad:
            self.log.info(f'place_item: bearing error {math.degrees(bearing_err):.1f}° '
                          f'within tolerance, skipping base correction')
            return False
        if abs(bearing_err) > max_correction_rad:
            bearing_err = math.copysign(max_correction_rad, bearing_err)
        self.log.info(f'place_item: surface at bearing {math.degrees(bearing):.1f}° '
                      f'(expected {float(config.alignment.expected_bearing_deg):.1f}°, '
                      f'error {math.degrees(bearing_err):.1f}°) — rotating base')
        if not self.motion.switch_to_navigation_mode():
            self.log.warning('place_item: could not switch to nav mode for base alignment')
            return False
        self.motion.sleep(0.3)
        self.motion.rotate_base(bearing_err, angular_vel)
        self.motion.sleep(0.3)
        self.motion.switch_to_position_mode()
        self.motion.sleep(0.3)
        return True

    def handle_detection(self, msg):
        with self._scan_lock:
            if not self._scan_active:
                return
            if time.time() < self._block_detections_until:
                return
        self._collect_surface_detections(msg, config.frames.map, self._scan_detections)

    def handle_gripper_detection(self, msg):
        with self._scan_lock:
            if not self._gripper_scan_active:
                return
        self._collect_surface_detections(msg, config.frames.base, self._gripper_detections)

    def _collect_surface_detections(self, msg, target_frame: str, out_list):
        if self._scan_surface == 'trash_can' and fresh_detection_payload(
                self.node, msg, max_age=float(config.detection.max_age_seconds)) is None:
            return
        t_now = time.time()
        records = collect_transformed_detection_records(self.node, msg, target_frame, default_frame=config.frames.base, timeout_s=config.scan.tf_timeout_seconds, min_score=config.detection.min_score, now=t_now, )
        if records:
            with self._scan_lock:
                out_list.extend(records)

    def _check_for_surface_detections(self, surface, detections=None):
        with self._scan_lock:
            raw = list(self._scan_detections if detections is None else detections)
        if not raw:
            return None, -1.0
        best = None
        best_score = -1.0
        for det in raw:
            if surface == 'trash_can' and (
                    type(det.get('marker_id')) is not int or
                    det['marker_id'] != int(config.trash.marker_id) or det.get('source') != 'aruco'):
                continue
            class_id = det.get('class_id', '')
            base_name = (
                class_id.rsplit('_', 1)[0] if '_' in class_id and class_id.rsplit('_', 1)[1].isdigit() else class_id)
            s = float(det.get('score', 0.0))
            if base_name == surface and s > best_score:
                best_score = s
                best = det
        return best, best_score

    def _look_at_surface_and_refresh_hint(self, surface: str):
        self.log.info(f'============== INSIDE _look_at_surface_and_refresh_hint for {surface} ==============')
        primary_pan = float(config.scan.head.pan)
        tilt = float(config.scan.head.tilt)
        move_dur = float(config.scan.head.move_duration)
        settle = float(config.scan.head.settle_seconds)
        dwell = float(config.scan.dwell_seconds)
        primary_hold = float(config.scan.head.primary_hold_seconds)
        pan_angles = [primary_pan, primary_pan + 0.35, primary_pan + 0.7]
        self._scan_surface = surface
        if surface == 'trash_can':
            # At a waypoint the bin can still be straight ahead. Observe that
            # view before moving the camera away from an already visible marker.
            with self._scan_lock:
                self._scan_detections.clear()
                self._block_detections_until = 0.0
                self._scan_active = True
            try:
                self.motion.sleep(float(config.scan.head.current_view_seconds))
            finally:
                with self._scan_lock:
                    self._scan_active = False
            if self._stop_evt.is_set():
                return None
            best, _ = self._check_for_surface_detections(surface)
            if best is not None:
                return self._remember_surface(best)
            hint = self._hint_in_base_frame()
            if hint is not None:
                primary_pan = max(-1.57, min(1.2, math.atan2(float(hint[1]), float(hint[0]))))
                pan_angles = [primary_pan, max(-1.57, primary_pan - .35), min(1.2, primary_pan + .35)]
            else:
                pan_angles = [0.0, -.6, .6, -1.57, 1.2]
        self.log.info(f'place_item: focused sweep for {surface!r} — '
                      f'tilt={math.degrees(tilt):.0f}°, '
                      f'pans={[int(math.degrees(p)) for p in pan_angles]}, '
                      f'primary_hold={primary_hold:.1f}s')
        with self._scan_lock:
            self._scan_detections.clear()
            self._scan_active = True
        try:
            for idx, pan in enumerate(pan_angles):
                if self._stop_evt.is_set():
                    return None
                hold_time = primary_hold if idx == 0 else dwell
                with self._scan_lock:
                    self._scan_detections.clear()
                    self._block_detections_until = time.time() + move_dur + settle
                self.log.info(f'place_item: sweep moving head to pan={math.degrees(pan):.0f}° '
                              f'and holding for {hold_time:.1f}s')
                if not self.motion.move_head(float(pan), tilt, duration=move_dur, wait=True):
                    return None
                if self._stop_evt.is_set():
                    return None
                self.motion.sleep(settle + hold_time)
                best, best_score = self._check_for_surface_detections(surface)
                if best is not None:
                    self.log.info(f'place_item: found {surface!r} during sweep at score={best_score:.2f}, stopping sweep early')
                    break
        finally:
            with self._scan_lock:
                self._scan_active = False
        if self._stop_evt.is_set():
            return None
        if surface != 'trash_can':
            self.motion.move_head(0.0, float(config.scan.head.tilt_neutral), duration=move_dur, wait=True)
        best, best_score = self._check_for_surface_detections(surface)
        if best is None:
            self.log.info(f'place_item: {surface!r} not found in targeted scan')
            return None
        return self._remember_surface(best)

    def _remember_surface(self, best):
        fresh_x = float(best['x'])
        fresh_y = float(best['y'])
        fresh_z = float(best['z'])
        self.log.info(f'place_item: localized {best["class_id"]!r} marker_id={best.get("marker_id")} '
                      f'→ x={fresh_x:.3f}, y={fresh_y:.3f}, z={fresh_z:.3f}')
        with self._lock:
            self._place_hint_map = np.array([fresh_x, fresh_y, fresh_z], dtype=np.float32)
        return fresh_z

    def _final_gripper_alignment(self, surface: str, lift_height: float, arm_extension: float) -> float:
        passes = int(config.final_alignment.passes)
        dwell = float(config.final_alignment.dwell_seconds)
        bearing_tol = math.radians(float(config.final_alignment.bearing_tolerance_deg))
        max_corr = math.radians(float(config.final_alignment.max_correction_deg))
        angular_vel = float(config.final_alignment.angular_vel)
        extension_tol = float(config.final_alignment.extension_tolerance_m)
        for attempt in range(passes):
            with self._scan_lock:
                self._gripper_detections.clear()
                self._gripper_scan_active = True
            try:
                self.motion.sleep(dwell)
            finally:
                with self._scan_lock:
                    self._gripper_scan_active = False
            best, best_score = self._check_for_surface_detections(surface, self._gripper_detections)
            if best is None:
                self.log.info('place_item: gripper camera did not see the surface, keeping current alignment')
                break
            target_base = np.array([float(best['x']), float(best['y']), float(best['z']), ], dtype=np.float32)
            bearing_err = self._bearing_error_rad(target_base)
            planar_dist = math.hypot(float(target_base[0]), float(target_base[1]))
            target_extension = max(float(config.arm.min), min(planar_dist - float(config.place.arm_body_offset_m), float(config.arm.max)))
            self.log.info(f'place_item: gripper check {attempt + 1}/{passes} sees {surface!r} '
                          f'score={best_score:.2f}, bearing_err={math.degrees(bearing_err):.2f}°, '
                          f'target_extension={target_extension:.3f}m')
            aligned = abs(bearing_err) <= bearing_tol
            extension_ok = abs(target_extension - arm_extension) <= extension_tol
            if aligned and extension_ok:
                self.log.info('place_item: final gripper-camera alignment is already good')
                break
            if not aligned:
                self._align_base_to_target(target_base, tolerance_rad=bearing_tol, max_correction_rad=max_corr, angular_vel=angular_vel, )
                if self._stop_evt.is_set():
                    return arm_extension
            if not extension_ok or not aligned:
                arm_extension = target_extension
                self._lower_to_target(lift_height, arm_extension)
                if self._stop_evt.is_set():
                    return arm_extension
        return arm_extension

from __future__ import annotations

import json
import math
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np
import rclpy
from cv_bridge import CvBridge
from geometry_msgs.msg import Point
from gesture_detection_node.gesture_event_gate import TimedGestureEventGate
from gesture_detection_node.gesture_processor import (GestureProcessor, GestureResult, PointingObservation, )
from nav_msgs.msg import OccupancyGrid
from rclpy.callback_groups import (MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup, )
from rclpy.duration import Duration
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import (DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy, )
from rclpy.time import Time
from sensor_msgs.msg import CompressedImage, Image
from std_msgs.msg import ColorRGBA, String
from visualization_msgs.msg import Marker, MarkerArray
from workstation_common import configure_logging, get_config, get_logger

from tool_status.msg import ToolStatus as RosToolStatus
from waypoint_interfaces.msg import Waypoint
from waypoint_interfaces.srv import SavePointedWaypoint, SaveWaypointAtPose


def _sigterm_handler(_signum, _frame):
    raise KeyboardInterrupt()


signal.signal(signal.SIGTERM, _sigterm_handler)
config = get_config('gesture_detection_node')
log = get_logger()


def _stamp_seconds(stamp: Any) -> float:
    return float(stamp.sec) + float(stamp.nanosec) * 1e-9


@dataclass(frozen=True)
class PointingSample:
    received_monotonic: float
    ground_intersection: np.ndarray
    bounded_intersection: np.ndarray
    direction: np.ndarray
    arm_points: dict[str, np.ndarray]
    wall_hit: bool
    confidence: float


@dataclass(frozen=True)
class PendingPointingRequest:
    request_id: int
    requested_monotonic: float
    observation: PointingObservation


class OccupancyMap:
    """Low-traffic wall and reachability view built from the latched /map."""

    def __init__(self, message: OccupancyGrid, cfg: Any, occupancy: np.ndarray | None = None, ):
        self.frame_id = message.header.frame_id or str(cfg.frames.map)
        if self.frame_id != str(cfg.frames.map):
            raise ValueError(f'occupancy grid frame {self.frame_id!r} is not '
                             f'{str(cfg.frames.map)!r}')
        self.resolution = float(message.info.resolution)
        self.width = int(message.info.width)
        self.height = int(message.info.height)
        if (
                self.resolution <= 0.0 or self.width <= 0 or self.height <= 0 or len(message.data) != self.width * self.height):
            raise ValueError('occupancy grid is incomplete')
        origin = message.info.origin
        self.origin_x = float(origin.position.x)
        self.origin_y = float(origin.position.y)
        quaternion = origin.orientation
        self.origin_yaw = math.atan2(2.0 * (
                float(quaternion.w) * float(quaternion.z) + float(quaternion.x) * float(quaternion.y)), 1.0 - 2.0 * (
                                             float(quaternion.y) ** 2 + float(quaternion.z) ** 2), )
        self.cos_yaw = math.cos(self.origin_yaw)
        self.sin_yaw = math.sin(self.origin_yaw)
        self.occupancy = (
            np.asarray(message.data, dtype=np.int16).reshape(self.height, self.width) if occupancy is None else occupancy)
        occupied_threshold = int(cfg.map.occupied_threshold)
        self.free = ((self.occupancy >= 0) & (self.occupancy < occupied_threshold))
        distance_pixels = cv2.distanceTransform(self.free.astype(np.uint8), cv2.DIST_L2, 5)
        minimum_clearance = float(cfg.map.waypoint_clearance_m)
        self.safe = (self.free & (distance_pixels * self.resolution >= minimum_clearance))
        _, self.components = cv2.connectedComponents(self.safe.astype(np.uint8), connectivity=8)

    def matches_message(self, message: OccupancyGrid, occupancy: np.ndarray, ) -> bool:
        origin = message.info.origin
        quaternion = origin.orientation
        origin_yaw = math.atan2(2.0 * (
                float(quaternion.w) * float(quaternion.z) + float(quaternion.x) * float(quaternion.y)), 1.0 - 2.0 * (
                                        float(quaternion.y) ** 2 + float(quaternion.z) ** 2), )
        return (
                self.width == int(message.info.width) and self.height == int(message.info.height) and math.isclose(self.resolution, float(message.info.resolution)) and math.isclose(self.origin_x, float(origin.position.x)) and math.isclose(self.origin_y, float(origin.position.y)) and math.isclose(self.origin_yaw, origin_yaw) and np.array_equal(self.occupancy, occupancy))

    def world_to_grid(self, x: float, y: float, ) -> tuple[int, int] | None:
        delta_x = float(x) - self.origin_x
        delta_y = float(y) - self.origin_y
        local_x = self.cos_yaw * delta_x + self.sin_yaw * delta_y
        local_y = -self.sin_yaw * delta_x + self.cos_yaw * delta_y
        column = int(math.floor(local_x / self.resolution))
        row = int(math.floor(local_y / self.resolution))
        if not (0 <= column < self.width and 0 <= row < self.height):
            return None
        return row, column

    def grid_to_world(self, row: int, column: int) -> np.ndarray:
        local_x = (float(column) + 0.5) * self.resolution
        local_y = (float(row) + 0.5) * self.resolution
        return np.asarray([self.origin_x + self.cos_yaw * local_x - self.sin_yaw * local_y,
                           self.origin_y + self.sin_yaw * local_x + self.cos_yaw * local_y, ], dtype=np.float64)

    def bound_ray(self, origin_xy: np.ndarray, target_xy: np.ndarray, *, wall_clearance_m: float, origin_ignore_m: float, ) -> \
    tuple[np.ndarray, bool]:
        vector = np.asarray(target_xy, dtype=np.float64) - np.asarray(origin_xy, dtype=np.float64)
        length = float(np.linalg.norm(vector))
        if length < 1e-6:
            return np.asarray(target_xy, dtype=np.float64), False
        unit = vector / length
        step = max(self.resolution * 0.5, 0.01)
        distances = np.arange(0.0, length + step, step)
        hit_distance = None
        for distance in distances:
            point = np.asarray(origin_xy, dtype=np.float64) + unit * distance
            cell = self.world_to_grid(float(point[0]), float(point[1]))
            if cell is None:
                hit_distance = float(distance)
                break
            if distance < float(origin_ignore_m):
                continue
            if not self.free[cell]:
                hit_distance = float(distance)
                break
        if hit_distance is None:
            return np.asarray(target_xy, dtype=np.float64), False
        bounded_distance = max(0.0, hit_distance - max(0.0, float(wall_clearance_m)))
        return (np.asarray(origin_xy, dtype=np.float64) + unit * bounded_distance, True,)

    def reachable_waypoint(self, target_xy: np.ndarray, robot_xy: np.ndarray, *, start_search_m: float, target_search_m: float, ) -> np.ndarray | None:
        robot_cell = self.world_to_grid(float(robot_xy[0]), float(robot_xy[1]))
        target_cell = self.world_to_grid(float(target_xy[0]), float(target_xy[1]))
        if robot_cell is None or target_cell is None:
            return None
        start = self._nearest_cell(robot_cell, self.safe, float(start_search_m))
        if start is None:
            return None
        component = int(self.components[start])
        if component == 0:
            return None
        component_mask = self.components == component
        candidate = self._nearest_cell(target_cell, component_mask, float(target_search_m))
        if candidate is None:
            return None
        return self.grid_to_world(*candidate)

    def _nearest_cell(self, center: tuple[int, int], mask: np.ndarray, radius_m: float, ) -> tuple[int, int] | None:
        center_row, center_column = center
        radius = max(0, int(math.ceil(radius_m / self.resolution)))
        row_start = max(0, center_row - radius)
        row_end = min(self.height, center_row + radius + 1)
        column_start = max(0, center_column - radius)
        column_end = min(self.width, center_column + radius + 1)
        rows, columns = np.nonzero(mask[row_start:row_end, column_start:column_end])
        if rows.size == 0:
            return None
        rows = rows + row_start
        columns = columns + column_start
        squared_distance = ((rows - center_row) ** 2 + (columns - center_column) ** 2)
        index = int(np.argmin(squared_distance))
        if (math.sqrt(float(squared_distance[index])) * self.resolution > float(radius_m) + self.resolution * 0.5):
            return None
        return int(rows[index]), int(columns[index])


class GestureDetectionNode(Node):
    """
    Unified workstation-only MediaPipe hand and right-arm pointing detector.
    Handoff gestures can use the whole camera image without person tracking.
    Legacy tracked/current-visible sessions remain supported. Pointing still
    requires one person. Right-arm pixels go to the robot's existing depth
    mapper, so raw depth never crosses the network.
    """

    def __init__(self) -> None:
        super().__init__('gesture_detection_node')
        self.log = log
        self.bridge = CvBridge()
        self.processor = GestureProcessor(config)
        self.state_lock = threading.RLock()
        self.processing_group = MutuallyExclusiveCallbackGroup()
        self.sensor_group = ReentrantCallbackGroup()
        self.service_group = ReentrantCallbackGroup()
        self.gesture_armed = False
        self.handoff_session = ''
        self.handoff_armed_at = 0.
        self.handoff_action = ''
        self.handoff_last_frame_stamp = 0.
        self.handoff_track = None
        self.handoff_recipient_policy = 'tracked'
        self.current_recipient_bbox = None
        self.current_recipient_stamp = 0.
        self.current_recipient_received = 0.
        self.current_recipient_reason = 'recipient_not_visible'
        self.current_recipient_distance = None
        self.person_track = None
        self.person_capture_stamp = 0.
        self._people_by_track = {}
        self.hand_stable_count = 0
        self.hand_event_gate = TimedGestureEventGate(stable_frames=int(config.hand.stable_frames),
            window_seconds=float(config.hand.confirmation_window_seconds),
            min_hold_seconds=float(config.hand.min_hold_seconds),
            release_seconds=float(config.hand.release_seconds),
            max_gap_seconds=float(config.hand.max_observation_gap_seconds))
        self.last_hand_diagnostic = 0.0
        self.person_count = 0
        self.person_bbox: dict[str, Any] | None = None
        self.last_person_detection_monotonic = 0.0
        self.pointing_stable_count = 0
        self.last_pointing_process_monotonic = 0.0
        self.last_pointing_diagnostic_monotonic = 0.0
        self.last_pointing_sample: PointingSample | None = None
        self.next_pointing_request_id = 1
        self.pending_pointing_request: PendingPointingRequest | None = None
        self.occupancy_map: OccupancyMap | None = None
        self.image_subscription = None
        image_qos = QoSProfile(depth=int(config.qos.image_depth), reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST, )
        self.image_qos = image_qos
        detections_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST, )
        control_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST, )
        map_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL, history=HistoryPolicy.KEEP_LAST, )
        live_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST, )
        self.gesture_publisher = self.create_publisher(String, config.topics.gesture, live_qos)
        self.handoff_status_publisher = self.create_publisher(String, config.topics.status, live_qos)
        self.marker_publisher = self.create_publisher(MarkerArray, config.topics.markers, live_qos)
        self.depth_request_publisher = self.create_publisher(String, config.topics.depth_request, control_qos)
        self.overlay_publisher = None
        if bool(config.debug.publish_hand_overlay):
            self.overlay_publisher = self.create_publisher(Image, config.topics.overlay, live_qos)
        self.create_subscription(String, config.topics.control, self.control_callback, control_qos, callback_group=self.sensor_group, )
        self.create_subscription(String, config.topics.person_detections, self.person_detection_callback, detections_qos, callback_group=self.sensor_group, )
        self.create_subscription(String, config.topics.handoff_person_detections, self.handoff_person_detection_callback, detections_qos, callback_group=self.sensor_group, )
        self.create_subscription(OccupancyGrid, config.topics.map, self.map_callback, map_qos, callback_group=self.sensor_group, )
        self.create_subscription(String, config.topics.depth_response, self.pointing_depth_response_callback, control_qos, callback_group=self.sensor_group, )
        import tf2_ros

        self.tf_buffer = tf2_ros.Buffer(cache_time=Duration(seconds=5.0))
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)
        self.waypoint_client = self.create_client(SaveWaypointAtPose, config.services.save_waypoint_pose, callback_group=self.service_group, )
        self.save_pointed_service = self.create_service(SavePointedWaypoint, config.services.save_pointed_waypoint, self.save_pointed_waypoint_callback, callback_group=self.service_group, )
        self.create_timer(float(config.timing.gate_check_period_seconds), self.gate_timer_callback, callback_group=self.sensor_group, )
        self._refresh_sensor_subscriptions()
        self.log.info('Unified gesture detector ready on workstation: '
                      f'person_gate={config.topics.person_detections}, '
                      f'image={config.topics.image}, '
                      f'depth_sampler={config.topics.depth_request}, '
                      f'map={config.topics.map}; '
                      'pointing sleeps until exactly one person is detected')

    def destroy_node(self):
        self.processor.close()
        return super().destroy_node()

    def control_callback(self, message: String) -> None:
        try:
            payload = json.loads(message.data)
        except (json.JSONDecodeError, AttributeError, TypeError):
            return
        command = str(payload.get('command') or '')
        if command == 'arm':
            try:
                session = str(payload['session_id'])
                armed_at = float(payload['armed_at'])
                track = payload['track_id']
                policy = payload.get('recipient_policy', 'tracked')
                action = str(payload.get('action', ''))
                if (policy not in ('tracked','current_visible','gesture_only') or
                        (policy == 'gesture_only' and action not in ('take_from_hand','give_to_person')) or
                        (policy == 'current_visible' and action != 'give_to_person') or
                        not session or not math.isfinite(armed_at) or (policy == 'tracked' and track is None)):
                    return
            except (KeyError, TypeError, ValueError):
                return
            with self.state_lock:
                self.handoff_session = session
                self.handoff_armed_at = armed_at
                self.handoff_track = None if policy == 'gesture_only' else track
                self.handoff_recipient_policy = policy
                self.handoff_action = action
                self.handoff_last_frame_stamp = 0.
                self._last_status_report = -math.inf
                self.hand_stable_count = 0
                self.hand_event_gate.reset(require_release=True)
                self.gesture_armed = True
                self.last_hand_diagnostic = 0.0
            self._refresh_sensor_subscriptions()
            self.log.info('Thumbs-up detector armed')
        elif command == 'disarm':
            with self.state_lock:
                if payload.get('session_id') and payload['session_id'] != self.handoff_session:
                    return
                self.gesture_armed = False
                self.hand_stable_count = 0
                self.hand_event_gate.reset()
            self._refresh_sensor_subscriptions()
            self.log.info('Thumbs-up detector disarmed')

    def person_detection_callback(self, message: String) -> None:
        payload = {}
        try:
            payload = json.loads(message.data)
            if payload.get('source', 'obj') != 'obj':
                return
            detections = payload.get('detections') or []
        except (json.JSONDecodeError, AttributeError, TypeError):
            detections = []
        people = [detection for detection in detections if
            isinstance(detection, dict) and str(detection.get('label') or '').casefold().startswith('person')]
        bbox = None
        if len(people) == 1:
            candidate = people[0].get('bbox_2d')
            if self._valid_bbox(candidate):
                bbox = dict(candidate)
        tracked = {}
        duplicate_tracks = set()
        for person in people:
            track = person.get('track_id')
            candidate = person.get('bbox_2d')
            if not isinstance(track,int) or isinstance(track,bool) or track < 0 or not self._valid_bbox(candidate):
                continue
            if track in tracked:
                duplicate_tracks.add(track)
            tracked[track] = dict(candidate)
        for track in duplicate_tracks:
            tracked.pop(track,None)
        with self.state_lock:
            try:
                capture = float(payload['header']['stamp'])
            except (KeyError,TypeError,ValueError):
                capture = 0.
            if capture > 0 and capture <= getattr(self,'person_capture_stamp',0.):
                return
            self.person_count = len(people)
            self.person_bbox = bbox
            self.person_track = people[0].get('track_id') if len(people) == 1 else None
            self.person_capture_stamp = capture if math.isfinite(capture) else 0.
            self._people_by_track = tracked if self.person_capture_stamp > 0 else {}
            self.last_person_detection_monotonic = time.monotonic()
            if len(people) != 1 or bbox is None:
                self.pointing_stable_count = 0
                self.pending_pointing_request = None
        if len(people) != 1 or bbox is None:
            self._clear_markers()
        self._refresh_sensor_subscriptions()

    def handoff_person_detection_callback(self, message: String) -> None:
        """Use the same current 3D view as the robot, without an identity gate."""
        try:
            payload = json.loads(message.data)
            if not isinstance(payload,dict) or payload.get('source','obj') != 'obj':
                return
            stamp = float(payload['header']['stamp'])
            if not math.isfinite(stamp) or stamp <= 0:
                return
            candidates = []
            reason='recipient_position_unavailable'
            if payload['header'].get('frame_id') == 'base_link':
                for person in payload.get('detections',[]):
                    if not isinstance(person,dict) or not str(person.get('label','')).casefold().startswith('person'):
                        continue
                    try:
                        position = person['position_3d']
                        x,y = float(position['x']),float(position['y'])
                        confidence = float(person['confidence'])
                        if all(math.isfinite(value) for value in (x,y,confidence)) and .5 <= confidence <= 1.:
                            candidates.append((math.hypot(x,y),person))
                    except (KeyError,TypeError,ValueError,OverflowError):
                        continue
            bbox = None
            distance=None
            if candidates:
                distance,person = min(candidates,key=lambda candidate:candidate[0])
                reason='recipient_out_of_range' if not .8 <= distance <= 2. else 'recipient_bbox_invalid'
                if .8 <= distance <= 2. and self._valid_bbox(person.get('bbox_2d')):
                    bbox = dict(person['bbox_2d'])
                    reason='recipient_visible'
            with self.state_lock:
                if stamp <= self.current_recipient_stamp:
                    return
                self.current_recipient_bbox = bbox
                self.current_recipient_stamp = stamp
                self.current_recipient_received = time.monotonic()
                self.current_recipient_reason = reason
                self.current_recipient_distance = distance
        except (json.JSONDecodeError,AttributeError,KeyError,TypeError,ValueError,OverflowError):
            return

    def map_callback(self, message: OccupancyGrid) -> None:
        try:
            width = int(message.info.width)
            height = int(message.info.height)
            if width <= 0 or height <= 0 or len(message.data) != width * height:
                raise ValueError('occupancy grid is incomplete')
            occupancy = np.asarray(message.data, dtype=np.int16).reshape(height, width)
            with self.state_lock:
                if (self.occupancy_map is not None and self.occupancy_map.matches_message(message, occupancy)):
                    return
            candidate = OccupancyMap(message, config, occupancy)
        except (TypeError, ValueError) as exc:
            self.log.warning(f'Ignoring invalid navigation map: {exc}')
            return
        with self.state_lock:
            self.occupancy_map = candidate
        self.log.info(f'Pointing map ready: {candidate.width}x{candidate.height} '
                      f'at {candidate.resolution:.3f}m/cell')

    def gate_timer_callback(self) -> None:
        now = time.monotonic()
        with self.state_lock:
            stale = (
                    self.last_person_detection_monotonic > 0.0 and now - self.last_person_detection_monotonic > float(config.timing.person_detection_ttl_seconds))
            if stale:
                self.person_count = 0
                self.person_bbox = None
                self._people_by_track = {}
                self.pointing_stable_count = 0
                self.pending_pointing_request = None
        if stale:
            self._clear_markers()
            self._refresh_sensor_subscriptions()

    def _pointing_gate(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else float(now)
        with self.state_lock:
            return (
                    self.person_count == 1 and self.person_bbox is not None and current - self.last_person_detection_monotonic <= float(config.timing.person_detection_ttl_seconds))

    def _handoff_person_bbox(self):
        return self._handoff_person_evidence()[0]

    def _handoff_person_evidence(self):
        with self.state_lock:
            if self.handoff_recipient_policy == 'current_visible':
                bbox = self.current_recipient_bbox
                if time.monotonic() - self.current_recipient_received > float(config.timing.person_detection_ttl_seconds):
                    bbox = None
                return dict(bbox) if bbox is not None else None, self.current_recipient_stamp
            if time.monotonic() - self.last_person_detection_monotonic > float(config.timing.person_detection_ttl_seconds):
                return None, self.person_capture_stamp
            bbox = self._people_by_track.get(self.handoff_track)
            return dict(bbox) if bbox is not None else None, self.person_capture_stamp

    @staticmethod
    def _valid_bbox(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        try:
            return (float(value['width']) > 1.0 and float(value['height']) > 1.0 and
                    all(math.isfinite(float(value[key])) for key in ('width', 'height', 'center_x', 'center_y')))
        except (KeyError, TypeError, ValueError):
            return False

    def _refresh_sensor_subscriptions(self) -> None:
        # Never destroy a reader while the multithreaded executor may already
        # have scheduled its callback. Idle frames return before decoding.
        with self.state_lock:
            if self.image_subscription is None:
                self.image_subscription = self.create_subscription(CompressedImage, config.topics.image, self.image_callback, self.image_qos, callback_group=self.processing_group, )

    def image_callback(self, message: CompressedImage) -> None:
        now = time.monotonic()
        with self.state_lock:
            hand_enabled = self.gesture_armed
            pointing_enabled = not hand_enabled and self._pointing_gate(now)
            person_bbox = (dict(self.person_bbox) if pointing_enabled and self.person_bbox is not None else None)
        if not hand_enabled and not pointing_enabled:
            return
        try:
            image = self._decode_color(message)
            if image is None:
                return
            if hand_enabled:
                self._process_handoff_gesture(image, message)
            point_period = float(config.timing.pointing_processing_period_seconds)
            if (
                    pointing_enabled and person_bbox is not None and now - self.last_pointing_process_monotonic >= point_period):
                self.last_pointing_process_monotonic = now
                self._process_pointing(image, person_bbox, message, now)
        except Exception as exc:
            self.log.warning(f'Gesture frame processing failed: {exc}')

    @staticmethod
    def _decode_color(message: CompressedImage) -> np.ndarray | None:
        encoded = np.frombuffer(message.data, dtype=np.uint8)
        return cv2.imdecode(encoded, cv2.IMREAD_COLOR)

    def _process_handoff_gesture(self, image: np.ndarray, message: CompressedImage, ) -> None:
        capture = _stamp_seconds(message.header.stamp)
        now_ros = self.get_clock().now().nanoseconds / 1e9
        with self.state_lock:
            session = self.handoff_session
            policy = self.handoff_recipient_policy
            selected_track = self.handoff_track
            if policy == 'gesture_only':
                # Camera alignment is set by the handoff action. A hand may
                # fill the view while the person's face/body is out of frame.
                height, width = image.shape[:2]
                bbox = {'center_x': width / 2, 'center_y': height / 2,
                        'width': width, 'height': height}
                person_stamp = None
            else:
                bbox,person_stamp = self._handoff_person_evidence()
            if capture <= self.handoff_last_frame_stamp:
                self._report_handoff_status('replayed_image',capture,person_stamp)
                return
            if (not self.gesture_armed or bbox is None or
                    not self.handoff_armed_at <= capture <= now_ros or now_ros - capture > 1.2 or
                    (policy != 'gesture_only' and (not 0 <= now_ros - person_stamp <= 1.2 or
                    abs(capture - person_stamp) > .5))):
                self.hand_stable_count = 0
                self.hand_event_gate.invalidate()
                if not self.gesture_armed:reason='not_armed'
                elif bbox is None:
                    reason=('person_detection_stale_or_future' if not 0<=now_ros-person_stamp<=1.2 else
                        getattr(self,'current_recipient_reason','recipient_crop_unavailable')
                        if policy=='current_visible' else 'recipient_crop_unavailable')
                elif capture<self.handoff_armed_at:reason='image_before_arming'
                elif not 0<=now_ros-capture<=1.2:reason='image_stale_or_future'
                elif person_stamp is not None and not 0<=now_ros-person_stamp<=1.2:reason='person_detection_stale_or_future'
                else:reason='image_person_time_mismatch'
                self._report_handoff_status(reason,capture,person_stamp)
                return
            if self.handoff_last_frame_stamp and capture - self.handoff_last_frame_stamp > float(config.hand.max_observation_gap_seconds):
                self.hand_stable_count = 0
                self.hand_event_gate.invalidate()
                self._report_handoff_status('frame_gap_reset',capture,person_stamp)
            self.handoff_last_frame_stamp = capture
        height, width = image.shape[:2]
        x0 = max(0, int(float(bbox['center_x']) - float(bbox['width']) * .55))
        x1 = min(width, int(float(bbox['center_x']) + float(bbox['width']) * .55))
        y0 = max(0, int(float(bbox['center_y']) - float(bbox['height']) * .55))
        y1 = min(height, int(float(bbox['center_y']) + float(bbox['height']) * .55))
        if x1 <= x0 or y1 <= y0:
            self._report_handoff_status('empty_person_crop',capture,person_stamp)
            return
        gesture, annotated = self.processor.process_hands(image[y0:y1, x0:x1], annotate=self.overlay_publisher is not None)
        with self.state_lock:
            if (not self.gesture_armed or self.handoff_session != session or
                    self.handoff_recipient_policy != policy or
                    (policy == 'tracked' and self.handoff_track != selected_track) or
                    (policy != 'gesture_only' and self._handoff_person_bbox() is None)):
                self._report_handoff_status('session_or_recipient_changed',capture,person_stamp)
                return
            now_ros = self.get_clock().now().nanoseconds / 1e9
            if policy != 'gesture_only':
                _,person_stamp = self._handoff_person_evidence()
            if (not 0 <= now_ros - capture <= 1.2 or
                    (policy != 'gesture_only' and not 0 <= now_ros - person_stamp <= 1.2)):
                self.hand_stable_count = 0
                self.hand_event_gate.invalidate()
                self._report_handoff_status('inference_result_stale',capture,person_stamp)
                return
            emit = self.hand_event_gate.update(gesture.name == 'thumbs_up',stamp=capture)
            stable_count = self.hand_event_gate.active_frames
            self.hand_stable_count = stable_count
            reason = ('confirmed' if emit else 'neutral_phase_required' if self.hand_event_gate.latched else
                'thumb_not_stable' if gesture.name == 'thumbs_up' else getattr(gesture,'reason','') or 'no_thumb')
            self._report_handoff_status(reason,capture,person_stamp,confidence=float(gesture.confidence),
                crop={'x':x0,'y':y0,'width':x1-x0,'height':y1-y0})
        now = time.monotonic()
        if now - self.last_hand_diagnostic >= 2.0:
            self.last_hand_diagnostic = now
            self.log.info(f'Hand candidate={gesture.name or gesture.reason}, '
                          f'confidence={gesture.confidence:.2f}, '
                          f'stable={stable_count}/{int(config.hand.stable_frames)}')
        if self.overlay_publisher is not None and annotated is not None:
            self._publish_hand_overlay(annotated, gesture, stable_count, message)
        if not emit:
            return
        payload = {
            'gesture': 'thumbs_up',
            'confidence': round(float(gesture.confidence), 3),
            'stable_frames': int(stable_count),
            'session_id': session, 'action': self.handoff_action, 'track_id': selected_track,
            'recipient_policy':policy,
            'stamp': _stamp_seconds(message.header.stamp), }
        self.gesture_publisher.publish(String(data=json.dumps(payload)))
        # Stay armed until the robot consumes/disarms this session. If the
        # robot rejects an event as stale or temporarily ambiguous, a new
        # release-and-thumbs-up can still complete the same waiting workflow.
        self.log.info('Published thumbs_up from unified gesture detector: '
                      f'confidence={gesture.confidence:.2f}')

    def _report_handoff_status(self, reason, capture=None, person_stamp=None, **details):
        """Bounded operator diagnostics, including frames rejected before ML."""
        if not self.gesture_armed:
            return
        now=time.monotonic()
        if reason != 'confirmed' and now-getattr(self,'_last_status_report',-math.inf) < 1.:
            return
        self._last_status_report=now
        clock=self.get_clock().now().nanoseconds/1e9
        gate=self.hand_event_gate
        payload={'session_id':self.handoff_session,'action':self.handoff_action,'stamp':clock,
            'reason':str(reason),'stable_frames':self.hand_stable_count,
            'neutral_frames':gate.neutral_frames,'release_latched':gate.latched,
            'image_age_seconds':None if capture is None else round(clock-capture,3),
            'person_age_seconds':None if person_stamp is None else round(clock-person_stamp,3),**details}
        if self.handoff_recipient_policy=='current_visible':
            payload['recipient_distance_m']=getattr(self,'current_recipient_distance',None)
        self.log.info('Handover gate: '+json.dumps(payload,ensure_ascii=False))
        publisher=getattr(self,'handoff_status_publisher',None)
        if publisher is not None:
            publisher.publish(String(data=json.dumps(payload,ensure_ascii=False)))

    def _publish_hand_overlay(self, image: np.ndarray, gesture: GestureResult, stable_count: int, message: CompressedImage, ) -> None:
        label = gesture.name or gesture.reason or 'no_gesture'
        cv2.putText(image, f'{label} {gesture.confidence:.2f} stable={stable_count}', (12,
                                                                                       28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (
            0,
            255,
            0) if gesture.name else (0, 0, 255), 2, cv2.LINE_AA, )
        output = self.bridge.cv2_to_imgmsg(image, encoding='bgr8')
        output.header = message.header
        self.overlay_publisher.publish(output)

    def _process_pointing(self, image: np.ndarray, bbox: dict[
        str, Any], color_message: CompressedImage, now: float, ) -> None:
        if not self._pointing_gate(now):
            return
        observation = self.processor.process_pointing(image, bbox)
        if not observation.pixels:
            self._reject_pointing(observation.reason)
            return
        with self.state_lock:
            pending = self.pending_pointing_request
            if (
                    pending is not None and now - pending.requested_monotonic <= float(config.timing.depth_request_timeout_seconds)):
                return
            request_id = self.next_pointing_request_id
            self.next_pointing_request_id += 1
            self.pending_pointing_request = PendingPointingRequest(request_id=request_id, requested_monotonic=now, observation=observation, )
        height, width = image.shape[:2]
        request = String()
        request.data = json.dumps({
            'request_id': request_id,
            'header': {
                'stamp': _stamp_seconds(color_message.header.stamp),
                'frame_id': color_message.header.frame_id, },
            'image': {'width': width, 'height': height},
            'points': {name: {'x': float(pixel[0]), 'y': float(pixel[1])} for name, pixel in
                observation.pixels.items()}, }, separators=(',', ':'))
        self.depth_request_publisher.publish(request)

    def pointing_depth_response_callback(self, message: String) -> None:
        try:
            payload = json.loads(message.data)
            request_id = int(payload['request_id'])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError,):
            return
        now = time.monotonic()
        with self.state_lock:
            pending = self.pending_pointing_request
            if pending is None or pending.request_id != request_id:
                return
            self.pending_pointing_request = None
            occupancy_map = self.occupancy_map
        if not self._pointing_gate(now):
            return
        outcome = str(payload.get('outcome') or 'invalid_response')
        if outcome != 'success':
            self._reject_pointing(f'depth_sampler_{outcome}')
            return
        if str(payload.get('frame_id') or '') != str(config.frames.map):
            self._reject_pointing('depth_sampler_wrong_frame')
            return
        arm_points = {}
        for name, point in (payload.get('points') or {}).items():
            if name not in ('shoulder', 'elbow', 'wrist', 'index'):
                continue
            try:
                candidate = np.asarray([float(point['x']), float(point['y']), float(point['z']), ], dtype=np.float64)
            except (KeyError, TypeError, ValueError):
                continue
            if np.all(np.isfinite(candidate)):
                arm_points[name] = candidate
        if arm_points:
            self._publish_markers(arm_points)
        observation = pending.observation
        if not observation.is_pointing:
            self._reject_pointing(observation.reason)
            return
        missing_depth = [name for name in ('elbow', 'wrist') if name not in arm_points]
        if missing_depth:
            self._reject_pointing('right_arm_depth_missing:' + ','.join(missing_depth))
            return
        if occupancy_map is None:
            self._reject_pointing('map_unavailable')
            return
        projection = self._project_right_arm(arm_points, occupancy_map)
        if projection is None:
            self._reject_pointing('right_arm_has_no_ground_intersection')
            return
        ground, bounded, direction, wall_hit = projection
        self._publish_markers(arm_points, bounded_intersection=bounded, wall_hit=wall_hit, )
        with self.state_lock:
            self.pointing_stable_count += 1
            stable_count = self.pointing_stable_count
            if (now - self.last_pointing_diagnostic_monotonic >= 2.0):
                self.last_pointing_diagnostic_monotonic = now
                self.log.info('Pointing ready: '
                              f'confidence={observation.confidence:.2f}, '
                              f'stable={stable_count}/'
                              f'{int(config.pointing.stable_frames)}, '
                              f'ground=({ground[0]:.2f}, {ground[1]:.2f}), '
                              f'bounded=({bounded[0]:.2f}, {bounded[1]:.2f}), '
                              f'wall_hit={wall_hit}')
            if self.pointing_stable_count < int(config.pointing.stable_frames):
                return
            self.last_pointing_sample = PointingSample(received_monotonic=now, ground_intersection=ground, bounded_intersection=bounded, direction=direction, arm_points=dict(arm_points), wall_hit=wall_hit, confidence=float(observation.confidence), )

    def _reject_pointing(self, reason: str) -> None:
        now = time.monotonic()
        with self.state_lock:
            self.pointing_stable_count = 0
            if now - self.last_pointing_diagnostic_monotonic < 2.0:
                return
            self.last_pointing_diagnostic_monotonic = now
        self.log.info(f'Pointing idle: {reason or "unknown"}')

    def _project_right_arm(self, arm_points: dict[str, np.ndarray], occupancy_map: OccupancyMap, ) -> tuple[
                                                                                                          np.ndarray, np.ndarray, np.ndarray, bool] | None:
        elbow = arm_points.get('elbow')
        wrist = arm_points.get('wrist')
        if elbow is None or wrist is None:
            return None
        endpoint = wrist
        index = arm_points.get('index')
        if (
                index is not None and float(np.linalg.norm(index - wrist)) <= float(config.pointing.max_wrist_index_separation_m)):
            endpoint = index
        direction = endpoint - elbow
        norm = float(np.linalg.norm(direction))
        if norm < float(config.pointing.min_forearm_length_m):
            return None
        direction /= norm
        if direction[2] >= -float(config.pointing.min_downward_direction_z):
            return None
        distance_to_ground = -float(elbow[2]) / float(direction[2])
        if distance_to_ground <= 0.0:
            return None
        ground = elbow + direction * distance_to_ground
        ground[2] = 0.0
        horizontal_distance = float(np.linalg.norm(ground[:2] - elbow[:2]))
        if horizontal_distance > float(config.pointing.max_ground_intersection_distance_m):
            return None
        bounded_xy, wall_hit = occupancy_map.bound_ray(elbow[:2], ground[
            :2], wall_clearance_m=float(config.map.wall_clearance_m), origin_ignore_m=float(config.map.raycast_origin_ignore_m), )
        bounded = np.asarray([bounded_xy[0], bounded_xy[1], 0.0], dtype=np.float64)
        return ground, bounded, direction, wall_hit

    def _publish_markers(self, arm_points: dict[
        str, np.ndarray], *, bounded_intersection: np.ndarray | None = None, wall_hit: bool = False, ) -> None:
        markers = MarkerArray()
        stamp = self.get_clock().now().to_msg()
        ordered = [arm_points[name] for name in ('shoulder', 'elbow', 'wrist', 'index') if name in arm_points]
        if len(ordered) >= 2:
            arm = Marker()
            arm.header.frame_id = str(config.frames.map)
            arm.header.stamp = stamp
            arm.ns = 'right_arm'
            arm.id = 0
            arm.type = Marker.LINE_STRIP
            arm.action = Marker.ADD
            arm.pose.orientation.w = 1.0
            arm.scale.x = float(config.debug.arm_line_width_m)
            arm.color = ColorRGBA(r=0.1, g=1.0, b=0.2, a=0.95)
            arm.points = [self._point_message(point) for point in ordered]
            arm.lifetime = Duration(seconds=float(config.debug.marker_lifetime_seconds)).to_msg()
            markers.markers.append(arm)
        elbow = arm_points.get('elbow')
        if bounded_intersection is not None and elbow is not None:
            ray = Marker()
            ray.header.frame_id = str(config.frames.map)
            ray.header.stamp = stamp
            ray.ns = 'bounded_pointing_ray'
            ray.id = 1
            ray.type = Marker.ARROW
            ray.action = Marker.ADD
            ray.pose.orientation.w = 1.0
            ray.scale.x = float(config.debug.ray_shaft_width_m)
            ray.scale.y = float(config.debug.ray_head_width_m)
            ray.scale.z = float(config.debug.ray_head_length_m)
            ray.color = (ColorRGBA(r=1.0, g=0.2, b=0.1, a=0.95) if wall_hit else ColorRGBA(r=0.1, g=0.7, b=1.0, a=0.95))
            ray.points = [self._point_message(elbow), self._point_message(bounded_intersection), ]
            ray.lifetime = Duration(seconds=float(config.debug.marker_lifetime_seconds)).to_msg()
            markers.markers.append(ray)
        self.marker_publisher.publish(markers)

    @staticmethod
    def _point_message(point: np.ndarray) -> Point:
        return Point(x=float(point[0]), y=float(point[1]), z=float(point[2]))

    def _clear_markers(self) -> None:
        marker = Marker()
        marker.action = Marker.DELETEALL
        self.marker_publisher.publish(MarkerArray(markers=[marker]))

    def save_pointed_waypoint_callback(self, request: SavePointedWaypoint.Request, response: SavePointedWaypoint.Response, ) -> SavePointedWaypoint.Response:
        name = str(request.name or '').strip()
        if not name:
            self.log.warning('save_pointed_waypoint: empty name rejected')
            return self._pointed_response(response, RosToolStatus.STATUS_FAILURE, 'invalid_name', 'A waypoint name is required.', )
        now = time.monotonic()
        configured_hysteresis = float(config.timing.hysteresis_seconds)
        requested_hysteresis = float(request.max_pointing_age_seconds)
        if requested_hysteresis <= 0.0:
            requested_hysteresis = configured_hysteresis
        maximum_age = min(configured_hysteresis, requested_hysteresis)
        with self.state_lock:
            sample = self.last_pointing_sample
            person_count = self.person_count
            detection_age = now - self.last_person_detection_monotonic
            occupancy_map = self.occupancy_map
        person_fresh = (detection_age <= float(config.timing.person_detection_ttl_seconds))
        if person_fresh and person_count > 1:
            self.log.warning(f'save_pointed_waypoint: multiple people detected '
                             f'(count={person_count})')
            return self._pointed_response(response, RosToolStatus.STATUS_FAILURE, 'multiple_people', 'Pointing requires exactly one person; multiple people are '
                                                                                                     'currently visible.', )
        if sample is None:
            if not person_fresh or person_count == 0:
                outcome = 'no_person'
                message = ('No person is currently confirmed by the head camera.')
            else:
                outcome = 'no_pointing'
                message = ('One person is visible, but no valid right-arm pointing '
                           'gesture has been recorded.')
            self.log.warning(f'save_pointed_waypoint: {outcome} '
                             f'(person_fresh={person_fresh}, person_count={person_count})')
            return self._pointed_response(response, RosToolStatus.STATUS_FAILURE, outcome, message)
        age = max(0.0, now - sample.received_monotonic)
        response.pointing_age_seconds = float(age)
        if age > maximum_age:
            self.log.warning(f'save_pointed_waypoint: expired hysteresis '
                             f'(age={age:.2f}s, max={maximum_age:.2f}s)')
            return self._pointed_response(response, RosToolStatus.STATUS_FAILURE, 'expired_hysteresis', f'The last pointing location is {age:.2f}s old; '
                                                                                                        f'the maximum is {maximum_age:.2f}s.', )
        current = (self._pointing_gate(now) and age <= float(config.timing.current_pointing_seconds))
        response.used_hysteresis = not current
        response.wall_hit = bool(sample.wall_hit)
        waypoint_xy = sample.bounded_intersection[:2]
        yaw = math.atan2(float(sample.direction[1]), float(sample.direction[0]))
        waypoint = Waypoint()
        waypoint.name = name
        waypoint.x = float(waypoint_xy[0])
        waypoint.y = float(waypoint_xy[1])
        waypoint.orientation = math.degrees(yaw) % 360.0
        waypoint.qx = 0.0
        waypoint.qy = 0.0
        waypoint.qz = math.sin(yaw / 2.0)
        waypoint.qw = math.cos(yaw / 2.0)
        waypoint.frame_id = str(config.frames.map)
        saved = self._save_waypoint_at_pose(waypoint)
        if (saved is None or saved.status.status != RosToolStatus.STATUS_SUCCESS):
            detail = (saved.status.message if saved is not None else 'waypoint service unavailable')
            self.log.warning(f'save_pointed_waypoint: save failed — {detail}')
            return self._pointed_response(response, RosToolStatus.STATUS_FAILURE, 'save_failed', f'Pointing was valid, but the waypoint could not be saved: '
                                                                                                 f'{detail}', )
        response.waypoint = saved.waypoint
        outcome = 'wall_hit' if sample.wall_hit else 'success'
        qualifier = (' The ray hit a mapped wall and was capped inside the room.' if sample.wall_hit else '')
        hysteresis = (f' Used the {age:.2f}s-old pointing sample.' if response.used_hysteresis else '')
        self.log.info(f'save_pointed_waypoint: saved {saved.waypoint.name!r} at '
                      f'({saved.waypoint.x:.2f}, {saved.waypoint.y:.2f}) '
                      f'outcome={outcome}')
        return self._pointed_response(response, RosToolStatus.STATUS_SUCCESS, outcome, f'Saved waypoint {saved.waypoint.name!r} at '
                                                                                       f'({saved.waypoint.x:.2f}, {saved.waypoint.y:.2f}).'
                                                                                       f'{qualifier}{hysteresis}', )

    def _robot_position_in_map(self) -> np.ndarray | None:
        try:
            transform = self.tf_buffer.lookup_transform(str(config.frames.map), str(config.frames.base), Time(), timeout=Duration(seconds=float(config.timing.tf_timeout_seconds)), )
        except Exception:
            return None
        translation = transform.transform.translation
        return np.asarray([translation.x, translation.y], dtype=np.float64)

    def _save_waypoint_at_pose(self, waypoint: Waypoint, ) -> SaveWaypointAtPose.Response | None:
        if not self.waypoint_client.wait_for_service(timeout_sec=0.5):
            self.log.warning('_save_waypoint_at_pose: waypoint service unavailable '
                             '(service not reachable after 0.5s)')
            return None
        request = SaveWaypointAtPose.Request()
        request.waypoint = waypoint
        future = self.waypoint_client.call_async(request)
        completed = threading.Event()
        future.add_done_callback(lambda _future: completed.set())
        if not completed.wait(timeout=float(config.timing.waypoint_service_timeout_seconds)):
            future.cancel()
            self.log.warning(f'_save_waypoint_at_pose: waypoint service timed out '
                             f'(>{config.timing.waypoint_service_timeout_seconds}s)')
            return None
        try:
            return future.result()
        except Exception as exc:
            self.log.warning(f'_save_waypoint_at_pose: service call failed: {exc}')
            return None

    @staticmethod
    def _pointed_response(response: SavePointedWaypoint.Response, status: int, outcome: str, message: str, ) -> SavePointedWaypoint.Response:
        response.status.status = int(status)
        response.status.message = str(message)
        response.outcome = str(outcome)
        response.message = str(message)
        return response


def main(args=None) -> None:
    configure_logging()
    rclpy.init(args=args)
    node = None
    executor = None
    try:
        node = GestureDetectionNode()
        executor = MultiThreadedExecutor(num_threads=3)
        executor.add_node(node)
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        if executor is not None:
            executor.shutdown()
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()

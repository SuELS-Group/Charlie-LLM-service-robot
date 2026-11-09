import json
import threading
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import rclpy
from geometry_msgs.msg import PointStamped
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.duration import Duration
from rclpy.executors import MultiThreadedExecutor, SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy, qos_profile_sensor_data
from robot_common import get_config, get_logger
from sensor_msgs.msg import CameraInfo, Image
from std_msgs.msg import String
from tf2_geometry_msgs import do_transform_point
from tf2_ros import Buffer, TransformListener
from visualization_msgs.msg import Marker, MarkerArray

config = get_config('depth_mapping_node', Path(__file__).stem)


def stamp_to_sec(stamp):
    return float(stamp.sec) + float(stamp.nanosec) * 1e-9


class DepthMappingNode(Node):

    def __init__(self):
        super().__init__('depth_mapping_node')
        self.log = get_logger()
        self.lock = threading.Lock()
        self.cb_group = ReentrantCallbackGroup()
        self.slop = config.timing.slop_seconds
        self.depth_buffer_len = int(config.timing.depth_buffer_seconds * config.timing.depth_frames_per_second)
        self.target_frame = config.target_frame
        self.tf_buffer = Buffer()
        # Detection callbacks wait for TF. Keep reception on its own executor
        # so those waits cannot starve the callbacks that fill the TF buffer.
        self.tf_node = Node('depth_mapping_tf_listener')
        self.tf_listener = TransformListener(self.tf_buffer, self.tf_node)
        self.cameras = {cam_id: vars(camera_config) for cam_id, camera_config in vars(config.cameras).items()}
        qos_det = QoSProfile(depth=config.qos.depth, reliability=QoSReliabilityPolicy.BEST_EFFORT, history=QoSHistoryPolicy.KEEP_LAST, )
        qos_live_pub = QoSProfile(depth=config.qos.depth, reliability=QoSReliabilityPolicy.BEST_EFFORT, history=QoSHistoryPolicy.KEEP_LAST, )
        qos_pointing = QoSProfile(depth=4, reliability=QoSReliabilityPolicy.RELIABLE, history=QoSHistoryPolicy.KEEP_LAST, )
        self.buffers = {}
        self.pubs = {}
        self.intrinsics = {}
        self.frames = {}
        self.stats = {}
        self._last_tf_warning_times = {}
        self.gripper_offset = np.zeros(3, dtype=np.float64)
        self.gripper_offset_valid = False
        self.gripper_offset_alpha = config.gripper_offset.alpha
        self.gripper_offset_max_norm = config.gripper_offset.max_norm
        self._gripper_map = vars(config.gripper_offset.marker_frames)
        for cam_id, cfg in self.cameras.items():
            self.buffers[cam_id] = {'depth': deque(maxlen=self.depth_buffer_len), 'info': None, }
            self.pubs[cam_id] = {
                'marker': self.create_publisher(MarkerArray, cfg['marker_pub'], qos_live_pub),
                'det3d': self.create_publisher(String, cfg['det3d_pub'], qos_live_pub),
                'door_grip': self.create_publisher(String, cfg['door_grip_pub'], qos_live_pub), }
            self.stats[cam_id] = {
                'received': 0,
                'depth_received': 0,
                'processed': 0,
                'dropped': 0,
                'detections': 0,
                'rec_prev': 0,
                'dep_prev': 0,
                'proc_prev': 0,
                'drop_prev': 0,
                'det_prev': 0, }
            self.create_subscription(String, cfg[
                'obj_sub'], lambda msg, cid=cam_id: self._on_json_detections(cid, msg, 'obj'), qos_det, callback_group=self.cb_group)
            self.create_subscription(String, cfg[
                'aruco_sub'], lambda msg, cid=cam_id: self._on_json_detections(cid, msg, 'aruco'), qos_det, callback_group=self.cb_group)
            self.create_subscription(String, cfg[
                'door_sub'], lambda msg, cid=cam_id: self._on_json_door_detections(cid, msg), qos_det, callback_group=self.cb_group)
            self.create_subscription(Image, cfg[
                'depth_sub'], lambda msg, cid=cam_id: self.on_depth(cid, msg), qos_profile_sensor_data, callback_group=self.cb_group)
            self.create_subscription(CameraInfo, cfg[
                'info_sub'], lambda msg, cid=cam_id: self.on_info(cid, msg), qos_profile_sensor_data, callback_group=self.cb_group)
        self.pointing_depth_pub = self.create_publisher(String, config.pointing_depth.response_topic, qos_pointing)
        self.create_subscription(String, config.pointing_depth.request_topic, self._on_pointing_depth_request, qos_pointing, callback_group=self.cb_group, )
        self.last_status_time = self.get_clock().now()
        self.create_timer(config.timing.status_period_seconds, self.status_callback, callback_group=self.cb_group)
        self.log.info(f'Depth mapping ready for cameras: {", ".join(sorted(self.cameras.keys()))}')

    def status_callback(self):
        now = self.get_clock().now()
        dt = (now - self.last_status_time).nanoseconds / 1e9
        if dt <= 0:
            return
        status_parts = []
        with self.lock:
            for cam_id in sorted(self.cameras.keys()):
                s = self.stats[cam_id]
                rec_delta = s['received'] - s['rec_prev']
                dep_delta = s['depth_received'] - s['dep_prev']
                proc_delta = s['processed'] - s['proc_prev']
                drop_delta = s['dropped'] - s['drop_prev']
                det_delta = s['detections'] - s['det_prev']
                s['rec_prev'] = s['received']
                s['dep_prev'] = s['depth_received']
                s['proc_prev'] = s['processed']
                s['drop_prev'] = s['dropped']
                s['det_prev'] = s['detections']
                rec_fps = rec_delta / dt
                dep_fps = dep_delta / dt
                proc_fps = proc_delta / dt
                avg_hits = int(det_delta / proc_delta) if proc_delta > 0 else 0
                status_parts.append(f'{cam_id}: In {rec_fps:.0f} (Dep {dep_fps:.0f}) Out {proc_fps:.0f} Drop {drop_delta} AvgHits {avg_hits}')
            self.last_status_time = now
        if status_parts:
            self.log.info(' | '.join(status_parts))

    def on_info(self, cam_id, msg):
        with self.lock:
            self.buffers[cam_id]['info'] = msg
            override = self.cameras[cam_id].get('frame_override')
            if override:
                self.frames[cam_id] = override
            else:
                self.frames[cam_id] = msg.header.frame_id
            updated_intrinsics = {
                'fx': msg.k[0],
                'fy': msg.k[4],
                'ppx': msg.k[2],
                'ppy': msg.k[5],
                'width': msg.width,
                'height': msg.height, }
            if self.intrinsics.get(cam_id) != updated_intrinsics:
                previous = self.intrinsics.get(cam_id)
                self.intrinsics[cam_id] = updated_intrinsics
                final_frame = self.frames[cam_id]
                change = 'ready' if previous is None else 'updated'
                self.log.info(f'Camera {cam_id} intrinsics {change}: '
                              f'{msg.width}x{msg.height}, frame '
                              f'{msg.header.frame_id} -> {final_frame}')

    def on_depth(self, cam_id, msg):
        t = stamp_to_sec(msg.header.stamp)
        with self.lock:
            self.stats[cam_id]['depth_received'] += 1
            self.buffers[cam_id]['depth'].append((t, msg))

    def _lookup_target_point(self, frame_id):
        try:
            tfm = self.tf_buffer.lookup_transform(self.target_frame, frame_id, rclpy.time.Time(), timeout=Duration(seconds=0.05))
            tr = tfm.transform.translation
            return np.array([float(tr.x), float(tr.y), float(tr.z)], dtype=np.float64)
        except Exception:
            return None

    @staticmethod
    def _segmentation_bbox(segmentation):
        if segmentation is None:
            return None
        bbox = segmentation.get('bbox') or {}
        width = float(bbox.get('width', 0.0))
        height = float(bbox.get('height', 0.0))
        if width <= 0.0 or height <= 0.0:
            return None
        return bbox

    @staticmethod
    def _median_depth_at(depth_image, px, py, radius=2):
        h, w = depth_image.shape[:2]
        x0 = max(0, int(px) - radius)
        x1 = min(w, int(px) + radius + 1)
        y0 = max(0, int(py) - radius)
        y1 = min(h, int(py) + radius + 1)
        if x1 <= x0 or y1 <= y0:
            return 0
        roi = depth_image[y0:y1, x0:x1]
        valid = roi[roi > 0]
        if valid.size == 0:
            return 0
        return int(np.median(valid))

    @staticmethod
    def _segmentation_camera_point(depth_image, intrinsics, segmentation, min_depth, max_depth):
        points = (segmentation or {}).get('points') or []
        if len(points) < 3:
            return None
        height, width = depth_image.shape[:2]
        polygon = np.rint(np.asarray(points, dtype=np.float32)).astype(np.int32)
        polygon[:, 0] = np.clip(polygon[:, 0], 0, width - 1)
        polygon[:, 1] = np.clip(polygon[:, 1], 0, height - 1)
        mask = np.zeros((height, width), dtype=np.uint8)
        cv2.fillPoly(mask, [polygon], 1)
        ys, xs = np.nonzero((mask > 0) & (depth_image > 0))
        if xs.size == 0:
            return None
        if xs.size > 5000:
            step = max(1, xs.size // 5000)
            xs = xs[::step]
            ys = ys[::step]
        depths = depth_image[ys, xs].astype(np.float64) / 1000.0
        valid = np.isfinite(depths) & (depths >= float(min_depth)) & (depths <= float(max_depth))
        if not np.any(valid):
            return None
        xs = xs[valid].astype(np.float64)
        ys = ys[valid].astype(np.float64)
        depths = depths[valid]
        median_depth = float(np.median(depths))
        depth_band = np.abs(depths - median_depth) <= max(0.15, median_depth * 0.12)
        if np.count_nonzero(depth_band) >= 10:
            xs = xs[depth_band]
            ys = ys[depth_band]
            depths = depths[depth_band]
        x_cam = (xs - float(intrinsics['ppx'])) * depths / float(intrinsics['fx'])
        y_cam = (ys - float(intrinsics['ppy'])) * depths / float(intrinsics['fy'])
        return np.array([float(np.median(x_cam)),
            float(np.median(y_cam)),
            float(np.median(depths)), ], dtype=np.float64)

    def _maybe_update_gripper_offset(self, observed, stamp):
        left = self._lookup_target_point('link_aruco_fingertip_left')
        right = self._lookup_target_point('link_aruco_fingertip_right')
        options = []

        def add_option(m):
            diffs = []
            for k, frame in m.items():
                obs = observed.get(k)
                exp = left if frame.endswith('_left') else right
                if obs is None or exp is None:
                    continue
                diffs.append(exp - obs)
            if not diffs:
                return
            diffs = np.stack(diffs, axis=0)
            off = diffs.mean(axis=0)
            cost = float(np.mean(np.linalg.norm(diffs - off[None, :], axis=1)))
            options.append((cost, off, m))

        add_option({'aruco_200': 'link_aruco_fingertip_left', 'aruco_201': 'link_aruco_fingertip_right'})
        add_option({'aruco_200': 'link_aruco_fingertip_right', 'aruco_201': 'link_aruco_fingertip_left'})
        if not options:
            return
        options.sort(key=lambda x: x[0])
        _, offset, mapping = options[0]
        if float(np.linalg.norm(offset)) > self.gripper_offset_max_norm:
            return
        self._gripper_map = mapping
        if not self.gripper_offset_valid:
            self.gripper_offset = offset
            self.gripper_offset_valid = True
        else:
            a = float(self.gripper_offset_alpha)
            self.gripper_offset = a * offset + (1.0 - a) * self.gripper_offset

    def _on_pointing_depth_request(self, msg):
        try:
            request = json.loads(msg.data)
            request_id = int(request['request_id'])
            request_stamp = float(request['header']['stamp'])
            image_width = int(request['image']['width'])
            image_height = int(request['image']['height'])
            requested_points = request['points']
            if (
                    image_width <= 0 or image_height <= 0 or not isinstance(requested_points, dict) or not requested_points):
                raise ValueError
        except (json.JSONDecodeError, KeyError, TypeError, ValueError,):
            return

        camera_id = str(config.pointing_depth.camera)
        with self.lock:
            camera_buffer = self.buffers.get(camera_id)
            intrinsics = self.intrinsics.get(camera_id)
            frame_id = self.frames.get(camera_id)
            if camera_buffer is None or not camera_buffer['depth']:
                depth_msg, depth_delta = None, float('inf')
            else:
                depth_msg, depth_delta = self._nearest(
                    camera_buffer['depth'], request_stamp, float(config.pointing_depth.slop_seconds), )
            intrinsics = (dict(intrinsics) if intrinsics is not None else None)

        if depth_msg is None:
            self._publish_pointing_depth_response(request_id, 'depth_not_synchronized', depth_delta=depth_delta)
            return
        if intrinsics is None or not frame_id:
            self._publish_pointing_depth_response(request_id, 'camera_intrinsics_unavailable', depth_delta=depth_delta)
            return

        depth_image = self._decode_pointing_depth(depth_msg)
        if depth_image is None:
            self._publish_pointing_depth_response(request_id, 'unsupported_depth_image', depth_delta=depth_delta)
            return

        target_frame = str(config.pointing_depth.target_frame)
        try:
            transform = self.tf_buffer.lookup_transform(target_frame, frame_id, rclpy.time.Time.from_msg(depth_msg.header.stamp), timeout=Duration(seconds=0.1), )
        except Exception:
            self._publish_pointing_depth_response(request_id, 'transform_unavailable', depth_delta=depth_delta)
            return

        scale_x = depth_image.shape[1] / float(image_width)
        scale_y = depth_image.shape[0] / float(image_height)
        points = {}
        missing = []
        for name, pixel in requested_points.items():
            if name not in ('shoulder', 'elbow', 'wrist', 'index'):
                continue
            try:
                pixel_x = float(pixel['x']) * scale_x
                pixel_y = float(pixel['y']) * scale_y
            except (KeyError, TypeError, ValueError):
                missing.append(name)
                continue
            depth_m = self._sample_pointing_depth(depth_image, pixel_x, pixel_y)
            if depth_m is None:
                missing.append(name)
                continue
            camera_point = PointStamped()
            camera_point.header.frame_id = frame_id
            camera_point.header.stamp = depth_msg.header.stamp
            camera_point.point.x = (pixel_x - float(intrinsics['ppx'])) * depth_m / float(intrinsics['fx'])
            camera_point.point.y = (pixel_y - float(intrinsics['ppy'])) * depth_m / float(intrinsics['fy'])
            camera_point.point.z = depth_m
            try:
                mapped = do_transform_point(camera_point, transform)
            except Exception:
                missing.append(name)
                continue
            points[name] = {'x': float(mapped.point.x), 'y': float(mapped.point.y), 'z': float(mapped.point.z), }

        outcome = 'success' if points else 'no_valid_depth'
        self._publish_pointing_depth_response(request_id, outcome, points=points, missing=missing, depth_delta=depth_delta, depth_stamp=stamp_to_sec(depth_msg.header.stamp), )

    def _publish_pointing_depth_response(self, request_id, outcome, *, points=None, missing=None, depth_delta=None, depth_stamp=None, ):
        response = String()
        response.data = json.dumps({
            'request_id': int(request_id),
            'outcome': str(outcome),
            'frame_id': str(config.pointing_depth.target_frame),
            'depth_stamp': depth_stamp,
            'depth_delta_seconds': (
                None if depth_delta is None or not np.isfinite(depth_delta) else float(depth_delta)),
            'points': points or {},
            'missing': missing or [], }, separators=(',', ':'))
        self.pointing_depth_pub.publish(response)

    @staticmethod
    def _decode_pointing_depth(msg):
        encoding = str(msg.encoding).upper()
        if encoding in ('16UC1', 'MONO16'):
            dtype = np.dtype('>u2' if msg.is_bigendian else '<u2')
        elif encoding == '32FC1':
            dtype = np.dtype('>f4' if msg.is_bigendian else '<f4')
        else:
            return None
        try:
            return np.ndarray(shape=(int(msg.height),
                                     int(msg.width)), dtype=dtype, buffer=msg.data, strides=(int(msg.step),
                                                                                             dtype.itemsize), )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _sample_pointing_depth(depth_image, pixel_x, pixel_y):
        radius = int(config.pointing_depth.sample_radius_pixels)
        center_x = int(round(pixel_x))
        center_y = int(round(pixel_y))
        x0 = max(0, center_x - radius)
        x1 = min(depth_image.shape[1], center_x + radius + 1)
        y0 = max(0, center_y - radius)
        y1 = min(depth_image.shape[0], center_y + radius + 1)
        if x1 <= x0 or y1 <= y0:
            return None
        values = np.asarray(depth_image[y0:y1, x0:x1], dtype=np.float64)
        if np.issubdtype(depth_image.dtype, np.integer):
            values *= 0.001
        valid = values[np.isfinite(values) & (values >= float(config.pointing_depth.min_depth_m)) & (
                    values <= float(config.pointing_depth.max_depth_m))]
        if valid.size == 0:
            return None
        return float(np.percentile(valid, 35.0))

    def _on_json_detections(self, cam_id, msg, source_type):
        try:
            envelope = json.loads(msg.data)
        except (json.JSONDecodeError, AttributeError):
            return
        stamp = envelope.get('header', {}).get('stamp', 0.0)
        detections = envelope.get('detections', [])
        self.process_detections(cam_id, stamp, detections, source_type)

    def _on_json_door_detections(self, cam_id, msg):
        try:
            envelope = json.loads(msg.data)
        except (json.JSONDecodeError, AttributeError):
            return
        stamp = envelope.get('header', {}).get('stamp', 0.0)
        detections = envelope.get('detections', [])
        self.process_door_detections(cam_id, stamp, detections)

    def process_detections(self, cam_id, det_stamp, detections, source_type):
        depth_msg = None
        intrinsics = None
        frame_id = None
        cfg = self.cameras.get(cam_id, {})
        min_depth = float(cfg.get('min_depth_range', 0.05))
        max_depth = float(cfg.get('max_depth_range', 3.5))
        with self.lock:
            if cam_id in self.stats:
                self.stats[cam_id]['received'] += 1
            if not self.buffers[cam_id]['depth']:
                if cam_id in self.stats:
                    self.stats[cam_id]['dropped'] += 1
                return
            if self.intrinsics.get(cam_id) is None:
                if cam_id in self.stats:
                    self.stats[cam_id]['dropped'] += 1
                return
            t_det = float(det_stamp)
            depth_msg, _ = self._nearest(self.buffers[cam_id]['depth'], t_det, self.slop)
            if depth_msg:
                intrinsics = self.intrinsics.get(cam_id).copy()
                frame_id = self.frames.get(cam_id)
        if depth_msg is None or intrinsics is None or not frame_id:
            with self.lock:
                if cam_id in self.stats:
                    self.stats[cam_id]['dropped'] += 1
            return
        try:
            depth_image = np.frombuffer(depth_msg.data, dtype=np.uint16).reshape(depth_msg.height, depth_msg.width)
        except ValueError:
            with self.lock:
                if cam_id in self.stats:
                    self.stats[cam_id]['dropped'] += 1
            return
        try:
            transform = self.tf_buffer.lookup_transform(self.target_frame, frame_id, rclpy.time.Time.from_msg(depth_msg.header.stamp), timeout=Duration(seconds=0.1))
        except Exception as e:
            with self.lock:
                if cam_id in self.stats:
                    self.stats[cam_id]['dropped'] += 1
            now_sec = self.get_clock().now().nanoseconds / 1e9
            last_warning_time = self._last_tf_warning_times.get(cam_id, 0.0)
            if now_sec - last_warning_time >= 2.0:
                self.log.warning(f'TF lookup failed for {cam_id}: {frame_id} -> {self.target_frame}: {e}')
                self._last_tf_warning_times[cam_id] = now_sec
            return
        det_items = []
        observed = {}
        img_w = intrinsics['width']
        img_h = intrinsics['height']
        if cam_id == 'head' and source_type == 'obj' and len(detections) > 0:
            self.log.debug(f'{cam_id} processing {len(detections)} object detections')
        for detection in detections:
            bbox = detection.get('bbox_2d', {})
            cx = float(bbox.get('center_x', 0))
            cy = float(bbox.get('center_y', 0))
            w = float(bbox.get('width', 0))
            h = float(bbox.get('height', 0))
            segmentation = detection.get('segmentation_2d')
            seg_bbox = self._segmentation_bbox(segmentation)
            if seg_bbox is not None:
                w = float(seg_bbox['width'])
                h = float(seg_bbox['height'])
            label = detection.get('label', 'object')
            score = float(detection.get('confidence', 0.0))
            track_id = detection.get('track_id')
            grip_point_px = detection.get('grip_point_px')
            marker_id = detection.get('marker_id')
            if track_id is None:
                track_id = -1
            else:
                track_id = int(track_id)
            description = detection.get('description', '') or ''
            face_name = detection.get('face_name', '') or ''
            face_confidence = detection.get('face_confidence')
            face_distance = detection.get('face_distance', face_confidence)
            if source_type == 'obj':
                self.log.debug(f'{cam_id} received {label} score={score:.2f} track={track_id}')
            x_min = int(max(0, cx - w / 2.0))
            y_min = int(max(0, cy - h / 2.0))
            x_max = int(min(img_w, cx + w / 2.0))
            y_max = int(min(img_h, cy + h / 2.0))
            if x_max <= x_min or y_max <= y_min:
                if source_type == 'obj':
                    self.log.debug(f'{cam_id} skipped {label}: invalid bbox')
                continue
            camera_point = None
            if str(label).lower().startswith('person') and segmentation is not None:
                camera_point = self._segmentation_camera_point(depth_image, intrinsics, segmentation, min_depth, max_depth)
            if camera_point is None:
                roi = depth_image[y_min:y_max, x_min:x_max]
                valid_pixels = roi[roi > 0]
                if valid_pixels.size == 0:
                    if source_type == 'obj':
                        self.log.debug(f'{cam_id} skipped {label}: no depth pixels')
                    continue
                if valid_pixels.size > 1000:
                    z_mm = np.median(valid_pixels[::4])
                else:
                    z_mm = np.median(valid_pixels)
                z_m = float(z_mm) / 1000.0
                camera_point = np.array([(cx - intrinsics['ppx']) * z_m / intrinsics['fx'],
                                         (cy - intrinsics['ppy']) * z_m / intrinsics['fy'],
                    z_m, ], dtype=np.float64)
            else:
                z_m = float(camera_point[2])
            if z_m < min_depth or z_m > max_depth:
                if source_type == 'obj':
                    self.log.debug(f'{cam_id} skipped {label}: depth {z_m:.2f}m outside {min_depth}-{max_depth}m')
                continue
            if source_type == 'obj':
                self.log.debug(f'{cam_id} published {label} at {z_m:.2f}m')
            cam_point = PointStamped()
            cam_point.header.frame_id = frame_id
            cam_point.header.stamp = depth_msg.header.stamp
            cam_point.point.x = float(camera_point[0])
            cam_point.point.y = float(camera_point[1])
            cam_point.point.z = float(camera_point[2])
            try:
                map_point = do_transform_point(cam_point, transform)
            except Exception:
                continue
            p = np.array([map_point.point.x, map_point.point.y, map_point.point.z], dtype=np.float64)
            if cam_id == 'gripper' and source_type == 'aruco' and label in ('aruco_200', 'aruco_201'):
                observed[label] = p
            det_width = float(max(1e-3, w * z_m / intrinsics['fx']))
            det_height = float(max(1e-3, h * z_m / intrinsics['fy']))
            grip_3d_point = None
            if grip_point_px is not None:
                grip_x = int(grip_point_px['x'])
                grip_y = int(grip_point_px['y'])
                if 0 <= grip_x < img_w and 0 <= grip_y < img_h:
                    grip_depth_mm = self._median_depth_at(depth_image, grip_x, grip_y)
                    if grip_depth_mm > 0:
                        grip_z_m = float(grip_depth_mm) / 1000.0
                        if min_depth <= grip_z_m <= max_depth:
                            grip_x_cam = (grip_x - intrinsics['ppx']) * grip_z_m / intrinsics['fx']
                            grip_y_cam = (grip_y - intrinsics['ppy']) * grip_z_m / intrinsics['fy']
                            grip_cam_point = PointStamped()
                            grip_cam_point.header.frame_id = frame_id
                            grip_cam_point.header.stamp = depth_msg.header.stamp
                            grip_cam_point.point.x = float(grip_x_cam)
                            grip_cam_point.point.y = float(grip_y_cam)
                            grip_cam_point.point.z = float(grip_z_m)
                            try:
                                grip_map_point = do_transform_point(grip_cam_point, transform)
                                grip_3d_point = np.array([grip_map_point.point.x,
                                                          grip_map_point.point.y,
                                                          grip_map_point.point.z], dtype=np.float64, )
                            except Exception:
                                grip_3d_point = None
            det_items.append((label,
                              float(score),
                              int(track_id),
                              p,
                              det_width,
                              det_height,
                              z_m,
                              description,
                              face_name,
                              face_confidence,
                              face_distance,
                              bbox,
                              segmentation,
                              grip_3d_point,
                              marker_id,))
        if cam_id == 'gripper' and source_type == 'aruco' and observed:
            remapped = {}
            if 'aruco_200' in observed or 'aruco_201' in observed:
                remapped.update(observed)
            self._maybe_update_gripper_offset(remapped, depth_msg.header.stamp)
        use_offset = (cam_id == 'gripper' and self.gripper_offset_valid)
        offset = self.gripper_offset if use_offset else None
        marker_array = MarkerArray()
        out_detections = []
        now_stamp = stamp_to_sec(depth_msg.header.stamp)
        viz_marker_id = 0
        if source_type == 'aruco':
            viz_marker_id = 10000
        for label, score, track_id, p, det_width, det_height, z_m, description, face_name, face_confidence, face_distance, bbox, segmentation, grip_3d_point, detection_marker_id in det_items:
            if offset is not None:
                p2 = p + offset
            else:
                p2 = p
            grip_dict = None
            if grip_3d_point is not None:
                grip_target = grip_3d_point + offset if offset is not None else grip_3d_point
                grip_dict = {'x': float(grip_target[0]), 'y': float(grip_target[1]), 'z': float(grip_target[2])}
            out_detections.append({
                'label': label,
                'confidence': float(score),
                'track_id': track_id if track_id != -1 else None,
                'marker_id': detection_marker_id,
                'source': source_type,
                'description': description if description else None,
                'face_name': face_name if face_name else None,
                'face_confidence': face_confidence,
                'face_distance': face_distance,
                'bbox_2d': bbox,
                'segmentation_2d': segmentation,
                'position_3d': {'x': float(p2[0]), 'y': float(p2[1]), 'z': float(p2[2])},
                'size_3d': {'x': float(det_width), 'y': float(det_height), 'z': 0.05},
                'grip_point': grip_dict, })
            marker = Marker()
            marker.header.stamp = depth_msg.header.stamp
            marker.header.frame_id = self.target_frame
            marker.ns = f'{cam_id}_{source_type}'
            marker.id = viz_marker_id
            viz_marker_id += 1
            marker.type = Marker.CUBE
            marker.action = Marker.ADD
            marker.pose.position.x = float(p2[0])
            marker.pose.position.y = float(p2[1])
            marker.pose.position.z = float(p2[2])
            marker.pose.orientation = transform.transform.rotation
            marker.scale.x = det_width
            marker.scale.y = det_height
            marker.scale.z = 0.05
            if source_type == 'aruco':
                marker.color.r = 0.0
                marker.color.g = 0.0
                marker.color.b = 1.0
            else:
                marker.color.r = 0.0
                marker.color.g = 1.0
                marker.color.b = 0.0
            marker.color.a = 0.5
            marker.lifetime = Duration(seconds=0.5).to_msg()
            marker_array.markers.append(marker)
            text_marker = Marker()
            text_marker.header.stamp = depth_msg.header.stamp
            text_marker.header.frame_id = self.target_frame
            text_marker.ns = f'{cam_id}_{source_type}_text'
            text_marker.id = viz_marker_id
            viz_marker_id += 1
            text_marker.type = Marker.TEXT_VIEW_FACING
            text_marker.action = Marker.ADD
            text_marker.pose.position.x = float(p2[0])
            text_marker.pose.position.y = float(p2[1])
            text_marker.pose.position.z = float(p2[2] + 0.2)
            text_marker.scale.z = 0.1
            text_marker.text = f'{label} ({z_m:.2f}m)'
            text_marker.color.r = 1.0
            text_marker.color.g = 1.0
            text_marker.color.b = 1.0
            text_marker.color.a = 1.0
            text_marker.lifetime = Duration(seconds=0.5).to_msg()
            marker_array.markers.append(text_marker)
        det3d_msg = String()
        det3d_msg.data = json.dumps({
            'header': {'stamp': now_stamp, 'frame_id': self.target_frame, },
            'source': source_type,
            'detections': out_detections, })
        self.pubs[cam_id]['det3d'].publish(det3d_msg)
        self.pubs[cam_id]['marker'].publish(marker_array)
        with self.lock:
            if cam_id in self.stats:
                self.stats[cam_id]['processed'] += 1
                self.stats[cam_id]['detections'] += len(det_items)

    def process_door_detections(self, cam_id, det_stamp, detections):
        depth_msg = None
        intrinsics = None
        frame_id = None
        cfg = self.cameras.get(cam_id, {})
        min_depth = float(cfg.get('min_depth_range', 0.05))
        max_depth = float(cfg.get('max_depth_range', 3.5))
        with self.lock:
            if not self.buffers[cam_id]['depth']:
                return
            if self.intrinsics.get(cam_id) is None:
                return
            t_door = float(det_stamp)
            depth_msg, _ = self._nearest(self.buffers[cam_id]['depth'], t_door, self.slop)
            if depth_msg:
                intrinsics = self.intrinsics.get(cam_id).copy()
                frame_id = self.frames.get(cam_id)
        if depth_msg is None or intrinsics is None or not frame_id:
            return
        try:
            depth_image = np.frombuffer(depth_msg.data, dtype=np.uint16).reshape(depth_msg.height, depth_msg.width)
        except ValueError:
            return
        try:
            transform = self.tf_buffer.lookup_transform(self.target_frame, frame_id, rclpy.time.Time(), timeout=Duration(seconds=0.1))
        except Exception:
            return
        grip_items = []
        marker_array = MarkerArray()
        marker_id = 20000
        img_w = intrinsics['width']
        img_h = intrinsics['height']
        for detection in detections:
            bbox = detection.get('bbox_2d', {})
            cx = float(bbox.get('center_x', 0))
            cy = float(bbox.get('center_y', 0))
            w = float(bbox.get('width', 0))
            h = float(bbox.get('height', 0))
            grip_px = detection.get('grip_point_px')
            grip_x = int(grip_px['x']) if grip_px else None
            grip_y = int(grip_px['y']) if grip_px else None
            x_min = int(max(0, cx - w / 2.0))
            y_min = int(max(0, cy - h / 2.0))
            x_max = int(min(img_w, cx + w / 2.0))
            y_max = int(min(img_h, cy + h / 2.0))
            if x_max <= x_min or y_max <= y_min:
                continue
            roi = depth_image[y_min:y_max, x_min:x_max]
            valid_pixels = roi[roi > 0]
            if valid_pixels.size == 0:
                continue
            if valid_pixels.size > 1000:
                z_mm = np.median(valid_pixels[::4])
            else:
                z_mm = np.median(valid_pixels)
            z_m = float(z_mm) / 1000.0
            if z_m < min_depth or z_m > max_depth:
                continue
            x_cam = (cx - intrinsics['ppx']) * z_m / intrinsics['fx']
            y_cam = (cy - intrinsics['ppy']) * z_m / intrinsics['fy']
            cam_point = PointStamped()
            cam_point.header.frame_id = frame_id
            cam_point.header.stamp = depth_msg.header.stamp
            cam_point.point.x = float(x_cam)
            cam_point.point.y = float(y_cam)
            cam_point.point.z = float(z_m)
            try:
                map_point = do_transform_point(cam_point, transform)
            except Exception:
                continue
            p = np.array([map_point.point.x, map_point.point.y, map_point.point.z], dtype=np.float64)
            det_width = float(max(1e-3, w * z_m / intrinsics['fx']))
            det_height = float(max(1e-3, h * z_m / intrinsics['fy']))
            grip_3d_point = None
            if grip_x is not None and grip_y is not None:
                if 0 <= grip_x < img_w and 0 <= grip_y < img_h:
                    grip_depth_mm = depth_image[grip_y, grip_x]
                    if grip_depth_mm > 0:
                        grip_z_m = float(grip_depth_mm) / 1000.0
                        if min_depth <= grip_z_m <= max_depth:
                            grip_x_cam = (grip_x - intrinsics['ppx']) * grip_z_m / intrinsics['fx']
                            grip_y_cam = (grip_y - intrinsics['ppy']) * grip_z_m / intrinsics['fy']
                            grip_cam_point = PointStamped()
                            grip_cam_point.header.frame_id = frame_id
                            grip_cam_point.header.stamp = depth_msg.header.stamp
                            grip_cam_point.point.x = float(grip_x_cam)
                            grip_cam_point.point.y = float(grip_y_cam)
                            grip_cam_point.point.z = float(grip_z_m)
                            try:
                                grip_map_point = do_transform_point(grip_cam_point, transform)
                                grip_3d_point = np.array([grip_map_point.point.x,
                                                          grip_map_point.point.y,
                                                          grip_map_point.point.z], dtype=np.float64)
                                grip_marker = Marker()
                                grip_marker.header.stamp = depth_msg.header.stamp
                                grip_marker.header.frame_id = self.target_frame
                                grip_marker.ns = f'{cam_id}_door_grip'
                                grip_marker.id = marker_id
                                marker_id += 1
                                grip_marker.type = Marker.SPHERE
                                grip_marker.action = Marker.ADD
                                grip_marker.pose.position.x = float(grip_3d_point[0])
                                grip_marker.pose.position.y = float(grip_3d_point[1])
                                grip_marker.pose.position.z = float(grip_3d_point[2])
                                grip_marker.scale.x = 0.05
                                grip_marker.scale.y = 0.05
                                grip_marker.scale.z = 0.05
                                grip_marker.color.r = 1.0
                                grip_marker.color.g = 0.0
                                grip_marker.color.b = 0.0
                                grip_marker.color.a = 1.0
                                grip_marker.lifetime = Duration(seconds=0.5).to_msg()
                                marker_array.markers.append(grip_marker)
                            except Exception:
                                pass
            grip_items.append((p, det_width, det_height, z_m, grip_3d_point))
        out_detections = []
        now_stamp = stamp_to_sec(depth_msg.header.stamp)
        for p, det_width, det_height, z_m, grip_3d_point in grip_items:
            grip_dict = None
            if grip_3d_point is not None:
                grip_dict = {'x': float(grip_3d_point[0]), 'y': float(grip_3d_point[1]), 'z': float(grip_3d_point[2]), }
            out_detections.append({
                'label': 'door_handle',
                'confidence': 1.0,
                'track_id': None,
                'description': None,
                'bbox_2d': None,
                'position_3d': {'x': float(p[0]), 'y': float(p[1]), 'z': float(p[2])},
                'size_3d': {'x': float(det_width), 'y': float(det_height), 'z': 0.05},
                'grip_point': grip_dict, })
        door_grip_msg = String()
        door_grip_msg.data = json.dumps({
            'header': {'stamp': now_stamp, 'frame_id': self.target_frame, },
            'detections': out_detections, })
        if marker_array.markers:
            self.pubs[cam_id]['marker'].publish(marker_array)
        self.pubs[cam_id]['door_grip'].publish(door_grip_msg)

    def _nearest(self, dq, t, slop):
        if not dq:
            return None, float('inf')
        best = None
        best_dt = float('inf')
        for i in range(len(dq) - 1, -1, -1):
            ts, msg = dq[i]
            dt = abs(ts - t)
            if dt < best_dt:
                best_dt = dt
                best = msg
            else:
                if best_dt < slop:
                    break
        if best_dt <= slop:
            return best, best_dt
        return None, best_dt


def main(args=None):
    rclpy.init(args=args)
    node = DepthMappingNode()
    executor = MultiThreadedExecutor(num_threads=6)
    executor.add_node(node)
    tf_executor = SingleThreadedExecutor()
    tf_executor.add_node(node.tf_node)
    tf_thread = threading.Thread(target=tf_executor.spin, name='depth-mapping-tf')
    tf_thread.start()
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        tf_executor.shutdown()
        tf_thread.join()
        node.tf_node.destroy_node()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

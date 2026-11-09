import base64
import json
import math
import threading
import time
from collections import deque

import cv2
import numpy as np
import rclpy
from cv_bridge import CvBridge
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CompressedImage, Image
from std_msgs.msg import Bool, String
from tool_status.msg import ToolStatus as RosToolStatus
from vision_interfaces.srv import ForgetFace, IdentifyFaces, LearnFace, VlmQuery
from workstation_common import get_config, get_logger

from vision_node.aruco_processor import ArucoProcessor
from vision_node.door_processor import DoorProcessor
from vision_node.face_processor import FaceProcessor
from vision_node.frame_scheduler import FrameRateLimiter, LatestFrameScheduler
from vision_node.vlm_processor import VlmProcessor
from vision_node.yolo_processor import YoloProcessor

config = get_config('vision_node')
log = get_logger()


class VisionNode(Node):

    def __init__(self):
        super().__init__('vision_node')
        self.log = log
        self.cameras = self._build_cameras()
        qos_sub = QoSProfile(depth=1, reliability=QoSReliabilityPolicy.BEST_EFFORT, history=QoSHistoryPolicy.KEEP_LAST, )
        qos_det_pub = QoSProfile(depth=1, reliability=QoSReliabilityPolicy.BEST_EFFORT, history=QoSHistoryPolicy.KEEP_LAST, )
        qos_img_pub = QoSProfile(depth=1, reliability=QoSReliabilityPolicy.BEST_EFFORT, history=QoSHistoryPolicy.KEEP_LAST, )
        self.bridge = CvBridge()
        self.frame_scheduler = LatestFrameScheduler(self.cameras)
        self.frame_rate_limiter = FrameRateLimiter({
            cam_id: camera['max_processing_fps']
            for cam_id, camera in self.cameras.items()})
        self.det_pubs = {}
        self.aruco_pubs = {}
        self.door_pubs = {}
        self.img_pubs = {}
        self.log.info(f'Initializing vision processors for cameras: {", ".join(sorted(self.cameras))}')
        self.yolo_processor = YoloProcessor(self._yolo_model_path(), self._yolo_device(), self.log)
        self.aruco_processor = ArucoProcessor(self.log)
        self.door_processor = DoorProcessor(self.log)
        for cam_id, camera in self.cameras.items():
            self.yolo_processor.set_model_for_camera(cam_id, camera['target_classes'])
            self.aruco_processor.init_camera(cam_id)
        self.lock = threading.Lock()
        self.stats = {}
        self.last_results = {}
        self.latest_frames = {}
        for cam_id in self.cameras.keys():
            self.latest_frames[cam_id] = None
        self.vlm_processor = VlmProcessor(self.log)
        if not self.vlm_processor.enabled:
            self.vlm_processor = None
        self.face_processor = FaceProcessor(self.log)
        self.vlm_pause_sub = self.create_subscription(Bool, config.topics.pause, self._vlm_pause_callback, 10)
        self.vlm_query_service = self.create_service(VlmQuery, config.services.vlm_query, self._handle_vlm_query)
        self.learn_face_service = self.create_service(LearnFace, config.services.learn_face, self._handle_learn_face)
        self.identify_faces_service = self.create_service(IdentifyFaces, config.services.identify_faces, self._handle_identify_faces)
        self.forget_face_service = self.create_service(ForgetFace, config.services.forget_face, self._handle_forget_face)
        for cam_id in self.cameras.keys():
            self.stats[cam_id] = {
                'received': 0,
                'processed': 0,
                'failed': 0,
                'dropped': 0,
                'throttled': 0,
                'detections': 0,
                'bytes_received': 0,
                'rec_prev': 0,
                'proc_prev': 0,
                'drop_prev': 0,
                'throttle_prev': 0,
                'det_prev': 0,
                'bytes_prev': 0,
                'frame_age_samples': deque(maxlen=300), }
            self.stats[cam_id].update({
                'frame_age_ms': 0.0,
                'source_width': 0,
                'source_height': 0,
                'decode_ms': 0.0,
                'yolo_ms': 0.0,
                'overlay_ms': 0.0,
                'total_ms': 0.0, })
            self.last_results[cam_id] = {'yolo': [], 'aruco': [], 'door': [], }
        for cam_id, cfg in self.cameras.items():
            self.det_pubs[cam_id] = self.create_publisher(String, cfg['det_pub'], qos_det_pub)
            self.aruco_pubs[cam_id] = self.create_publisher(String, cfg['aruco_pub'], qos_det_pub)
            self.door_pubs[cam_id] = self.create_publisher(String, cfg['door_pub'], qos_det_pub)
            self.img_pubs[cam_id] = self.create_publisher(Image, cfg['img_pub'], qos_img_pub)
            self.create_subscription(CompressedImage,
                                     cfg['sub'], lambda msg, cid=cam_id: self.image_callback(cid, msg), qos_sub, )
        self.last_status_time = self.get_clock().now()
        self.status_timer = self.create_timer(float(config.status.period_seconds), self.status_callback)
        self.thread = threading.Thread(target=self.worker, daemon=True)
        self.thread.start()
        if self.vlm_processor is not None:
            self.vlm_processor.start(rclpy.ok)
        self.log.info(f'Vision node ready: vlm_query_service={config.services.vlm_query}, '
                      f'vlm_enabled={self.vlm_processor is not None}')

    @staticmethod
    def _rotation_value(name):
        if not name:
            return None
        return getattr(cv2, str(name), None)

    def _build_cameras(self):
        cameras = {}
        for cam_id, camera in vars(config.cameras).items():
            cameras[cam_id] = {
                'sub': camera.topics.input,
                'det_pub': camera.topics.detections,
                'aruco_pub': camera.topics.aruco,
                'door_pub': camera.topics.door,
                'img_pub': camera.topics.overlay,
                'rotation': self._rotation_value(camera.image.rotation),
                'overlay_max_dimension': int(getattr(camera.image, 'overlay_max_dimension', 0)),
                'max_processing_fps': float(getattr(camera.image, 'max_processing_fps', 0.0)),
                'target_classes': list(camera.classes.target), }
        return cameras

    @staticmethod
    def _yolo_model_path():
        return get_config('vision_node', 'yolo_processor').model.path

    @staticmethod
    def _yolo_device():
        return get_config('vision_node', 'yolo_processor').runtime.device

    def _make_stamp(self, msg_header):
        return float(msg_header.stamp.sec) + float(msg_header.stamp.nanosec) * 1e-9

    def _serialize_detections(self, detections, msg_header):
        envelope = {
            'header': {'stamp': self._make_stamp(msg_header), 'frame_id': msg_header.frame_id, },
            'detections': detections, }
        out = String()
        out.data = json.dumps(envelope)
        return out

    def _handle_vlm_query(self, request, response):
        prompt = request.prompt
        camera = request.camera or 'head'
        if not prompt:
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, 'No prompt provided')
        if self.vlm_processor is None:
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, 'VLM processor is disabled')
        self.log.info(f'VLM query started for camera={camera}')
        image_b64 = self._capture_latest_image(camera)
        if not image_b64:
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, 'Camera capture failed')
        if self.vlm_processor is not None:
            self.vlm_processor.set_paused(True)
        try:
            response_text = self.vlm_processor.query_direct(prompt, image_b64)
            if response_text:
                self.log.info(f'VLM query completed: {response_text}')
                self._set_status(response, RosToolStatus.STATUS_SUCCESS, 'VLM query completed')
                response.response_text = response_text
            else:
                self._set_status(response, RosToolStatus.STATUS_FAILURE, 'VLM query returned empty')
        except Exception as e:
            self.log.error(f'VLM query failed: {e}')
            self._set_status(response, RosToolStatus.STATUS_FAILURE, f'VLM query failed: {e}')
        finally:
            if self.vlm_processor is not None:
                self.vlm_processor.set_paused(False)
        return response

    def _handle_learn_face(self, request, response):
        name = request.name
        self.log.info(f'Learn face requested for {name}')
        camera = 'head'
        img_bgr = None
        with self.lock:
            msg = self.latest_frames.get(camera)
        if msg is not None:
            try:
                np_arr = np.frombuffer(msg.data, np.uint8)
                img_bgr = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                rotation = self.cameras[camera].get('rotation')
                if rotation is not None:
                    img_bgr = cv2.rotate(img_bgr, rotation)
            except Exception as e:
                self.log.error(f'Failed to decode image for learn face: {e}')
        if img_bgr is None:
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, 'Camera capture failed')
        result = self.face_processor.learn_face(img_bgr, name)
        if result['success']:
            response.name = result['name']
            response.sample_count = result.get('sample_count', 1)
            return self._set_status(response, RosToolStatus.STATUS_SUCCESS, result['message'])
        else:
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, result['message'])

    def _handle_identify_faces(self, request, response):
        self.log.info('Identify faces requested')
        camera = 'head'
        image = self._latest_image(camera, rotate=False)
        with self.lock:
            yolo_dets = [dict(det) for det in self.last_results.get(camera, {}).get('yolo', [])]
        person_dets = [det for det in yolo_dets if str(det.get('label', '')).startswith('person')]
        face_results = {}
        if image is not None and person_dets:
            face_results = self.face_processor.identify_frame(camera, image, person_dets)
        faces = []
        known_names = set()
        unknown_count = 0
        for det in person_dets:
            trk_id = det.get('track_id')
            face_info = face_results.get(trk_id) if trk_id is not None else None
            if face_info is not None:
                face_name = self.face_processor.apply_mapping_result(
                    det, face_info)
            else:
                face_name = det.get('face_name') or 'unknown'
            faces.append(face_name)
            if face_name == 'unknown':
                unknown_count += 1
            else:
                known_names.add(face_name)
        if person_dets:
            updated = []
            person_by_track = {det.get('track_id'): det for det in person_dets if det.get('track_id') is not None}
            for det in yolo_dets:
                trk_id = det.get('track_id')
                updated.append(person_by_track.get(trk_id, det))
            with self.lock:
                self.last_results[camera]['yolo'] = updated
        response.faces = faces
        response.known_names = list(known_names)
        response.count = len(person_dets)
        response.unknown_count = unknown_count
        return self._set_status(response, RosToolStatus.STATUS_SUCCESS, 'Identification complete')

    def _handle_forget_face(self, request, response):
        name = (request.name or '').strip()
        self.log.info(f'Forget face requested for {name if name else "<all>"}')
        result = self.face_processor.remove_face(name)
        if result['success']:
            response.name = result.get('name', name)
            return self._set_status(response, RosToolStatus.STATUS_SUCCESS, result['message'])
        else:
            response.name = result.get('name', name)
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, result['message'])

    @staticmethod
    def _set_status(response, status_code: int, message: str):
        response.status.status = status_code
        response.status.message = message
        return response

    def _latest_image(self, camera_id, rotate=True):
        if camera_id not in self.cameras:
            self.log.error(f'Unknown camera requested: {camera_id}')
            return None
        with self.lock:
            msg = self.latest_frames.get(camera_id)
        if msg is None:
            self.log.error(f'No frame available for {camera_id}')
            return None
        try:
            np_arr = np.frombuffer(msg.data, np.uint8)
            image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            if image is None:
                return None
            rotation = self.cameras[camera_id].get('rotation') if rotate else None
            return cv2.rotate(image, rotation) if rotation is not None else image
        except Exception as e:
            self.log.error(f'Failed to decode latest image for {camera_id}: {e}')
            return None

    def _person_crops_from_detections(self, image, detections):
        crops = []
        for det in detections:
            if not str(det.get('label', '')).startswith('person'):
                continue
            trk_id = det.get('track_id')
            if trk_id is None:
                continue
            try:
                bbox = det['bbox_2d']
                crop = self._crop_detection(image, bbox['center_x'], bbox['center_y'], bbox['width'], bbox['height'])
            except (KeyError, ValueError, IndexError):
                crop = None
            if crop is not None:
                crops.append({'track_id': trk_id, 'crop': crop})
        return crops

    def _capture_latest_image(self, camera_id):
        image = self._latest_image(camera_id)
        if image is None:
            return None
        try:
            _, buffer = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
            return base64.b64encode(buffer).decode('utf-8')
        except Exception as e:
            self.log.error(f'Failed to encode image for {camera_id}: {e}')
            return None

    def _vlm_pause_callback(self, msg: Bool):
        if self.vlm_processor is not None:
            self.vlm_processor.set_paused(msg.data)

    def _oriented_rect_corners(self, cx, cy, w, h, theta):
        hw = float(w) * 0.5
        hh = float(h) * 0.5
        ct = math.cos(float(theta))
        st = math.sin(float(theta))
        R = np.array([[ct, -st], [st, ct]], dtype=np.float32)
        local = np.array([[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]], dtype=np.float32)
        pts = (local @ R.T) + np.array([float(cx), float(cy)], dtype=np.float32)
        return pts

    def _transform_points(self, pts, img_w, img_h, rotation):
        pts = np.asarray(pts, dtype=np.float32)
        if rotation is None:
            return pts
        if rotation == cv2.ROTATE_90_CLOCKWISE:
            x = img_h - 1.0 - pts[:, 1]
            y = pts[:, 0]
            return np.stack([x, y], axis=1)
        if rotation == cv2.ROTATE_90_COUNTERCLOCKWISE:
            x = pts[:, 1]
            y = img_w - 1.0 - pts[:, 0]
            return np.stack([x, y], axis=1)
        return pts

    def _get_inverse_rotation(self, rotation):
        if rotation == cv2.ROTATE_90_CLOCKWISE:
            return cv2.ROTATE_90_COUNTERCLOCKWISE
        elif rotation == cv2.ROTATE_90_COUNTERCLOCKWISE:
            return cv2.ROTATE_90_CLOCKWISE
        elif rotation == cv2.ROTATE_180:
            return cv2.ROTATE_180
        return None

    def _rotated_bbox_from_points(self, pts):
        min_xy = pts.min(axis=0)
        max_xy = pts.max(axis=0)
        return {
            'center_x': float((min_xy[0] + max_xy[0]) / 2.0),
            'center_y': float((min_xy[1] + max_xy[1]) / 2.0),
            'width': float(max_xy[0] - min_xy[0]),
            'height': float(max_xy[1] - min_xy[1]), }

    def _rotate_detection_back(self, det, h_rotated, w_rotated, h_original, w_original, inverse_rotation):
        if inverse_rotation is None:
            return det
        bbox = det['bbox_2d']
        cx = bbox['center_x']
        cy = bbox['center_y']
        bw = bbox['width']
        bh = bbox['height']
        theta = bbox.get('theta', 0.0)
        x1 = cx - bw * 0.5
        y1 = cy - bh * 0.5
        x2 = cx + bw * 0.5
        y2 = cy + bh * 0.5
        pts = np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=np.float32)
        tpts = self._transform_points(pts, w_rotated, h_rotated, inverse_rotation)
        min_xy = tpts.min(axis=0)
        max_xy = tpts.max(axis=0)
        new_cx = float((min_xy[0] + max_xy[0]) / 2.0)
        new_cy = float((min_xy[1] + max_xy[1]) / 2.0)
        new_bw = float(max_xy[0] - min_xy[0])
        new_bh = float(max_xy[1] - min_xy[1])
        if inverse_rotation == cv2.ROTATE_90_CLOCKWISE:
            new_theta = float(theta - np.pi / 2.0)
        elif inverse_rotation == cv2.ROTATE_90_COUNTERCLOCKWISE:
            new_theta = float(theta + np.pi / 2.0)
        else:
            new_theta = float(theta)
        rotated_det = dict(det)
        rotated_det['bbox_2d'] = {
            'center_x': new_cx,
            'center_y': new_cy,
            'width': new_bw,
            'height': new_bh,
            'theta': new_theta, }
        if det.get('segmentation_2d') is not None:
            seg_points = det['segmentation_2d'].get('points') or []
            if seg_points:
                seg_pts = np.asarray(seg_points, dtype=np.float32)
                seg_tpts = self._transform_points(seg_pts, w_rotated, h_rotated, inverse_rotation)
                rotated_det['segmentation_2d'] = {
                    'points': [[float(x), float(y)] for x, y in seg_tpts],
                    'bbox': self._rotated_bbox_from_points(seg_tpts), }
        if det.get('grip_point_px') is not None:
            gpx = det['grip_point_px']['x']
            gpy = det['grip_point_px']['y']
            grip_pts = np.array([[float(gpx), float(gpy)]], dtype=np.float32)
            grip_tpts = self._transform_points(grip_pts, w_rotated, h_rotated, inverse_rotation)
            rotated_det['grip_point_px'] = {'x': int(grip_tpts[0, 0]), 'y': int(grip_tpts[0, 1]), }
        return rotated_det

    def _crop_detection(self, img, cx, cy, bw, bh, padding=10):
        h, w = img.shape[:2]
        x1 = int(max(0, cx - bw / 2 - padding))
        y1 = int(max(0, cy - bh / 2 - padding))
        x2 = int(min(w, cx + bw / 2 + padding))
        y2 = int(min(h, cy + bh / 2 + padding))
        if x2 <= x1 or y2 <= y1:
            return None
        return img[y1:y2, x1:x2].copy()

    def image_callback(self, cam_id, msg):
        with self.lock:
            if cam_id in self.stats:
                self.stats[cam_id]['received'] += 1
                self.stats[cam_id]['bytes_received'] += len(msg.data)
            self.latest_frames[cam_id] = msg
        if not self.frame_rate_limiter.accept(cam_id):
            with self.lock:
                self.stats[cam_id]['throttled'] += 1
            return
        if self.frame_scheduler.put(cam_id, msg):
            with self.lock:
                self.stats[cam_id]['dropped'] += 1

    def worker(self):
        while rclpy.ok():
            scheduled = self.frame_scheduler.take(timeout=0.1)
            if scheduled is None:
                continue
            cam_id, msg = scheduled
            processing_started = time.perf_counter()
            try:
                stamp = self._make_stamp(msg.header)
                now_seconds = self.get_clock().now().nanoseconds / 1e9
                frame_age_ms = max(0.0, (now_seconds - stamp) * 1000.0)
                decode_started = time.perf_counter()
                img_original = self.bridge.compressed_imgmsg_to_cv2(msg, desired_encoding='bgr8')
                decode_ms = (time.perf_counter() - decode_started) * 1000.0
                h_original, w_original = img_original.shape[:2]
                rotation = self.cameras[cam_id].get('rotation')
                img_rotated = cv2.rotate(img_original, rotation) if rotation is not None else img_original
                h_rotated, w_rotated = img_rotated.shape[:2]
                frame_id = msg.header.frame_id
                yolo_started = time.perf_counter()
                yolo_dets_rotated, yolo_count = self.yolo_processor.process(cam_id, img_rotated, stamp, frame_id)
                yolo_ms = (time.perf_counter() - yolo_started) * 1000.0
                person_detections = [
                    detection for detection in yolo_dets_rotated
                    if str(detection.get('label', '')).startswith('person')]
                face_results = self.face_processor.process(
                    cam_id, img_rotated, person_detections)
                for det in yolo_dets_rotated:
                    trk_id = det.get('track_id')
                    if trk_id in face_results:
                        self.face_processor.apply_mapping_result(
                            det, face_results[trk_id])
                self.face_processor.cleanup_stale_cache(self._active_yolo_tracks())
                if self.vlm_processor is not None:
                    for det in yolo_dets_rotated:
                        trk_id = det.get('track_id')
                        if trk_id is not None:
                            try:
                                bbox = det['bbox_2d']
                                crop = self._crop_detection(img_rotated,
                                                            bbox['center_x'],
                                                            bbox['center_y'], bbox['width'], bbox['height'], )
                                if crop is not None:
                                    vlm_label = 'person' if det.get('label', '').startswith('person') else det['label']
                                    self.vlm_processor.submit_request(cam_id, trk_id, crop, vlm_label)
                            except (ValueError, IndexError):
                                pass
                aruco_dets_rotated = self.aruco_processor.process(cam_id, img_rotated)
                door_dets_rotated, door_detections_raw = self.door_processor.process(img_rotated)
                inverse_rotation = self._get_inverse_rotation(rotation)
                yolo_dets = []
                for det in yolo_dets_rotated:
                    rotated_det = self._rotate_detection_back(det, h_rotated, w_rotated, h_original, w_original, inverse_rotation)
                    if self.vlm_processor is not None and rotated_det.get('track_id') is not None:
                        try:
                            description = self.vlm_processor.get_cached_description(cam_id, rotated_det['track_id'])
                            if description:
                                rotated_det['description'] = description
                        except (ValueError, IndexError):
                            pass
                    yolo_dets.append(rotated_det)
                aruco_dets = [
                    self._rotate_detection_back(det, h_rotated, w_rotated, h_original, w_original, inverse_rotation) for
                    det in aruco_dets_rotated]
                door_dets = [
                    self._rotate_detection_back(det, h_rotated, w_rotated, h_original, w_original, inverse_rotation) for
                    det in door_dets_rotated]
                with self.lock:
                    self.last_results[cam_id]['yolo'] = yolo_dets
                    self.last_results[cam_id]['aruco'] = aruco_dets
                    self.last_results[cam_id]['door'] = door_detections_raw
                det_pub = self.det_pubs.get(cam_id)
                if det_pub is not None:
                    det_pub.publish(self._serialize_detections(yolo_dets, msg.header))
                aruco_pub = self.aruco_pubs.get(cam_id)
                if aruco_pub is not None:
                    aruco_pub.publish(self._serialize_detections(aruco_dets, msg.header))
                door_pub = self.door_pubs.get(cam_id)
                if door_pub is not None:
                    door_pub.publish(self._serialize_detections(door_dets, msg.header))
                overlay_started = time.perf_counter()
                self._publish_overlay(cam_id, img_rotated, msg.header, yolo_dets_rotated, aruco_dets_rotated, door_detections_raw)
                overlay_ms = (time.perf_counter() - overlay_started) * 1000.0
                total_ms = (time.perf_counter() - processing_started) * 1000.0
                with self.lock:
                    stats = self.stats[cam_id]
                    stats['processed'] += 1
                    stats['detections'] += yolo_count
                    stats['frame_age_ms'] = frame_age_ms
                    stats['frame_age_samples'].append(frame_age_ms)
                    stats['source_width'] = w_original
                    stats['source_height'] = h_original
                    stats['decode_ms'] = decode_ms
                    stats['yolo_ms'] = yolo_ms
                    stats['overlay_ms'] = overlay_ms
                    stats['total_ms'] = total_ms
            except Exception as e:
                with self.lock:
                    self.stats[cam_id]['failed'] += 1
                self.log.exception(f'Vision processing error: {e}')

    def _publish_overlay(self, cam_id, img_rotated, header, yolo_dets, aruco_dets, door_detections):
        img_pub = self.img_pubs.get(cam_id)
        if img_pub is None:
            return
        overlay = img_rotated.copy()
        for det in yolo_dets:
            seg = det.get('segmentation_2d')
            if seg is not None and seg.get('points'):
                pts = np.round(np.asarray(seg['points'], dtype=np.float32)).astype(np.int32).reshape((-1, 1, 2))
                cv2.polylines(overlay, [pts], True, (0, 255, 0), 2)
                bounds = self._rotated_bbox_from_points(pts.reshape(-1, 2))
            else:
                bbox = det['bbox_2d']
                cx = bbox['center_x']
                cy = bbox['center_y']
                bw = bbox['width']
                bh = bbox['height']
                x1 = int(cx - bw * 0.5)
                y1 = int(cy - bh * 0.5)
                x2 = int(cx + bw * 0.5)
                y2 = int(cy + bh * 0.5)
                cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 255, 0), 2)
                bounds = {'center_x': cx, 'center_y': cy, 'width': bw, 'height': bh, }
            label = f'{det["label"]} {det["confidence"]:.2f}'
            if det.get('track_id') is not None:
                label += f' ID:{det["track_id"]}'
            x1 = int(bounds['center_x'] - bounds['width'] * 0.5)
            y2 = int(bounds['center_y'] + bounds['height'] * 0.5)
            text_x = max(0, x1 + 3)
            text_y = max(0, y2 - 5)
            cv2.putText(overlay, label, (text_x, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2, cv2.LINE_AA, )
        for det in aruco_dets:
            bbox = det['bbox_2d']
            cx = bbox['center_x']
            cy = bbox['center_y']
            bw = bbox['width']
            bh = bbox['height']
            theta = bbox.get('theta', 0.0)
            pts = self._oriented_rect_corners(cx, cy, bw, bh, theta)
            ipts = np.round(pts).astype(np.int32).reshape((-1, 1, 2))
            cv2.polylines(overlay, [ipts], True, (255, 0, 0), 2)
            label = det['label']
            min_xy = pts.min(axis=0)
            max_xy = pts.max(axis=0)
            text_x = int(max(0, min_xy[0] + 3))
            text_y = int(max(0, max_xy[1] - 5))
            cv2.putText(overlay, label, (text_x, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 2, cv2.LINE_AA, )
        for door_det in door_detections:
            x, y, bw, bh = door_det['bbox']
            grip = door_det['grip']
            x1 = int(x)
            y1 = int(y)
            x2 = int(x + bw)
            y2 = int(y + bh)
            cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 0, 255), 2)
            cv2.drawMarker(overlay, (int(grip[0]), int(grip[1])), (0, 0, 255), cv2.MARKER_CROSS, 20, 2)
        max_dimension = self.cameras[cam_id].get('overlay_max_dimension', 0)
        height, width = overlay.shape[:2]
        longest = max(height, width)
        if max_dimension > 0 and longest > max_dimension:
            scale = float(max_dimension) / float(longest)
            overlay = cv2.resize(overlay, (max(1, int(width * scale)), max(1, int(height * scale))), interpolation=cv2.INTER_AREA)
        out_msg = self.bridge.cv2_to_imgmsg(overlay, encoding='bgr8')
        out_msg.header = header
        img_pub.publish(out_msg)

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
                proc_delta = s['processed'] - s['proc_prev']
                drop_delta = s['dropped'] - s['drop_prev']
                throttle_delta = s['throttled'] - s['throttle_prev']
                det_delta = s['detections'] - s['det_prev']
                byte_delta = s['bytes_received'] - s['bytes_prev']
                age_samples = list(s['frame_age_samples'])
                s['frame_age_samples'].clear()
                s['rec_prev'] = s['received']
                s['proc_prev'] = s['processed']
                s['drop_prev'] = s['dropped']
                s['throttle_prev'] = s['throttled']
                s['det_prev'] = s['detections']
                s['bytes_prev'] = s['bytes_received']
                rec_fps = rec_delta / dt
                proc_fps = proc_delta / dt
                avg_hits = int(det_delta / proc_delta) if proc_delta > 0 else 0
                mean_kib = byte_delta / rec_delta / 1024.0 if rec_delta > 0 else 0.0
                bandwidth_mib = byte_delta / dt / (1024.0 * 1024.0)
                age_p95_ms = (
                    float(np.percentile(age_samples, 95))
                    if age_samples
                    else s['frame_age_ms']
                )
                resolution = (
                    f'{s["source_width"]}x{s["source_height"]}'
                    if s['source_width'] > 0
                    else 'waiting'
                )
                status_parts.append(
                    f'{cam_id}: I:{rec_fps:.0f} O:{proc_fps:.0f} '
                    f'D:{drop_delta} L:{throttle_delta} E:{s["failed"]} H:{avg_hits} '
                    f'R:{resolution} S:{mean_kib:.0f}KiB B:{bandwidth_mib:.1f}MiB/s '
                    f'A95:{age_p95_ms:.0f}ms Y:{s["yolo_ms"]:.0f}ms '
                    f'T:{s["total_ms"]:.0f}ms')
            self.last_status_time = now
        if self.vlm_processor is not None:
            active_track_ids = set()
            for cam_id in self.cameras.keys():
                for trk_id in self.yolo_processor.track_state.get(cam_id, {}).keys():
                    active_track_ids.add((cam_id, trk_id))
            stale_count = self.vlm_processor.cleanup_stale_entries(active_track_ids)
            if stale_count > 0:
                self.log.debug(f'Cleaned {stale_count} stale VLM cache entries')
        self.face_processor.cleanup_stale_cache(self._active_yolo_tracks())
        face_stats = self.face_processor.get_stats()
        if status_parts:
            if self.vlm_processor is not None and self.vlm_processor.auto:
                cache_size = self.vlm_processor.get_cache_size()
                vlm_stats_copy = self.vlm_processor.get_stats()
                ready_str = '✓' if vlm_stats_copy.get('model_ready', False) else '◌'
                vlm_info = (f'[VLM] {ready_str} Q:{vlm_stats_copy.get("queue_size", 0)} '
                            f'Ok:{vlm_stats_copy["completed"]} Fail:{vlm_stats_copy["failed"]} C:{cache_size}')
            else:
                vlm_info = '[VLM] disabled'
            face_info = (f'[Face] Q:{face_stats.get("queue_size", 0)} '
                         f'Ok:{face_stats.get("completed", 0)} '
                         f'Fail:{face_stats.get("failed", 0)} '
                         f'Limited:{face_stats.get("rate_limited", 0)} '
                         f'Avg:{face_stats.get("processing_avg_ms", 0):.0f}ms '
                         f'Max:{face_stats.get("processing_max_ms", 0):.0f}ms')
            self.log.info(' | '.join(status_parts) + f' | {face_info} | {vlm_info}')

    def _active_yolo_tracks(self):
        active_track_ids = set()
        for cam_id in self.cameras.keys():
            for trk_id in self.yolo_processor.track_state.get(cam_id, {}).keys():
                active_track_ids.add((cam_id, trk_id))
        return active_track_ids


def main(args=None):
    rclpy.init(args=args)
    node = VisionNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.face_processor.stop()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

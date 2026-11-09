from pathlib import Path

import cv2
import numpy as np
from workstation_common import get_config

config = get_config('vision_node', Path(__file__).stem)


class ArucoProcessor:

    def __init__(self, logger):
        self.label_map = {int(marker.id): marker.label for marker in config.markers}
        self.logger = logger.sub('aruco')
        self.aruco_dict = None
        self.aruco_params = None
        self.aruco_detector = None
        self.marker_cache = {}
        self.cache_frames = int(config.cache.frames)
        self._init_detector()

    def init_camera(self, cam_id):
        self.marker_cache[cam_id] = {}

    def _init_detector(self):
        try:
            self.aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_6X6_250)
            self.aruco_params = cv2.aruco.DetectorParameters()
            self.aruco_detector = cv2.aruco.ArucoDetector(self.aruco_dict, self.aruco_params)
        except Exception as exc:
            self.logger.error(f'Failed to initialize ArUco detector: {exc}')
            self.aruco_dict = None
            self.aruco_params = None
            self.aruco_detector = None

    def process(self, cam_id, img):
        detections = []
        if self.aruco_detector is None:
            return detections
        if cam_id not in self.marker_cache:
            self.marker_cache[cam_id] = {}
        cam_cache = self.marker_cache[cam_id]
        try:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            corners, ids, _ = self.aruco_detector.detectMarkers(gray)
            current_detected = set()
            if ids is not None:
                for idx, marker_id in enumerate(ids.flatten()):
                    marker_id = int(marker_id)
                    current_detected.add(marker_id)
                    points = corners[idx].reshape((4, 2)).astype(np.float32)
                    center_x, center_y = points.mean(axis=0)
                    edge_a = points[1] - points[0]
                    edge_b = points[2] - points[1]
                    cam_cache[marker_id] = {
                        'cx': float(center_x),
                        'cy': float(center_y),
                        'bw': float(np.linalg.norm(edge_a)),
                        'bh': float(np.linalg.norm(edge_b)),
                        'theta': float(np.arctan2(edge_a[1], edge_a[0])),
                        'frames_left': self.cache_frames, }
            for marker_id in list(cam_cache):
                if marker_id in current_detected:
                    continue
                cam_cache[marker_id]['frames_left'] -= 1
                if cam_cache[marker_id]['frames_left'] <= 0:
                    del cam_cache[marker_id]
            for marker_id, data in cam_cache.items():
                detections.append({
                    'label': self.label_map.get(marker_id, f'aruco_{marker_id}'),
                    'marker_id': marker_id,
                    'confidence': 1.0,
                    'track_id': None,
                    'description': None,
                    'bbox_2d': {
                        'center_x': data['cx'],
                        'center_y': data['cy'],
                        'width': data['bw'],
                        'height': data['bh'],
                        'theta': data['theta'], },
                    'position_3d': None,
                    'size_3d': None,
                    'grip_point': None, })
        except Exception as exc:
            self.logger.error(f'ArUco detection error: {exc}')
        return detections

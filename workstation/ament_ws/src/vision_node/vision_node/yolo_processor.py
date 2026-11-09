from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO
from workstation_common import get_config

config = get_config('vision_node', Path(__file__).stem)


class YoloProcessor:
    LABEL_ALIASES = {'ball': {'sports ball', }, 'cup': {'wine glass', }, }

    def __init__(self, model_path, device, logger):
        self.model_path = model_path
        self.device = device
        self.logger = logger.sub('yolo')
        self.conf_thres = float(config.thresholds.confidence)
        self.smooth_factor = float(config.tracking.smooth_factor)
        self.detection_log_interval = int(config.tracking.detection_log_interval)
        self.tracker_path = self._resolve_tracker_path(config.tracking.tracker)
        self.frame_count = {}
        self.models = {}
        self.names = None
        self.track_state = {}
        self.class_filters = {}
        self.default_image_size = int(getattr(config.inference, 'image_size', 640))
        per_camera = getattr(config.inference, 'image_size_by_camera', None)
        self.image_sizes = {camera_id: int(value) for camera_id, value in
            vars(per_camera).items()} if per_camera is not None else {}

    @staticmethod
    def _resolve_tracker_path(value):
        raw = Path(str(value)).expanduser()
        if raw.is_absolute() or raw.exists():
            return str(raw)
        package_config = Path(__file__).resolve().parents[1] / 'config' / raw
        if package_config.exists():
            return str(package_config)
        try:
            from ament_index_python.packages import get_package_share_directory

            installed_config = Path(get_package_share_directory('vision_node')) / 'config' / raw
            if installed_config.exists():
                return str(installed_config)
        except Exception:
            pass
        return str(value)

    @classmethod
    def _normalize_label(cls, label):
        raw = str(label or '').strip().lower()
        if not raw:
            return ''
        for canonical, aliases in cls.LABEL_ALIASES.items():
            if raw == canonical or raw in aliases:
                return canonical
        raw_words = set(raw.replace('_', ' ').replace('-', ' ').split())
        for canonical, aliases in cls.LABEL_ALIASES.items():
            if canonical in raw_words:
                return canonical
            for alias in aliases:
                alias_words = set(alias.replace('_', ' ').replace('-', ' ').split())
                if alias_words and alias_words.issubset(raw_words):
                    return canonical
        return raw

    def set_model_for_camera(self, cam_id, target_classes):
        try:
            self.logger.info(f'Loading model on {self.device} for {cam_id}')
            model = YOLO(self.model_path)
            model.to(self.device)
            self.models[cam_id] = model
            if self.names is None:
                self.names = model.names
            class_filter = self._resolve_target_classes(model.names, target_classes)
            self.class_filters[cam_id] = class_filter
            self.track_state[cam_id] = {}
            mapped = ', '.join(str(model.names[class_id]) for class_id in class_filter)
            self.logger.info(f'{cam_id} class filter: {mapped}')
        except Exception as e:
            self.logger.error(f'Failed to initialize {cam_id}: {e}')
            raise

    def _resolve_target_classes(self, model_names, target_classes):
        class_filter = []
        requested_labels = [self._normalize_label(label) for label in target_classes]
        for target_label, requested_label in zip(target_classes, requested_labels):
            matched_ids = []
            for class_id, model_label in model_names.items():
                raw_label = str(model_label).strip().lower()
                canonical_label = self._normalize_label(raw_label)
                if raw_label == requested_label or canonical_label == requested_label:
                    matched_ids.append(int(class_id))
            if not matched_ids:
                self.logger.warning(f'No closed-vocabulary match for {target_label}')
                continue
            for class_id in matched_ids:
                if class_id not in class_filter:
                    class_filter.append(class_id)
            resolved = ', '.join(str(model_names[class_id]) for class_id in matched_ids)
            self.logger.info(f'Resolved {target_label} to {resolved}')
        if not class_filter:
            raise RuntimeError(f'No target classes from {target_classes} matched model vocabulary')
        return class_filter

    def _extract_segmentation(self, polygon):
        if polygon is None:
            return None
        points = np.asarray(polygon, dtype=np.float32)
        if points.ndim != 2 or points.shape[0] < 3:
            return None
        epsilon = max(1.0, 0.003 * cv2.arcLength(points.reshape(-1, 1, 2), True))
        contour = cv2.approxPolyDP(points.reshape(-1, 1, 2), epsilon, True)
        if contour.shape[0] < 3:
            contour = points.reshape(-1, 1, 2)
        contour_points = contour.reshape(-1, 2).astype(np.float32)
        seg_x, seg_y, seg_w, seg_h = cv2.boundingRect(np.round(contour_points).astype(np.int32))
        return {
            'points': [[float(px), float(py)] for px, py in contour_points],
            'bbox': {'x': float(seg_x), 'y': float(seg_y), 'width': float(seg_w), 'height': float(seg_h), }, }

    def process(self, cam_id, img, stamp, frame_id):
        if cam_id not in self.models:
            return [], 0
        if cam_id not in self.frame_count:
            self.frame_count[cam_id] = 0
        self.frame_count[cam_id] += 1
        detections = []
        active_track_ids = set()
        model = self.models[cam_id]
        results = model.track(img, conf=self.conf_thres, classes=self.class_filters.get(cam_id), persist=True, half=bool(config.inference.use_half), verbose=False, tracker=self.tracker_path, rect=True, retina_masks=bool(config.inference.use_retina_masks), imgsz=self.image_sizes.get(cam_id, self.default_image_size), )
        num_dets = 0
        detected_objects = []
        if results:
            r = results[0]
            if r.boxes is not None and r.boxes.xywh is not None:
                xywh = r.boxes.xywh.detach().cpu().numpy()
                confs = r.boxes.conf.detach().cpu().numpy()
                clss = r.boxes.cls.detach().cpu().numpy()
                polygons = r.masks.xy if r.masks is not None else []
                if r.boxes.id is not None:
                    ids = r.boxes.id.detach().cpu().numpy()
                else:
                    ids = [-1] * len(xywh)
                alpha = self.smooth_factor
                for det_idx, ((x, y, bw, bh), conf, cls, trk_id) in enumerate(zip(xywh, confs, clss, ids)):
                    conf = float(conf)
                    if conf < self.conf_thres:
                        continue
                    cls = int(cls)
                    trk_id = int(trk_id)
                    if trk_id != -1:
                        active_track_ids.add(trk_id)
                        prev = self.track_state[cam_id].get(trk_id)
                        if prev is not None:
                            x = alpha * x + (1.0 - alpha) * prev[0]
                            y = alpha * y + (1.0 - alpha) * prev[1]
                            bw = alpha * bw + (1.0 - alpha) * prev[2]
                            bh = alpha * bh + (1.0 - alpha) * prev[3]
                        self.track_state[cam_id][trk_id] = (x, y, bw, bh)
                    segmentation = self._extract_segmentation(polygons[det_idx]) if det_idx < len(polygons) else None
                    label = self._normalize_label(self.names[cls])
                    bbox = {'center_x': float(x), 'center_y': float(y), 'width': float(bw), 'height': float(bh), }
                    detections.append({
                        'label': label,
                        'confidence': conf,
                        'track_id': trk_id if trk_id != -1 else None,
                        'description': None,
                        'bbox_2d': bbox,
                        'segmentation_2d': segmentation,
                        'position_3d': None,
                        'size_3d': None,
                        'grip_point': None, })
                    num_dets += 1
                    detected_objects.append(f'{label}({conf:.2f})')
        self.track_state[cam_id] = {trk_id: state for trk_id, state in self.track_state[cam_id].items() if
                                    trk_id in active_track_ids}
        if cam_id == 'gripper' and num_dets > 0 and self.frame_count[cam_id] % self.detection_log_interval == 0:
            self.logger.info(f'{cam_id} detections: {", ".join(detected_objects)}')
        return detections, num_dets

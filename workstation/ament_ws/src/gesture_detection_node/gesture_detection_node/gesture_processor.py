from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

RIGHT_SHOULDER = 12
RIGHT_ELBOW = 14
RIGHT_WRIST = 16
RIGHT_INDEX = 20


def angle_degrees(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> float:
    """Return the joint angle used by the existing pointing detector."""
    first = a - b
    second = c - b
    denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
    if denominator < 1e-6:
        return 0.0
    cosine = float(np.clip(np.dot(first, second) / denominator, -1.0, 1.0))
    return math.degrees(math.acos(cosine))


def lm_to_xy(landmarks: Any, index: int, width: int, height: int, ) -> np.ndarray:
    """Convert an existing MediaPipe normalized landmark to image pixels."""
    landmark = landmarks[index]
    return np.asarray([landmark.x * width, landmark.y * height], dtype=np.float32)


def lm_visible(landmarks: Any, index: int, threshold: float = 0.5, ) -> bool:
    """Use Task API visibility and presence exactly as the prior detector did."""
    landmark = landmarks[index]
    visibility = float(getattr(landmark, 'visibility', 1.0))
    presence = float(getattr(landmark, 'presence', 1.0))
    return visibility >= threshold and presence >= threshold


def world_forearm_direction(world_landmarks: Any, elbow_index: int = RIGHT_ELBOW, wrist_index: int = RIGHT_WRIST, ) -> np.ndarray | None:
    """Return the normalized MediaPipe world-space right forearm direction."""
    if not world_landmarks:
        return None
    try:
        elbow = world_landmarks[elbow_index]
        wrist = world_landmarks[wrist_index]
        direction = np.asarray([float(wrist.x) - float(elbow.x),
                                float(wrist.y) - float(elbow.y),
                                float(wrist.z) - float(elbow.z), ], dtype=np.float64)
    except (AttributeError, IndexError, TypeError, ValueError):
        return None
    norm = float(np.linalg.norm(direction))
    if not math.isfinite(norm) or norm <= 1e-6:
        return None
    return direction / norm


@dataclass(frozen=True)
class GestureResult:
    name: str
    confidence: float
    reason: str = ''


@dataclass(frozen=True)
class PointingObservation:
    """Right-arm pose result expressed in unrotated color-image pixels."""
    is_pointing: bool
    confidence: float
    pixels: dict[str, tuple[float, float]] = field(default_factory=dict)
    reason: str = ''


class ThumbsUpClassifier:
    """The existing tuned thumbs-up handoff classifier, kept unchanged."""
    WRIST = 0
    THUMB_MCP = 2
    THUMB_TIP = 4
    INDEX_MCP = 5
    INDEX_TIP = 8
    MIDDLE_MCP = 9
    MIDDLE_TIP = 12
    RING_MCP = 13
    RING_TIP = 16
    PINKY_MCP = 17
    PINKY_TIP = 20

    def __init__(self, cfg: Any):
        self.cfg = cfg

    def classify(self, landmarks: Any) -> GestureResult:
        points = np.array([[landmark.x, landmark.y, landmark.z] for landmark in landmarks.landmark], dtype=np.float32, )
        wrist = points[self.WRIST]
        thumb_tip = points[self.THUMB_TIP]
        thumb_mcp = points[self.THUMB_MCP]
        index_mcp = points[self.INDEX_MCP]
        pinky_mcp = points[self.PINKY_MCP]
        palm_width = float(np.linalg.norm(index_mcp[:2] - pinky_mcp[:2]))
        if palm_width < float(self.cfg.min_palm_width):
            return GestureResult('', 0.0, 'hand_too_small')
        thumb_vector = thumb_tip[:2] - thumb_mcp[:2]
        thumb_up = float(thumb_mcp[1] - thumb_tip[1]) / palm_width
        thumb_side = abs(float(thumb_vector[0])) / max(abs(float(thumb_vector[1])), 1e-4)
        thumb_extension = float(np.linalg.norm(thumb_tip[:2] - wrist[:2])) / palm_width
        fold_scores = []
        curled_count = 0
        for mcp_index, tip_index in ((self.INDEX_MCP, self.INDEX_TIP),
                                     (self.MIDDLE_MCP, self.MIDDLE_TIP),
                                     (self.RING_MCP, self.RING_TIP),
                                     (self.PINKY_MCP, self.PINKY_TIP),):
            score = (float(points[tip_index][1] - points[mcp_index][1]) / palm_width)
            fold_scores.append(score)
            if score >= float(self.cfg.folded_finger_max):
                curled_count += 1
        average_fold = float(np.mean(fold_scores))
        wrist_roll = abs(float(index_mcp[1] - pinky_mcp[1])) / palm_width
        reasons = []
        if thumb_extension < float(self.cfg.thumb_extension_min):
            reasons.append('thumb_not_extended')
        if thumb_up < float(self.cfg.thumb_upright_min):
            reasons.append('thumb_not_upright')
        if (
                thumb_side > float(self.cfg.max_thumb_sideways_ratio) and thumb_up < float(self.cfg.thumb_upright_min) * 3.0):
            reasons.append('thumb_sideways')
        if curled_count < int(self.cfg.min_folded_fingers):
            reasons.append('fingers_not_folded')
        if wrist_roll > float(self.cfg.max_wrist_roll_abs):
            reasons.append('wrist_roll')
        confidence = 0.0
        confidence += np.clip((thumb_up - float(self.cfg.thumb_upright_min)) / 0.35, 0.0, 1.0) * 0.35
        confidence += np.clip(thumb_extension / max(float(self.cfg.thumb_extension_min) * 1.8, 1e-4), 0.0, 1.0) * 0.25
        confidence += np.clip(curled_count / 4.0, 0.0, 1.0) * 0.25
        confidence += np.clip((average_fold + 0.08) / 0.22, 0.0, 1.0) * 0.10
        confidence += np.clip(1.0 - thumb_side / max(float(self.cfg.max_thumb_sideways_ratio), 1e-4), 0.0, 1.0) * 0.05
        confidence = float(np.clip(confidence, 0.0, 1.0))
        if reasons or confidence < float(self.cfg.min_confidence):
            return GestureResult('', confidence, ','.join(reasons) or 'low_confidence')
        return GestureResult('thumbs_up', confidence)


class GestureProcessor:
    """Own the MediaPipe models used by the unified workstation node."""
    RIGHT_LANDMARKS = {'shoulder': RIGHT_SHOULDER, 'elbow': RIGHT_ELBOW, 'wrist': RIGHT_WRIST, 'index': RIGHT_INDEX, }

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.rotation_name = str(getattr(cfg.image, 'rotation', '') or '')
        self.classifier = ThumbsUpClassifier(cfg.hand)
        try:
            import mediapipe as mp
            from ament_index_python.packages import (get_package_share_directory, )
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
        except ImportError as exc:
            raise RuntimeError('mediapipe is required for gesture_detection_node') from exc
        self.mp = mp
        self.mp_hands = mp.solutions.hands
        self.mp_drawing = mp.solutions.drawing_utils
        hand_cfg = cfg.mediapipe.hand
        pose_cfg = cfg.mediapipe.pose
        self.hands = self.mp_hands.Hands(static_image_mode=False, max_num_hands=int(hand_cfg.max_num_hands), model_complexity=int(hand_cfg.model_complexity), min_detection_confidence=float(hand_cfg.min_detection_confidence), min_tracking_confidence=float(hand_cfg.min_tracking_confidence), )
        model_path = (f'{get_package_share_directory("gesture_detection_node")}'
                      f'/config/{pose_cfg.model_path}')
        pose_options = vision.PoseLandmarkerOptions(base_options=mp_python.BaseOptions(model_asset_path=model_path), running_mode=vision.RunningMode.VIDEO, num_poses=1, min_pose_detection_confidence=float(pose_cfg.min_detection_confidence), min_pose_presence_confidence=float(pose_cfg.min_presence_confidence), min_tracking_confidence=float(pose_cfg.min_tracking_confidence), )
        self.pose = vision.PoseLandmarker.create_from_options(pose_options)
        self._last_pose_timestamp_ms = 0

    def close(self) -> None:
        self.hands.close()
        self.pose.close()

    def process_hands(self, image_bgr: np.ndarray, *, annotate: bool = False, ) -> tuple[
        GestureResult, np.ndarray | None]:
        """Classify the existing thumbs-up gesture with the existing model."""
        rotated = self._rotate_image(image_bgr)
        rgb = cv2.cvtColor(rotated, cv2.COLOR_BGR2RGB)
        result = self.hands.process(rgb)
        gesture = GestureResult('', 0.0, 'no_hand')
        annotated = rotated.copy() if annotate else None
        if result.multi_hand_landmarks:
            candidates = [self.classifier.classify(hand_landmarks) for hand_landmarks in result.multi_hand_landmarks]
            # The supporting hand is irrelevant to confirmation. A rejected
            # hand must never outvote a valid thumb on the other hand.
            valid = [candidate for candidate in candidates if candidate.name == 'thumbs_up']
            gesture = max(valid or candidates, key=lambda item: item.confidence)
            if annotated is not None:
                for hand_landmarks in result.multi_hand_landmarks:
                    self.mp_drawing.draw_landmarks(annotated, hand_landmarks, self.mp_hands.HAND_CONNECTIONS, )
        return gesture, annotated

    def process_pointing(self, image_bgr: np.ndarray, person_bbox: dict[str, Any], ) -> PointingObservation:
        """
        Evaluate exactly one pose and only its right shoulder/elbow/wrist/index.
        The caller is responsible for the one-person activation gate. Cropping
        to that confirmed person's box gives MediaPipe no second-person choice.
        """
        if image_bgr is None or image_bgr.size == 0:
            return PointingObservation(False, 0.0, reason='empty_image')
        raw_height, raw_width = image_bgr.shape[:2]
        rotated = self._rotate_image(image_bgr)
        crop_bounds = self._person_crop_bounds(person_bbox, raw_width, raw_height)
        if crop_bounds is None:
            return PointingObservation(False, 0.0, reason='invalid_person_box')
        x0, y0, x1, y1 = crop_bounds
        crop = rotated[y0:y1, x0:x1]
        if crop.size == 0:
            return PointingObservation(False, 0.0, reason='empty_person_crop')
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        media_image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
        timestamp_ms = max(self._last_pose_timestamp_ms + 1, int(time.monotonic() * 1000), )
        self._last_pose_timestamp_ms = timestamp_ms
        result = self.pose.detect_for_video(media_image, timestamp_ms)
        if not result.pose_landmarks:
            return PointingObservation(False, 0.0, reason='no_pose')
        landmarks = result.pose_landmarks[0]
        pixels: dict[str, tuple[float, float]] = {}
        visibilities = []
        crop_height, crop_width = crop.shape[:2]
        for name, index in self.RIGHT_LANDMARKS.items():
            landmark = landmarks[index]
            visibility = min(float(getattr(landmark, 'visibility', 1.0)), float(getattr(landmark, 'presence', 1.0)), )
            visibilities.append(visibility)
            landmark_xy = lm_to_xy(landmarks, index, crop_width, crop_height)
            rotated_x = x0 + float(landmark_xy[0])
            rotated_y = y0 + float(landmark_xy[1])
            pixels[name] = self._rotated_to_raw(rotated_x, rotated_y, raw_width, raw_height)
        minimum_visibility = min(visibilities)
        visibility_threshold = float(self.cfg.pointing.min_visibility)
        if (minimum_visibility < visibility_threshold or not all(
            lm_visible(landmarks, index, visibility_threshold) for index in
            (RIGHT_SHOULDER, RIGHT_ELBOW, RIGHT_WRIST, RIGHT_INDEX))):
            return PointingObservation(False, minimum_visibility, pixels, 'right_arm_not_visible')
        angle_points = (result.pose_world_landmarks[0] if result.pose_world_landmarks else landmarks)
        shoulder = self._landmark_vector(angle_points[self.RIGHT_LANDMARKS['shoulder']])
        elbow = self._landmark_vector(angle_points[self.RIGHT_LANDMARKS['elbow']])
        wrist = self._landmark_vector(angle_points[self.RIGHT_LANDMARKS['wrist']])
        arm_angle = angle_degrees(shoulder, elbow, wrist)
        elbow_pixel = np.asarray(pixels['elbow'], dtype=np.float64)
        index_pixel = np.asarray(pixels['index'], dtype=np.float64)
        normalized_forearm = float(np.linalg.norm(index_pixel - elbow_pixel) / max(math.hypot(raw_width, raw_height), 1.0))
        minimum_angle = float(self.cfg.pointing.min_arm_angle_degrees)
        minimum_length = float(self.cfg.pointing.min_forearm_image_length)
        angle_score = np.clip((arm_angle - minimum_angle) / max(180.0 - minimum_angle, 1.0), 0.0, 1.0)
        length_score = np.clip(normalized_forearm / max(minimum_length * 1.8, 1e-6), 0.0, 1.0)
        confidence = float(np.clip(0.45 * minimum_visibility + 0.40 * angle_score + 0.15 * length_score, 0.0, 1.0, ))
        reasons = []
        if arm_angle < minimum_angle:
            reasons.append('right_arm_not_extended')
        if normalized_forearm < minimum_length:
            reasons.append('right_arm_too_small')
        if confidence < float(self.cfg.pointing.min_confidence):
            reasons.append('low_pointing_confidence')
        return PointingObservation(not reasons, confidence, pixels, ','.join(reasons), )

    @staticmethod
    def _landmark_vector(landmark: Any) -> np.ndarray:
        return np.asarray([landmark.x, landmark.y, landmark.z], dtype=np.float64)

    @staticmethod
    def _joint_angle_degrees(shoulder: np.ndarray, elbow: np.ndarray, wrist: np.ndarray, ) -> float:
        return angle_degrees(shoulder, elbow, wrist)

    def _rotate_image(self, image: np.ndarray) -> np.ndarray:
        if not self.rotation_name:
            return image
        rotation = getattr(cv2, self.rotation_name, None)
        return cv2.rotate(image, rotation) if rotation is not None else image

    def _person_crop_bounds(self, bbox: dict[str, Any], raw_width: int, raw_height: int, ) -> tuple[
                                                                                                  int, int, int, int] | None:
        try:
            center_x = float(bbox['center_x'])
            center_y = float(bbox['center_y'])
            width = float(bbox['width'])
            height = float(bbox['height'])
        except (KeyError, TypeError, ValueError):
            return None
        if width <= 1.0 or height <= 1.0:
            return None
        raw_corners = ((center_x - width / 2.0, center_y - height / 2.0),
                       (center_x + width / 2.0, center_y - height / 2.0),
                       (center_x - width / 2.0, center_y + height / 2.0),
                       (center_x + width / 2.0, center_y + height / 2.0),)
        rotated_corners = [self._raw_to_rotated(x, y, raw_width, raw_height) for x, y in raw_corners]
        xs = [point[0] for point in rotated_corners]
        ys = [point[1] for point in rotated_corners]
        rotated_height, rotated_width = ((raw_width, raw_height) if self.rotation_name in ('ROTATE_90_CLOCKWISE',
                                                                                           'ROTATE_90_COUNTERCLOCKWISE') else (
            raw_height,
            raw_width))
        padding = float(self.cfg.pointing.crop_padding_fraction)
        box_width = max(xs) - min(xs)
        box_height = max(ys) - min(ys)
        x0 = max(0, int(math.floor(min(xs) - box_width * padding)))
        y0 = max(0, int(math.floor(min(ys) - box_height * padding)))
        x1 = min(rotated_width, int(math.ceil(max(xs) + box_width * padding)))
        y1 = min(rotated_height, int(math.ceil(max(ys) + box_height * padding)))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        return x0, y0, x1, y1

    def _raw_to_rotated(self, x: float, y: float, width: int, height: int, ) -> tuple[float, float]:
        if self.rotation_name == 'ROTATE_90_CLOCKWISE':
            return height - 1.0 - y, x
        if self.rotation_name == 'ROTATE_90_COUNTERCLOCKWISE':
            return y, width - 1.0 - x
        if self.rotation_name == 'ROTATE_180':
            return width - 1.0 - x, height - 1.0 - y
        return x, y

    def _rotated_to_raw(self, x: float, y: float, width: int, height: int, ) -> tuple[float, float]:
        if self.rotation_name == 'ROTATE_90_CLOCKWISE':
            raw_x, raw_y = y, height - 1.0 - x
        elif self.rotation_name == 'ROTATE_90_COUNTERCLOCKWISE':
            raw_x, raw_y = width - 1.0 - y, x
        elif self.rotation_name == 'ROTATE_180':
            raw_x, raw_y = width - 1.0 - x, height - 1.0 - y
        else:
            raw_x, raw_y = x, y
        return (float(np.clip(raw_x, 0.0, max(width - 1.0, 0.0))), float(np.clip(raw_y, 0.0, max(height - 1.0, 0.0))),)

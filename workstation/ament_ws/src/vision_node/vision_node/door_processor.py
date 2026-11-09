from pathlib import Path

import cv2
import numpy as np
from workstation_common import get_config

config = get_config('vision_node', Path(__file__).stem)


class DoorProcessor:

    def __init__(self, logger):
        self.logger = logger.sub('door')
        self.red_ranges = [(np.array(range_config.lower), np.array(range_config.upper)) for range_config in
                           config.color.red_ranges]
        self.history = []
        self.max_history = int(config.history.max_frames)
        self.detection_threshold = int(config.detection.threshold)
        self.min_area = float(config.contour.min_area)
        self.max_area = float(config.contour.max_area)
        self.min_y_fraction = float(config.region.min_y_fraction)
        self.max_y_fraction = float(config.region.max_y_fraction)
        self.min_solidity = float(config.shape.min_solidity)
        self.min_aspect_ratio = float(config.shape.min_aspect_ratio)
        self.grip_x_fraction = float(config.grip_point.x_fraction)
        self.grip_y_fraction = float(config.grip_point.y_fraction)

    def enhance_image(self, bgr_img):
        lab = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2LAB)
        lightness, channel_a, channel_b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        lightness = clahe.apply(lightness)
        enhanced = cv2.merge((lightness, channel_a, channel_b))
        return cv2.cvtColor(enhanced, cv2.COLOR_LAB2BGR)

    def get_red_mask(self, hsv):
        mask = None
        for lower, upper in self.red_ranges:
            range_mask = cv2.inRange(hsv, lower, upper)
            mask = range_mask if mask is None else cv2.bitwise_or(mask, range_mask)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    def filter_temporal(self):
        if not self.history:
            return []
        stable = []
        last_frame = self.history[-1]
        for current in last_frame:
            matches = 0
            for previous_frame in self.history[:-1]:
                for previous in previous_frame:
                    distance = np.linalg.norm(np.array(current['center']) - np.array(previous['center']))
                    if distance < 40:
                        matches += 1
                        break
            if matches >= self.detection_threshold:
                stable.append(current)
        return stable

    def process(self, img):
        if not config.enabled:
            return [], []
        height, _ = img.shape[:2]
        hsv = cv2.cvtColor(self.enhance_image(img), cv2.COLOR_BGR2HSV)
        contours, _ = cv2.findContours(self.get_red_mask(hsv), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        frame_handles = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if not (self.min_area < area < self.max_area):
                continue
            x, y, width, height_box = cv2.boundingRect(contour)
            if y < height * self.min_y_fraction or y > height * self.max_y_fraction:
                continue
            aspect_ratio = float(width) / height_box
            hull_area = cv2.contourArea(cv2.convexHull(contour))
            solidity = float(area) / hull_area if hull_area > 0 else 0.0
            if solidity < self.min_solidity or aspect_ratio <= self.min_aspect_ratio:
                continue
            frame_handles.append({
                'grip': (x + int(width * self.grip_x_fraction), y + int(height_box * self.grip_y_fraction),),
                'bbox': (x, y, width, height_box),
                'center': (x + width // 2, y + height_box // 2), })
        self.history.append(frame_handles)
        if len(self.history) > self.max_history:
            self.history.pop(0)
        detections = []
        for detection in self.filter_temporal():
            x, y, width, height_box = detection['bbox']
            grip_x, grip_y = detection['grip']
            detections.append({
                'label': 'door_handle',
                'confidence': 1.0,
                'track_id': None,
                'description': None,
                'bbox_2d': {
                    'center_x': x + width / 2.0,
                    'center_y': y + height_box / 2.0,
                    'width': float(width),
                    'height': float(height_box), },
                'grip_point_px': {'x': int(grip_x), 'y': int(grip_y), },
                'position_3d': None,
                'size_3d': None,
                'grip_point': None, })
        return detections, self.filter_temporal()

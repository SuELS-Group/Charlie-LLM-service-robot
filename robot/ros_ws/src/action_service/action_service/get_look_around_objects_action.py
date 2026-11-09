import threading
import time
from pathlib import Path

from robot_common import get_config
from std_msgs.msg import String

from action_service.common.detection import (build_items_result, collect_transformed_detection_records, deduplicate_objects, parse_detection_message, fresh_detection_payload, )
from action_service.common.motion import MotionHelper

config = get_config('action_service', Path(__file__).stem)


class GetLookAroundObjectsAction:

    def __init__(self, node):
        self.node = node
        self.log = node.log.sub('look_around')
        self.scan_detections = []
        self.scan_messages = 0
        self.scan_active = False
        self._scan_lock = threading.Lock()
        self._stop_evt = threading.Event()
        self.motion = MotionHelper(node, stop_evt=self._stop_evt, log_prefix='look_around')
        self._block_detections_until = 0.0

    def stop(self):
        self._stop_evt.set()
        with self._scan_lock:
            self.scan_active = False

    def execute(self, request, response):
        with self._scan_lock:
            self._stop_evt.clear()
            self.scan_detections = []
            self.scan_messages = 0
            self.scan_active = True
            self._block_detections_until = 0.0
        failed = False
        try:
            start = float(config.scan.head.pan_start)
            end = float(config.scan.head.pan_end)
            speed = float(config.scan.head.pan_speed_rad_per_sec)
            angles = self._linspace(start, end, max(2, int(config.scan.head.steps)))
            self.log.info(f'Starting head scan: {start:.2f} -> {end:.2f} rad, '
                          f'tilt={float(config.scan.head.tilt):.2f}, '
                          f'steps={len(angles)}, dwell={config.scan.dwell_seconds:.2f}s')
            self._move_camera_pan(angles[0], duration_sec=abs(float(angles[0])) / speed)
            self.motion.sleep(float(config.scan.dwell_seconds))
            last_angle = angles[0]
            for a in angles[1:]:
                if self._stop_evt.is_set():
                    self.log.info('FindItemsAction: scan aborted by stop')
                    break
                da = abs(float(a) - float(last_angle))
                self._move_camera_pan(a, duration_sec=da / speed)
                last_angle = a
                self.motion.sleep(float(config.scan.dwell_seconds))
            if not self._stop_evt.is_set():
                self._move_camera_pan(0.0, duration_sec=abs(float(last_angle)) / speed)
        except Exception as e:
            failed = True
            self.log.error(f'Look-around scan failed: {e}')
        finally:
            with self._scan_lock:
                self.scan_active = False
        if self._stop_evt.is_set():
            self.log.info('get_look_around_objects: stopped by global stop')
            return self.node.set_stopped(response, 'Look-around scan stopped')
        if failed:
            return self.node.set_failure(response, 'Head scan motion was not confirmed')
        with self._scan_lock:
            raw_detections = list(self.scan_detections)
            message_count = self.scan_messages
        if message_count == 0:
            self.log.warning('get_look_around_objects: no settled head detection messages received')
            return self.node.set_failure(response, 'No head detection frames received during scan')
        deduplicated = deduplicate_objects(raw_detections, distance_threshold=float(config.detection.dedup_distance_m), min_observations=int(config.detection.min_observations), )
        for obj in deduplicated:
            self.log.info(f'Found: {obj["label"]} (class_id={obj["class_id"]}, score={obj["score"]:.2f})')
        self.log.info(f'Scan complete: {message_count} messages, {len(raw_detections)} raw detections, {len(deduplicated)} unique objects')
        return self.node.set_success(response, f'Found {len(deduplicated)} items', build_items_result(deduplicated))

    def handle_detection(self, msg: String):
        if fresh_detection_payload(self.node, msg) is None:
            return
        now = time.time()
        with self._scan_lock:
            if (not self.scan_active) or (now < self._block_detections_until):
                return
        frame_id, _ = parse_detection_message(msg)
        if frame_id is None:
            return
        records = collect_transformed_detection_records(self.node, msg, 'map', timeout_s=config.scan.tf_timeout_seconds, min_score=config.detection.min_score, now=now, )
        with self._scan_lock:
            if self.scan_active and now >= self._block_detections_until:
                self.scan_messages += 1
                if records:
                    self.scan_detections.extend(records)

    def _move_camera_pan(self, pan_angle_rad, duration_sec=1.5):
        with self._scan_lock:
            self._block_detections_until = time.time() + float(duration_sec) + float(config.scan.head.settle_seconds)
        if not self.motion.move_head(pan=float(pan_angle_rad), tilt=float(config.scan.head.tilt),
                                     duration=float(duration_sec), wait=True):
            raise RuntimeError('Head scan movement failed')
        with self._scan_lock:
            self._block_detections_until = time.time() + float(config.scan.head.settle_seconds)

    @staticmethod
    def _linspace(a: float, b: float, n: int):
        if n <= 1:
            return [float(b)]
        step = (b - a) / (n - 1)
        return [float(a + i * step) for i in range(n)]

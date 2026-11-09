import threading
import time
from pathlib import Path

from robot_common import get_config
from std_msgs.msg import String

from action_service.common.detection import (build_detection_record, build_items_result, deduplicate_objects, iter_object_detections, parse_detection_message, person_name_from_label, fresh_detection_payload, )

config = get_config('action_service', Path(__file__).stem)


class GetVisibleObjectsAction:

    def __init__(self, node):
        self.node = node
        self.log = node.log.sub('visible')
        self.snapshot_detections = []
        self.latest_person_records = []
        self.latest_person_stamp = 0.
        self.snapshot_messages = 0
        self.snapshot_active = False
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()

    def stop(self):
        self._stop_evt.set()
        with self._lock:
            self.snapshot_active = False

    def execute(self, request, response):
        with self._lock:
            self._stop_evt.clear()
            self.snapshot_detections = []
            self.latest_person_records = []
            self.latest_person_stamp = 0.
            self.snapshot_messages = 0
            self.snapshot_active = True
        self._wait_for_first_message(float(config.timeouts.first_message_seconds))
        with self._lock:
            received_message = self.snapshot_messages > 0
        if received_message and not self._stop_evt.is_set():
            self._sleep_with_ok(float(config.timeouts.visible_seconds))
        with self._lock:
            stopped = self._stop_evt.is_set()
            self.snapshot_active = False
            raw_detections = list(self.snapshot_detections)
            people = []
            now = self.node.get_clock().now().nanoseconds / 1e9
            if 0 <= now - self.latest_person_stamp <= 1.2:
                people = list(self.latest_person_records)
            message_count = self.snapshot_messages
        if stopped:
            self.log.info('get_visible_objects: stopped by global stop')
            return self.node.set_stopped(response, 'Get visible objects stopped')
        if message_count == 0:
            self.log.warning('get_visible_objects: no head detection messages received')
            return self.node.set_failure(response, 'No head detection frames received')
        deduplicated = deduplicate_objects(raw_detections, distance_threshold=float(config.detection.dedup_distance_m), min_observations=1, )
        # One current tracked frame already separates humans. Object clustering
        # must not merge nearby people or erase their track identities.
        object_count = len(deduplicated)
        deduplicated.extend({**person,'class_id':f'person_{index + object_count}'}
            for index,person in enumerate(people))
        self.log.info(f'Visible items: {message_count} messages, {len(raw_detections) + len(people)} raw, {len(deduplicated)} unique')
        return self.node.set_success(response, f'Found {len(deduplicated)} items', build_items_result(deduplicated))

    def handle_detection(self, msg: String):
        with self._lock:
            if not self.snapshot_active:
                return
        frame_id, detections = parse_detection_message(msg)
        payload = fresh_detection_payload(self.node, msg)
        if frame_id is None or payload is None:
            return
        records = []
        for det, label, score in iter_object_detections(detections, min_score=config.detection.min_score):
            record = build_detection_record(det.get('position_3d'), label, score, description=det.get('description', ''), face_name=det.get('face_name', '') or person_name_from_label(det.get('label', '')), marker_id=det.get('marker_id'), source=det.get('source', ''), )
            if record is not None:
                if label.casefold() == 'person':
                    record['track_id'] = det.get('track_id')
                records.append(record)
        with self._lock:
            if not self.snapshot_active:
                return
            self.snapshot_messages += 1
            self.snapshot_detections.extend(record for record in records if record['class_id'].casefold() != 'person')
            if payload.get('source', 'obj') == 'obj' and float(payload['header']['stamp']) >= self.latest_person_stamp:
                self.latest_person_records = [record for record in records if record['class_id'].casefold() == 'person'
                    and 0.5 <= record['score'] <= 1.]
                self.latest_person_stamp = float(payload['header']['stamp'])

    def _wait_for_first_message(self, seconds: float):
        end = time.monotonic() + float(seconds)
        while time.monotonic() < end and not self._stop_evt.is_set():
            with self._lock:
                if self.snapshot_messages > 0:
                    return
            time.sleep(0.01)

    def _sleep_with_ok(self, seconds: float):
        end = time.monotonic() + float(seconds)
        while time.monotonic() < end and not self._stop_evt.is_set():
            time.sleep(0.01)

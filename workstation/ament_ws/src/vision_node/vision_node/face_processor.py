import json
import math
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from workstation_common import get_config

config = get_config('vision_node', Path(__file__).stem)


def _confirmed_face(face_info, mapping_threshold):
    if not face_info or str(face_info.get('face_name') or '').strip().casefold() in ('', 'unknown'):
        return False
    try:
        distance = float(face_info.get('face_distance', face_info.get('face_confidence')))
        return math.isfinite(distance) and 0 <= distance <= float(mapping_threshold)
    except (TypeError, ValueError):
        return False


def apply_face_mapping_result(detection, face_info, mapping_threshold):
    """Publish a permissive face candidate but map only a strict identity."""
    for key in (
            'face_name', 'face_confidence', 'face_distance',
            'face_candidate_name', 'face_candidate_distance'):
        detection.pop(key, None)

    label = str(detection.get('label') or '')
    if label.casefold().startswith('person (') and label.endswith(')'):
        detection['label'] = 'person'

    candidate_name = str((face_info or {}).get('face_name') or '').strip()
    if not candidate_name or candidate_name.casefold() == 'unknown':
        return 'unknown'

    candidate_distance = (face_info or {}).get(
        'face_distance', (face_info or {}).get('face_confidence'))
    try:
        candidate_distance = float(candidate_distance)
    except (TypeError, ValueError):
        candidate_distance = None

    detection['face_candidate_name'] = candidate_name
    detection['face_candidate_distance'] = candidate_distance
    if not _confirmed_face(face_info, mapping_threshold):
        return 'unknown'

    detection['face_name'] = candidate_name
    detection['face_confidence'] = candidate_distance
    detection['face_distance'] = candidate_distance
    detection['label'] = f'person ({candidate_name})'
    return candidate_name


class FaceProcessor:

    def __init__(self, logger):
        self.logger = logger.sub('face')
        self.db_path = self._resolve_path(config.database.path)
        self.auto_enabled = bool(getattr(config.recognition, 'auto', False))
        self.match_threshold = float(config.recognition.match_threshold)
        self.mapping_threshold = float(config.recognition.mapping_threshold)
        self.min_face_height = int(config.recognition.min_face_height)
        self.detect_model = str(config.recognition.detect_model)
        self.max_input_size = int(getattr(config.recognition, 'max_input_size', 480))
        self.crop_max_input_size = int(getattr(
            config.recognition, 'crop_max_input_size', 0))
        self.max_faces_per_request = int(getattr(config.recognition, 'max_faces_per_request', 3))
        self.learn_detect_model = str(config.learning.detect_model)
        self.learn_max_input_size = int(getattr(config.learning, 'max_input_size', 960))
        self.unknown_retry_seconds = float(config.cache.unknown_retry_seconds)
        self.identity_ttl_seconds = float(getattr(config.cache, 'identity_ttl_seconds', 10.0))
        self.queue_size = int(config.queue.size)
        self.lock = threading.Lock()
        self.known_faces = {}
        self.face_metadata = {}
        self.identity_cache = {}
        self.unknown_cache = {}
        self.pending = set()
        self.request_queue = queue.Queue(maxsize=self.queue_size)
        self.stats = {
            'queued': 0,
            'completed': 0,
            'failed': 0,
            'rate_limited': 0,
            'processing_jobs': 0,
            'processing_total_ms': 0.0,
            'processing_last_ms': 0.0,
            'processing_max_ms': 0.0, }
        self._fr = None
        self._fr_available = None
        self._running = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_db()
        self._thread.start()
        self.logger.info(
            f'Initialized DB={self.db_path} auto={self.auto_enabled} '
            f'match_threshold={self.match_threshold:.3f} '
            f'mapping_threshold={self.mapping_threshold:.3f} '
            f'retry={self.unknown_retry_seconds:.2f}s '
            f'known={list(self.known_faces)}')

    def apply_mapping_result(self, detection, face_info):
        return apply_face_mapping_result(
            detection, face_info, self.mapping_threshold)

    @staticmethod
    def _resolve_path(path):
        raw = Path(str(path)).expanduser()
        if raw.is_absolute():
            return raw
        cwd_candidate = Path.cwd() / raw
        if cwd_candidate.exists() or cwd_candidate.parent.exists():
            return cwd_candidate
        here = Path(__file__).resolve()
        for parent in here.parents:
            candidate = parent / raw
            if candidate.exists() or candidate.parent.exists():
                return candidate
        return Path.home() / '.pg-llm-robot' / raw.name

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def process(self, cam_id, image, person_detections):
        results = {}
        now = time.time()
        active_ids = set()
        needs_identification = False
        for detection in person_detections:
            trk_id = detection.get('track_id')
            if trk_id is None:
                continue
            trk_id = int(trk_id)
            active_ids.add(trk_id)
            cached = self.get_cached_identity(cam_id, trk_id)
            if cached:
                results[trk_id] = cached
            # A permissive greeting candidate is not a confirmed identity.
            # Retry it on fresh images even while its visual track stays alive.
            if (not cached or not _confirmed_face(cached, self.mapping_threshold)) and (
                    now - self.unknown_cache.get((cam_id, trk_id), 0.0) >= self.unknown_retry_seconds):
                needs_identification = True
        self.touch_tracks(cam_id, active_ids, now)
        if self.auto_enabled and needs_identification:
            self._submit_frame(cam_id, image, person_detections, now)
        return results

    def identify_frame(self, cam_id, image, person_detections):
        now = time.time()
        recognized = self._recognize_frame(image, person_detections)
        self._store_frame_results(cam_id, person_detections, recognized, now)
        return recognized

    @staticmethod
    def _largest_crops(person_crops, limit):
        if limit <= 0:
            return []

        def crop_area(item):
            crop = item.get('crop')
            if crop is None or getattr(crop, 'size', 0) == 0:
                return 0
            h, w = crop.shape[:2]
            return int(h) * int(w)

        return sorted(person_crops, key=crop_area, reverse=True)[:limit]

    def get_cached_identity(self, cam_id, trk_id):
        with self.lock:
            entry = self.identity_cache.get((cam_id, int(trk_id)))
            if entry is None:
                return None
            return {
                'face_name': entry['face_name'],
                'face_confidence': entry['face_confidence'],
                'face_distance': entry.get('face_distance', entry['face_confidence']), }

    def touch_tracks(self, cam_id, track_ids, now=None):
        timestamp = time.time() if now is None else float(now)
        with self.lock:
            for trk_id in track_ids:
                entry = self.identity_cache.get((cam_id, int(trk_id)))
                if entry is not None:
                    entry['last_seen'] = timestamp

    def _submit_frame(self, cam_id, image, person_detections, now):
        if image is None or image.size == 0:
            return False
        with self.lock:
            if self._fr_available is False or not self.known_faces:
                return False
            if cam_id in self.pending:
                self.stats['rate_limited'] += 1
                return False
            self.pending.add(cam_id)
        try:
            detections = [self._copy_person_geometry(item) for item in person_detections if item.get('track_id') is not None]
            self.request_queue.put_nowait({
                'cam_id': cam_id,
                'image': image.copy(),
                'detections': detections,
                'queued_at': now, })
            with self.lock:
                self.stats['queued'] += 1
            return True
        except queue.Full:
            with self.lock:
                self.pending.discard(cam_id)
                self.stats['failed'] += 1
            return False

    @staticmethod
    def _copy_person_geometry(detection):
        return {
            'track_id': int(detection['track_id']),
            'bbox_2d': dict(detection.get('bbox_2d') or {}),
            'segmentation_2d': {
                'points': [list(point) for point in (detection.get('segmentation_2d') or {}).get('points', [])], }, }

    def _worker(self):
        while self._running:
            try:
                request = self.request_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            cam_id = request['cam_id']
            processing_started = time.perf_counter()
            try:
                results = self._recognize_frame(request['image'], request['detections'])
            except Exception as exc:
                self.logger.debug(f'Recognition failed for {cam_id}: {exc}')
                results = None
            processing_ms = (time.perf_counter() - processing_started) * 1000.0
            with self.lock:
                self.pending.discard(cam_id)
                self.stats['processing_jobs'] += 1
                self.stats['processing_total_ms'] += processing_ms
                self.stats['processing_last_ms'] = processing_ms
                self.stats['processing_max_ms'] = max(
                    self.stats['processing_max_ms'], processing_ms)
                if results is None:
                    self.stats['failed'] += 1
                    continue
            self._store_frame_results(cam_id, request['detections'], results, time.time())
            with self.lock:
                self.stats['completed'] += 1

    def _store_frame_results(self, cam_id, person_detections, results, now):
        track_ids = {int(item['track_id']) for item in person_detections if item.get('track_id') is not None}
        named_results = {
            int(track_id): result for track_id, result in results.items()
            if result.get('face_name') and result.get('face_name') != 'unknown'}
        with self.lock:
            for track_id in track_ids:
                key = (cam_id, track_id)
                result = named_results.get(track_id)
                if result is None:
                    self.unknown_cache[key] = now
                    continue
                normalized_name = self._normalize_name(result['face_name'])
                confirmed = _confirmed_face(result, self.mapping_threshold)
                duplicate_keys = [
                    cached_key for cached_key, cached in self.identity_cache.items()
                    if confirmed and cached_key[0] == cam_id and cached_key != key
                    and self._normalize_name(cached.get('face_name')) == normalized_name]
                for duplicate_key in duplicate_keys:
                    del self.identity_cache[duplicate_key]
                stored = dict(result)
                stored['last_seen'] = now
                self.identity_cache[key] = stored
                if confirmed:
                    self.unknown_cache.pop(key, None)
                else:
                    self.unknown_cache[key] = now

    @staticmethod
    def _normalize_name(name):
        return ' '.join(str(name or '').split()).casefold()

    def _recognize_frame(self, image, person_detections):
        if image is None or getattr(image, 'size', 0) == 0 or not person_detections:
            return {}
        fr = self._get_fr()
        if fr is None:
            return {}
        face_results = []
        try:
            image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            detect_rgb, scale = self._resize_for_detection(image_rgb, self.max_input_size)
            locations = fr.face_locations(detect_rgb, model=self.detect_model)
        except Exception as exc:
            self.logger.debug(f'Face detection error: {exc}')
            locations = []
            scale = 1.0
        locations = [
            location for location in locations
            if (location[2] - location[0]) / scale >= self.min_face_height]
        locations = sorted(locations, key=self._face_area, reverse=True)[:self.max_faces_per_request]
        if locations:
            try:
                encodings = fr.face_encodings(detect_rgb, locations)
            except Exception as exc:
                self.logger.debug(f'Face encoding error: {exc}')
                encodings = []
            for location, encoding in zip(locations, encodings):
                name, distance = self._match_encoding(fr, encoding)
                face_results.append({
                    'location': location,
                    'face_name': name,
                    'face_confidence': round(float(distance), 3),
                    'face_distance': round(float(distance), 3), })
        assignments = self._assign_faces_to_people(face_results, person_detections, scale)

        # The full frame is bounded for throughput. A distant face can become
        # too small after that resize even though its YOLO person crop still
        # contains enough native pixels. Retry only unmatched people as a
        # bounded number of native-resolution crops.
        matched_track_ids = {
            track_id for track_id, result in assignments.items()
            if result.get('face_name') not in ('', 'unknown', None)}
        person_crops = self._person_crops(
            image, person_detections, exclude_track_ids=matched_track_ids)
        for item in self._largest_crops(
                person_crops, self.max_faces_per_request):
            result = self._recognize_crop(item['crop'])
            if not result or result.get('face_name') in ('', 'unknown', None):
                continue
            location = result.pop('location', None)
            if location is None:
                continue
            top, right, bottom, left = location
            center_x = item['left'] + (left + right) * 0.5
            center_y = item['top'] + (top + bottom) * 0.5
            owners = self._person_tracks_containing_point(
                person_detections, center_x, center_y)
            if owners != {item['track_id']}:
                self.logger.debug(
                    'Skipping ambiguous cropped face assignment for '
                    f'track {item["track_id"]}: candidate owners={owners}')
                continue
            assignments[item['track_id']] = result

        winners = {}
        for track_id, result in assignments.items():
            name = result.get('face_name', 'unknown')
            if name == 'unknown':
                continue
            normalized = self._normalize_name(name)
            current = winners.get(normalized)
            if current is None or result['face_distance'] < current[1]['face_distance']:
                winners[normalized] = (track_id, result)
        return {track_id: result for track_id, result in winners.values()}

    @staticmethod
    def _person_crops(image, person_detections, exclude_track_ids=None):
        if image is None or getattr(image, 'size', 0) == 0:
            return []
        excluded = {
            int(track_id) for track_id in (exclude_track_ids or set())}
        image_height, image_width = image.shape[:2]
        crops = []
        for person in person_detections:
            track_id = person.get('track_id')
            bbox = person.get('bbox_2d') or {}
            try:
                track_id = int(track_id)
                width = float(bbox['width'])
                height = float(bbox['height'])
                center_x = float(bbox['center_x'])
                center_y = float(bbox['center_y'])
            except (KeyError, TypeError, ValueError):
                continue
            if track_id in excluded or width <= 0.0 or height <= 0.0:
                continue
            left = max(0, int(np.floor(center_x - width * 0.5)))
            right = min(
                image_width, int(np.ceil(center_x + width * 0.5)))
            top = max(0, int(np.floor(center_y - height * 0.5)))
            bottom = min(
                image_height, int(np.ceil(center_y + height * 0.5)))
            if right <= left or bottom <= top:
                continue
            crops.append({
                'track_id': track_id,
                'crop': image[top:bottom, left:right],
                'left': left,
                'top': top,
            })
        return crops

    @staticmethod
    def _person_tracks_containing_point(
            person_detections, point_x, point_y):
        owners = set()
        for person in person_detections:
            track_id = person.get('track_id')
            bbox = person.get('bbox_2d') or {}
            try:
                track_id = int(track_id)
                width = float(bbox['width'])
                height = float(bbox['height'])
                center_x = float(bbox['center_x'])
                center_y = float(bbox['center_y'])
            except (KeyError, TypeError, ValueError):
                continue
            if (width > 0.0 and height > 0.0
                    and abs(float(point_x) - center_x) <= width * 0.5
                    and abs(float(point_y) - center_y) <= height * 0.5):
                owners.add(track_id)
        return owners

    @classmethod
    def _assign_faces_to_people(cls, face_results, person_detections, scale):
        candidates = []
        for face_index, face in enumerate(face_results):
            top, right, bottom, left = face['location']
            center_x = (left + right) * 0.5 / scale
            center_y = (top + bottom) * 0.5 / scale
            for person_index, person in enumerate(person_detections):
                track_id = person.get('track_id')
                bbox = person.get('bbox_2d') or {}
                width = float(bbox.get('width', 0.0))
                height = float(bbox.get('height', 0.0))
                if track_id is None or width <= 0.0 or height <= 0.0:
                    continue
                bbox_cx = float(bbox.get('center_x', 0.0))
                bbox_cy = float(bbox.get('center_y', 0.0))
                inside_bbox = (abs(center_x - bbox_cx) <= width * 0.55 and
                               abs(center_y - bbox_cy) <= height * 0.55)
                if not inside_bbox:
                    continue
                points = (person.get('segmentation_2d') or {}).get('points') or []
                inside_mask = False
                if len(points) >= 3:
                    polygon = np.asarray(points, dtype=np.float32)
                    inside_mask = cv2.pointPolygonTest(polygon, (float(center_x), float(center_y)), False) >= 0
                normalized_distance = ((center_x - bbox_cx) / width) ** 2 + ((center_y - bbox_cy) / height) ** 2
                containment_penalty = 0.0 if inside_mask else 1.0
                candidates.append((containment_penalty + normalized_distance,
                                   face_index, person_index, int(track_id)))
        assignments = {}
        used_faces = set()
        used_people = set()
        for _, face_index, person_index, track_id in sorted(candidates):
            if face_index in used_faces or person_index in used_people:
                continue
            assignments[track_id] = {
                key: value for key, value in face_results[face_index].items()
                if key != 'location'}
            used_faces.add(face_index)
            used_people.add(person_index)
        return assignments

    def _recognize_crop(self, crop):
        if crop is None or getattr(crop, 'size', 0) == 0:
            return None
        fr = self._get_fr()
        if fr is None:
            return None
        try:
            crop_rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
            detect_rgb, scale = self._resize_for_detection(
                crop_rgb, self.crop_max_input_size)
            face_locations = fr.face_locations(detect_rgb, model=self.detect_model)
        except Exception as exc:
            self.logger.debug(f'Face detection error: {exc}')
            return None
        if not face_locations:
            return {
                'face_name': 'unknown',
                'face_confidence': 1.0,
                'face_distance': 1.0,
            }
        largest_face = max(face_locations, key=self._face_area)
        original_face_height = (largest_face[2] - largest_face[0]) / scale
        if original_face_height < self.min_face_height:
            return {
                'face_name': 'unknown',
                'face_confidence': 1.0,
                'face_distance': 1.0,
            }
        try:
            encodings = fr.face_encodings(detect_rgb, [largest_face])
        except Exception as exc:
            self.logger.debug(f'Face encoding error: {exc}')
            return None
        if not encodings:
            return {
                'face_name': 'unknown',
                'face_confidence': 1.0,
                'face_distance': 1.0,
            }
        face_name, face_distance = self._match_encoding(fr, encodings[0])
        return {
            'face_name': face_name,
            'face_confidence': round(float(face_distance), 3),
            'face_distance': round(float(face_distance), 3),
            'location': tuple(
                float(value) / scale for value in largest_face),
        }

    @staticmethod
    def _face_area(face_location):
        top, right, bottom, left = face_location
        return max(0, bottom - top) * max(0, right - left)

    @staticmethod
    def _resize_for_detection(image_rgb, max_size):
        if max_size <= 0:
            return image_rgb, 1.0
        h, w = image_rgb.shape[:2]
        longest = max(h, w)
        if longest <= max_size:
            return image_rgb, 1.0
        scale = float(max_size) / float(longest)
        resized = cv2.resize(image_rgb, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
        return resized, scale

    def _get_fr(self):
        if self._fr_available is False:
            return None
        if self._fr is not None:
            return self._fr
        try:
            import face_recognition as fr

            self._fr = fr
            self._fr_available = True
            self.logger.info('face_recognition loaded')
            return self._fr
        except ImportError as exc:
            self._fr_available = False
            self.logger.error(f'face_recognition unavailable: {exc}')
            return None

    def _match_encoding(self, fr, encoding):
        best_name = 'unknown'
        best_distance = 1.0
        with self.lock:
            faces = {name: list(embeddings) for name, embeddings in self.known_faces.items()}
        for name, embeddings in faces.items():
            if not embeddings:
                continue
            try:
                distance = float(np.min(fr.face_distance(embeddings, encoding)))
            except Exception:
                continue
            if distance < best_distance:
                best_name = name
                best_distance = distance
        if best_distance > self.match_threshold:
            best_name = 'unknown'
        return best_name, best_distance

    def learn_face(self, image_bgr, name, notes=''):
        normalized = (name or '').strip()
        if not normalized:
            return {'success': False, 'message': 'No name provided.', 'name': normalized}
        fr = self._get_fr()
        if fr is None:
            return {'success': False, 'message': 'face_recognition library not available', 'name': normalized}
        try:
            image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
            detect_rgb, _ = self._resize_for_detection(image_rgb, self.learn_max_input_size)
            face_locations = fr.face_locations(detect_rgb, model=self.learn_detect_model)
        except Exception as exc:
            return {'success': False, 'message': f'Face detection failed: {exc}', 'name': normalized}
        if not face_locations:
            return {'success': False, 'message': 'No face detected in the image.', 'name': normalized}
        try:
            encodings = fr.face_encodings(detect_rgb, [max(face_locations, key=self._face_area)])
        except Exception as exc:
            return {'success': False, 'message': f'Encoding failed: {exc}', 'name': normalized}
        if not encodings:
            return {'success': False, 'message': 'Could not compute face embedding.', 'name': normalized}
        with self.lock:
            is_new = normalized not in self.known_faces
            self.known_faces.setdefault(normalized, []).append(encodings[0])
            self.face_metadata[normalized] = notes or self.face_metadata.get(normalized, '')
            sample_count = len(self.known_faces[normalized])
        self._save_db()
        self._clear_cache()
        action = 'Learned' if is_new else 'Updated'
        self.logger.info(f'{action} face for "{normalized}" ({sample_count} sample(s))')
        return {
            'success': True,
            'message': f'{action} face for "{normalized}" successfully ({sample_count} sample(s) stored).',
            'name': normalized,
            'sample_count': sample_count, }

    def remove_face(self, name):
        normalized = (name or '').strip()
        if self._is_forget_all_request(normalized):
            return self._remove_all_faces()
        with self.lock:
            if normalized not in self.known_faces:
                return {'success': False, 'message': f'No face stored for "{normalized}".', 'name': normalized}
            del self.known_faces[normalized]
            self.face_metadata.pop(normalized, None)
        self._save_db()
        self._clear_cache()
        self.logger.info(f'Removed face for "{normalized}"')
        return {'success': True, 'message': f'Removed face for "{normalized}".', 'name': normalized}

    @staticmethod
    def _is_forget_all_request(name):
        token = ' '.join((name or '').strip().lower().replace('_', ' ').split())
        return token in {'', '*', 'all', 'all faces', 'everyone', 'everybody', 'alle', 'alle gesichter'}

    def _remove_all_faces(self):
        with self.lock:
            removed_count = len(self.known_faces)
            self.known_faces = {}
            self.face_metadata = {}
        self._save_db()
        self._clear_cache()
        self.logger.info(f'Removed all known faces ({removed_count})')
        if removed_count == 0:
            message = 'No stored faces were present.'
        elif removed_count == 1:
            message = 'Removed all stored faces (1 person).'
        else:
            message = f'Removed all stored faces ({removed_count} people).'
        return {'success': True, 'message': message, 'name': 'all', 'removed_count': removed_count}

    def list_known_faces(self):
        with self.lock:
            return sorted(self.known_faces)

    def cleanup_stale_cache(self, active_track_ids, now=None):
        timestamp = time.time() if now is None else float(now)
        active_track_ids = set(active_track_ids)
        with self.lock:
            for key in active_track_ids:
                entry = self.identity_cache.get(key)
                if entry is not None:
                    entry['last_seen'] = timestamp
            stale_identity = [
                key for key, entry in self.identity_cache.items()
                if timestamp - float(entry.get('last_seen', timestamp)) > self.identity_ttl_seconds]
            stale_unknown = [
                key for key, last_seen in self.unknown_cache.items()
                if timestamp - float(last_seen) > self.identity_ttl_seconds]
            for key in stale_identity:
                del self.identity_cache[key]
            for key in stale_unknown:
                del self.unknown_cache[key]
        return len(stale_identity) + len(stale_unknown)

    def get_stats(self):
        with self.lock:
            stats = self.stats.copy()
            stats['queue_size'] = self.request_queue.qsize()
            stats['known_faces'] = len(self.known_faces)
            stats['identities'] = len(self.identity_cache)
            jobs = stats.get('processing_jobs', 0)
            stats['processing_avg_ms'] = (
                stats.get('processing_total_ms', 0.0) / jobs if jobs else 0.0)
        return stats

    def _load_db(self):
        if not self.db_path.exists():
            return
        try:
            with self.db_path.open('r', encoding='utf-8') as stream:
                data = json.load(stream)
            loaded = {}
            loaded_meta = {}
            for name, entry in data.get('faces', {}).items():
                embeddings = []
                for embedding in entry.get('embeddings', []):
                    arr = np.array(embedding, dtype=np.float64)
                    if arr.shape == (128,):
                        embeddings.append(arr)
                if embeddings:
                    loaded[name] = embeddings
                    loaded_meta[name] = entry.get('notes', '')
            with self.lock:
                self.known_faces = loaded
                self.face_metadata = loaded_meta
            self.logger.info(f'Loaded {len(loaded)} face(s) from {self.db_path}')
        except Exception as exc:
            self.logger.error(f'Failed to load DB: {exc}')
            with self.lock:
                self.known_faces = {}
                self.face_metadata = {}

    def _save_db(self):
        try:
            with self.lock:
                faces_copy = {name: list(embeddings) for name, embeddings in self.known_faces.items()}
                meta_copy = dict(self.face_metadata)
            faces = {}
            updated_at = datetime.now().isoformat()
            for name, embeddings in faces_copy.items():
                faces[name] = {
                    'embeddings': [embedding.tolist() for embedding in embeddings],
                    'updated_at': updated_at,
                    'notes': meta_copy.get(name, ''), }
            tmp_path = self.db_path.with_suffix(self.db_path.suffix + '.tmp')
            with tmp_path.open('w', encoding='utf-8') as stream:
                json.dump({'version': 1, 'faces': faces}, stream, indent=2)
            tmp_path.replace(self.db_path)
            self.logger.info(f'Saved {len(faces)} face(s) to {self.db_path}')
        except Exception as exc:
            self.logger.error(f'Failed to save DB: {exc}')

    def _clear_cache(self):
        with self.lock:
            self.identity_cache.clear()
            self.unknown_cache.clear()
            self.pending.clear()

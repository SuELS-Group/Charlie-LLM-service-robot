import base64
import queue
import threading
import time
from pathlib import Path

import cv2
import requests
from workstation_common import get_config, log_summary

config = get_config('vision_node', Path(__file__).stem, profile_key='model_profiles')


class VlmProcessor:

    def __init__(self, logger):
        self.logger = logger.sub('vlm')
        self.url = f'{config.endpoint.base_url}/api/generate'
        self.model = config.model.name
        self.model_ctx = int(config.model.context)
        self.timeout = int(config.request.timeout_seconds)
        self.rate_limit = float(config.request.rate_limit_seconds)
        self.keep_alive = config.session.keep_alive
        self.queue_size = int(config.queue.size)
        self.cameras = list(config.cameras)
        self.query_timeout_s = int(config.request.direct_query_timeout_seconds)
        self.lock = threading.Lock()
        self.description_cache = {}
        self.paused = False
        self.pending = set()
        self.last_query_time = {}
        self.request_queue = queue.Queue(maxsize=self.queue_size)
        self.stats = {'queued': 0, 'completed': 0, 'failed': 0, 'rate_limited': 0}
        self._running = False
        self._thread = None
        self._check_running = None
        self.enabled = bool(config.enabled)
        self.auto = bool(config.auto)

    def _payload(self, prompt, image_b64):
        return {
            'model': self.model,
            'prompt': prompt,
            'images': [image_b64],
            'stream': False,
            'keep_alive': self.keep_alive,
            'options': {'num_ctx': self.model_ctx}, }

    def _request_text(self, prompt, image_b64, timeout):
        response = requests.post(self.url, json=self._payload(prompt, image_b64), timeout=timeout)
        if response.status_code != 200:
            self.logger.warning(f'HTTP {response.status_code} from Ollama')
            return None
        return response.json().get('response', '').strip() or None

    def start(self, check_running_fn):
        log_summary(config, self.logger, 'model.name', 'model.context')
        self._running = True
        self._check_running = check_running_fn
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        self.logger.info(f'Worker started with queue_size={self.queue_size} cameras={", ".join(self.cameras)}')

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def set_paused(self, paused):
        with self.lock:
            self.paused = paused
        self.logger.info('Queries paused' if paused else 'Queries resumed')

    def get_cached_description(self, cam_id, trk_id):
        with self.lock:
            return self.description_cache.get((cam_id, trk_id))

    def submit_request(self, cam_id, trk_id, crop, class_name):
        if not bool(config.auto):
            return False
        if cam_id not in self.cameras:
            return False
        cache_key = (cam_id, trk_id)
        current_time = time.time()
        with self.lock:
            if cache_key in self.description_cache or cache_key in self.pending:
                return False
            last_query = self.last_query_time.get(cache_key, 0)
            if current_time - last_query < self.rate_limit:
                self.stats['rate_limited'] += 1
                return False
            self.pending.add(cache_key)
            self.last_query_time[cache_key] = current_time
        try:
            self.request_queue.put_nowait({
                'cam_id': cam_id,
                'trk_id': trk_id,
                'crop': crop.copy(),
                'class_name': class_name})
            with self.lock:
                self.stats['queued'] += 1
            return True
        except queue.Full:
            with self.lock:
                self.pending.discard(cache_key)
            return False

    def get_stats(self):
        with self.lock:
            stats = self.stats.copy()
        stats['queue_size'] = self.request_queue.qsize()
        stats['model_ready'] = True
        return stats

    def get_cache_size(self):
        with self.lock:
            return len(self.description_cache)

    def cleanup_stale_entries(self, active_track_ids):
        with self.lock:
            stale_keys = [key for key in self.description_cache if key not in active_track_ids]
            for key in stale_keys:
                del self.description_cache[key]
            stale_rate_keys = [key for key in self.last_query_time if key not in active_track_ids]
            for key in stale_rate_keys:
                del self.last_query_time[key]
        return len(stale_keys)

    def _worker(self):
        while self._running and self._check_running():
            try:
                request = self.request_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            cache_key = (request['cam_id'], request['trk_id'])
            with self.lock:
                if self.paused:
                    self.pending.discard(cache_key)
                    continue
            start_time = time.time()
            description = self._query_vlm(request['crop'], request['class_name'])
            elapsed = time.time() - start_time
            with self.lock:
                self.pending.discard(cache_key)
                if description:
                    self.description_cache[cache_key] = description
                    self.stats['completed'] += 1
                    track_id = request['trk_id']
                    self.logger.info(f'Track {track_id} described in {elapsed:.1f}s: {description}...')
                else:
                    self.stats['failed'] += 1
                    track_id = request['trk_id']
                    self.logger.warning(f'Track {track_id} failed after {elapsed:.1f}s')

    def _query_vlm(self, crop_img, class_id):
        if crop_img is None or crop_img.size == 0:
            return None
        try:
            _, buffer = cv2.imencode('.jpg', crop_img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            image_b64 = base64.b64encode(buffer).decode('utf-8')
            return self._request_text(self._get_prompt_for_class(class_id), image_b64, self.timeout)
        except requests.Timeout:
            self.logger.warning('Request timed out')
            return None
        except requests.ConnectionError as exc:
            self.logger.warning(f'Connection error: {exc}')
            return None
        except Exception as exc:
            self.logger.warning(f'Query error: {exc}')
            return None

    def query_direct(self, prompt, image_b64):
        try:
            return self._request_text(prompt, image_b64, self.query_timeout_s)
        except requests.Timeout:
            self.logger.warning('Direct query timed out')
            return None
        except Exception as exc:
            self.logger.error(f'Direct query failed: {exc}')
            return None

    @staticmethod
    def _get_prompt_for_class(class_id):
        if class_id == 'person':
            return ('Describe this person in ONE short sentence for robot following. '
                    'Focus ONLY on: shirt/jacket color, pants color, hair color. '
                    'Example: \'Man with blue shirt, black pants, brown hair.\' '
                    'Be brief and specific.')
        if class_id in ['bottle', 'can']:
            return (f'Describe this {class_id} in ONE short sentence for robot grabbing. '
                    'Focus ONLY on: color, label/brand if visible, size (small/medium/large). '
                    'Example: \'Red Coca-Cola can\' or \'Clear plastic water bottle with blue cap\'. '
                    'Be brief and specific.')
        return (f'Describe this {class_id} in ONE short sentence. '
                'Focus on color and distinctive features. Be brief.')

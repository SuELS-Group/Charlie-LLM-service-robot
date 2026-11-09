import threading
import time
from collections import deque


class LatestFrameScheduler:
    """Keep one fresh frame per camera while preserving camera fairness."""

    def __init__(self, camera_ids):
        self._condition = threading.Condition()
        self._frames = {camera_id: None for camera_id in camera_ids}
        self._ready = deque()

    def put(self, camera_id, frame):
        with self._condition:
            replaced = self._frames[camera_id] is not None
            self._frames[camera_id] = frame
            if not replaced:
                self._ready.append(camera_id)
            self._condition.notify()
            return replaced

    def take(self, timeout=None):
        with self._condition:
            if not self._ready:
                self._condition.wait(timeout=timeout)
            if not self._ready:
                return None
            camera_id = self._ready.popleft()
            frame = self._frames[camera_id]
            self._frames[camera_id] = None
            return camera_id, frame


class FrameRateLimiter:
    """Limit scheduled work without slowing reception or accumulating frames."""

    def __init__(self, max_fps_by_camera, clock=None):
        self._max_fps = {camera_id: max(0.0, float(max_fps)) for camera_id, max_fps in max_fps_by_camera.items()}
        self._next_frame_at = {camera_id: 0.0 for camera_id in self._max_fps}
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()

    def accept(self, camera_id):
        max_fps = self._max_fps.get(camera_id, 0.0)
        if max_fps <= 0.0:
            return True
        interval = 1.0 / max_fps
        now = self._clock()
        with self._lock:
            next_frame_at = self._next_frame_at.get(camera_id, 0.0)
            if now < next_frame_at:
                return False
            if now - next_frame_at >= interval:
                self._next_frame_at[camera_id] = now + interval
            else:
                self._next_frame_at[camera_id] = next_frame_at + interval
        return True

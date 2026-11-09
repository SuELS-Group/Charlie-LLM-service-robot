"""Fresh driver evidence for rechecking a completed stop, never a reset command."""
import math
import threading
import time


class DriverState:
    MAX_AGE = 1.5

    def __init__(self):
        self.lock = threading.Lock()
        self.value = None
        self.received = 0.
        self.idle_since = None

    def update(self, value, *, now=None, monotonic=None):
        now = time.time() if now is None else now
        monotonic = time.monotonic() if monotonic is None else monotonic
        if not isinstance(value, dict):
            return False
        for key in ('sampled_at', 'driver_started_at'):
            number = value.get(key)
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                return False
        for key in ('runstop_active', 'arm_service_active', 'streaming_position_active', 'motion_idle'):
            if type(value.get(key)) is not bool:
                return False
        if (not isinstance(value.get('mode'), str) or
                not 0 < value['driver_started_at'] <= value['sampled_at'] or
                not 0 <= now - value['sampled_at'] <= self.MAX_AGE):
            return False
        with self.lock:
            previous = self.value
            if previous and value['sampled_at'] <= previous['sampled_at']:
                return False
            idle = (value['motion_idle'] and not value['arm_service_active'] and
                    not value['streaming_position_active'] and not value['runstop_active'] and
                    value['mode'] in {'position', 'navigation', 'trajectory'})
            continuous = (previous and previous['driver_started_at'] == value['driver_started_at'] and
                          value['sampled_at'] - previous['sampled_at'] <= self.MAX_AGE and
                          monotonic - self.received <= self.MAX_AGE)
            if not idle:
                self.idle_since = None
            elif not continuous or self.idle_since is None:
                self.idle_since = monotonic
            self.value = {key: value[key] for key in ('sampled_at', 'driver_started_at', 'mode',
                'runstop_active', 'arm_service_active', 'streaming_position_active', 'motion_idle')}
            self.received = monotonic
        return True

    def snapshot(self, *, now=None, monotonic=None):
        now = time.time() if now is None else now
        monotonic = time.monotonic() if monotonic is None else monotonic
        with self.lock:
            value = dict(self.value or {})
            fresh = bool(value and 0 <= now - value['sampled_at'] <= self.MAX_AGE and
                         0 <= monotonic - self.received <= self.MAX_AGE)
            value.update(fresh=fresh, stationary_for_seconds=(
                max(0., self.received - self.idle_since) if fresh and self.idle_since is not None else 0.))
            return value

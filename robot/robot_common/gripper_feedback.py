"""Short-lived position/load evidence; never a claim of verified possession."""
from collections import deque
import math
import threading
import time


class GripperFeedback:
    def __init__(self, config=None):
        self.joint = getattr(config, 'joint', 'joint_gripper_finger_left')
        self.max_age = float(getattr(config, 'max_age_seconds', .4))
        self.stable_seconds = float(getattr(config, 'stable_seconds', .4))
        self.position_span = float(getattr(config, 'position_span_rad', .004))
        self.min_load = float(getattr(config, 'min_closing_load_pct', 5.))
        if (not all(math.isfinite(value) for value in (
                self.max_age, self.stable_seconds, self.position_span, self.min_load)) or
                not .1 <= self.max_age <= 1. or not .2 <= self.stable_seconds <= 1. or
                not 0 < self.position_span <= .01 or not 1. <= self.min_load <= 30.):
            raise ValueError('Invalid gripper contact evidence thresholds')
        self.samples = deque(maxlen=64)
        self.lock = threading.Lock()

    def update(self, message):
        try:
            index = list(message.name).index(self.joint)
            stamp = float(message.header.stamp.sec) + float(message.header.stamp.nanosec) * 1e-9
            position, load = float(message.position[index]), float(message.effort[index])
            if not all(math.isfinite(value) for value in (stamp, position, load)) or stamp <= 0:
                return
        except (ValueError, TypeError, IndexError, AttributeError):
            return
        with self.lock:
            if self.samples and stamp <= self.samples[-1][0]:
                return
            self.samples.append((stamp, position, load, time.monotonic()))

    def latest_position(self, now):
        with self.lock:
            sample = self.samples[-1] if self.samples else None
        if sample and 0 <= now - sample[0] <= self.max_age and 0 <= time.monotonic() - sample[3] <= self.max_age:
            return sample[1]
        return None

    def contact_indicated(self, *, now, started_at, target, start_position):
        if start_position is None:
            return False
        with self.lock:
            samples = list(self.samples)
        if not samples:
            return False
        latest = samples[-1]
        if (not 0 <= now - latest[0] <= self.max_age or
                not 0 <= time.monotonic() - latest[3] <= self.max_age):
            return False
        during = [sample for sample in samples if sample[0] >= started_at]
        window = [sample for sample in during if latest[0] - sample[0] <= self.stable_seconds + .15]
        if (len(window) < 3 or window[-1][0] - window[0][0] < self.stable_seconds or
                any(b[0] - a[0] > self.max_age for a, b in zip(window, window[1:]))):
            return False
        # A moving motor can draw a high load. Require both a sustained closing
        # load and a position plateau, away from the requested closed endpoint.
        return (latest[1] > target + .02 and start_position - latest[1] >= .02 and
                max(sample[1] for sample in window) - min(sample[1] for sample in window) <= self.position_span and
                all(-100. <= sample[2] <= -self.min_load for sample in window))

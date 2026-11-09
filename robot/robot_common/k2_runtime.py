"""Atomic base/arm ownership and measured transit-posture admission."""
import math
import threading
import time
import uuid
from collections import deque
from robot_common.gripper_feedback import GripperFeedback


class K2Runtime:
    def __init__(self, config):
        required = {'wrist_extension', 'joint_lift', 'joint_wrist_yaw', 'joint_wrist_pitch', 'joint_wrist_roll'}
        ranges = vars(config.transit_ranges)
        if not required.issubset(ranges):
            raise ValueError('Transit posture must configure every arm/wrist joint')
        if not .1 <= float(config.joint_max_age_seconds) <= 2.:
            raise ValueError('Joint feedback age must be between 0.1 and 2 seconds')
        for name, limits in ranges.items():
            if (not isinstance(limits, (list, tuple)) or len(limits) != 2 or
                    not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in limits) or
                    limits[0] > limits[1]):
                raise ValueError(f'Invalid transit bounds for {name}')
        if not 0 <= ranges['wrist_extension'][0] <= ranges['wrist_extension'][1] <= .05:
            raise ValueError('Transit arm extension cannot exceed 0.05 m')
        self.retracted_encoder_tolerance = float(getattr(config, 'retracted_encoder_tolerance_metres', .001))
        if not math.isfinite(self.retracted_encoder_tolerance) or not 0 <= self.retracted_encoder_tolerance <= .002:
            raise ValueError('Retracted encoder tolerance must be between 0 and 0.002 m')
        self.config = config
        self.lock = threading.RLock()
        self.workflow = ''
        self.lease = ''
        self.released = deque(maxlen=64)
        self.joints = {}
        self.joint_received = 0.
        self.joint_captured = 0.
        self.charger_connected = None
        self.battery_captured = 0.
        self.gripper_feedback = GripperFeedback(getattr(config, 'gripper_contact', None))

    def update_joints(self, message):
        values = {name: float(message.position[i]) for i, name in enumerate(message.name)
                  if i < len(message.position)}
        stamp = message.header.stamp
        with self.lock:
            self.joints = values
            self.joint_received = time.monotonic()
            self.joint_captured = float(stamp.sec) + float(stamp.nanosec) * 1e-9
            self.gripper_feedback.update(message)

    def feedback_error(self, now):
        with self.lock:
            if (not 0 <= now - self.joint_captured <= float(self.config.joint_max_age_seconds) or
                    time.monotonic() - self.joint_received > float(self.config.joint_max_age_seconds)):
                return 'Fresh joint feedback is required'
            if any(name not in self.joints or not math.isfinite(self.joints[name])
                   for name in vars(self.config.transit_ranges)):
                return 'Joint feedback is incomplete or invalid'
        return None

    def update_battery(self, message):
        stamp=float(message.header.stamp.sec)+float(message.header.stamp.nanosec)*1e-9
        with self.lock:
            if not math.isfinite(stamp) or stamp<=self.battery_captured:
                return
            self.battery_captured=stamp
            self.charger_connected=bool(message.present)

    def posture_error(self, now):
        with self.lock:
            error = self.feedback_error(now)
            if error:
                return error
            for name, limits in vars(self.config.transit_ranges).items():
                value = self.joints.get(name)
                lower, upper = limits
                # A fully retracted arm can report a few negative encoder counts.
                # Only tolerate that zero offset; never expand the extension limit.
                if name == 'wrist_extension' and lower == 0:
                    lower -= self.retracted_encoder_tolerance
                if value is None or not math.isfinite(value) or not lower <= value <= upper:
                    return f'Transit posture not confirmed: {name}={value} outside [{lower}, {upper}]'
        return None

    def begin_action(self, token):
        with self.lock:
            if self.workflow or self.lease:
                return False
            self.workflow = token
            return True

    def finish_action(self, token):
        with self.lock:
            if self.workflow == token:
                self.workflow = ''

    def acquire_base(self, token, now):
        with self.lock:
            if self.lease or (self.workflow and self.workflow != token):
                return None, 'Another base or arm operation is active'
            # Charger contact also means contact with the docking station. It
            # is telemetry, not evidence of a cable or a motion interlock.
            error = self.posture_error(now)
            if error:
                return None, error
            self.lease = uuid.uuid4().hex
            return self.lease, None

    def release_base(self, lease):
        with self.lock:
            if lease in self.released:
                return True  # A lost response must not make safe cleanup permanent.
            if not lease or lease != self.lease:
                return False
            self.released.append(lease)
            self.lease = ''
            return True

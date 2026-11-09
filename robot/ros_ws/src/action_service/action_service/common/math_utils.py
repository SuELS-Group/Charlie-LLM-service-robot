from collections import deque
from enum import Enum, auto

import numpy as np


class GrabberState(Enum):
    IDLE = auto()
    SEARCHING = auto()
    SERVOING = auto()
    GRASPING = auto()
    RETRACTING = auto()
    FINISHED = auto()


def clamp(x, lo, hi):
    return float(np.clip(float(x), float(lo), float(hi)))


def soft_step(err, gain, max_step):
    err = float(err)
    gain = float(gain)
    max_step = float(max_step)
    if max_step <= 1e-9:
        return 0.0
    return float(max_step * np.tanh((gain * err) / max_step))


def rot_yxz(y, x, z):
    cy, sy = np.cos(y), np.sin(y)
    cx, sx = np.cos(x), np.sin(x)
    cz, sz = np.cos(z), np.sin(z)
    Ry = np.array([[cy, 0.0, sy], [0.0, 1.0, 0.0], [-sy, 0.0, cy]], dtype=np.float32)
    Rx = np.array([[1.0, 0.0, 0.0], [0.0, cx, -sx], [0.0, sx, cx]], dtype=np.float32)
    Rz = np.array([[cz, -sz, 0.0], [sz, cz, 0.0], [0.0, 0.0, 1.0]], dtype=np.float32)
    return (Ry @ Rx @ Rz).astype(np.float32)


class MedianFilter:

    def __init__(self, n=5):
        self.buf = deque(maxlen=int(n))

    def reset(self):
        self.buf.clear()

    def update(self, x):
        x = np.asarray(x, dtype=np.float32)
        self.buf.append(x)
        a = np.stack(self.buf, axis=0)
        return np.median(a, axis=0).astype(np.float32)

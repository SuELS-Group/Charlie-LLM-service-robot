from collections import deque
import math


class GestureEventGate:
    """Emit once for a stable gesture and re-arm only after a release."""

    def __init__(self, stable_frames: int, release_frames: int):
        self.stable_frames = max(1, int(stable_frames))
        self.release_frames = max(1, int(release_frames))
        self.reset()

    def reset(self, *, require_release: bool = False) -> None:
        self.active_frames = 0
        self.neutral_frames = 0
        self.latched = bool(require_release)

    def invalidate(self):
        self.active_frames=0
        self.neutral_frames=0

    def update(self, active: bool, *, stamp=None) -> bool:
        if active:
            self.neutral_frames = 0
            if self.latched:
                return False
            self.active_frames += 1
            if self.active_frames < self.stable_frames:
                return False
            self.latched = True
            return True
        self.active_frames = 0
        if not self.latched:
            return False
        self.neutral_frames += 1
        if self.neutral_frames >= self.release_frames:
            self.latched = False
            self.neutral_frames = 0
        return False


class TimedGestureEventGate:
    """Several fresh observations in a window, with one brief release edge."""
    def __init__(self,stable_frames=3,window_seconds=1.5,min_hold_seconds=.2,
                 release_seconds=.15,max_gap_seconds=1.5):
        self.stable_frames=max(3,int(stable_frames))
        self.window_seconds=float(window_seconds)
        self.min_hold_seconds=float(min_hold_seconds)
        self.release_seconds=float(release_seconds)
        self.max_gap_seconds=float(max_gap_seconds)
        if (not all(math.isfinite(value) for value in (self.window_seconds,self.min_hold_seconds,
                    self.release_seconds,self.max_gap_seconds)) or
                not .3<=self.window_seconds<=2. or not 0<self.max_gap_seconds<=self.window_seconds or
                not 0<self.release_seconds<=self.window_seconds or not 0<self.min_hold_seconds<self.window_seconds):
            raise ValueError('Invalid gesture timing window')
        self.reset()

    def reset(self,*,require_release=False):
        self.latched=bool(require_release)
        self.positive=deque(maxlen=128)
        self.neutral=deque(maxlen=128)
        self.last_stamp=None
        self.active_frames=self.neutral_frames=0

    def invalidate(self):
        # A bad frame cannot confirm anything. A brief interruption does not
        # demand a second deliberate release after one was already observed.
        self.positive.clear();self.neutral.clear()
        self.active_frames=self.neutral_frames=0

    def update(self,active,*,stamp):
        stamp=float(stamp)
        if not math.isfinite(stamp) or (self.last_stamp is not None and stamp<=self.last_stamp):
            return False
        if self.last_stamp is not None and stamp-self.last_stamp>self.max_gap_seconds:
            self.invalidate()
        self.last_stamp=stamp
        while self.positive and stamp-self.positive[0]>self.window_seconds:self.positive.popleft()
        self.active_frames=len(self.positive)
        if active:
            self.neutral.clear();self.neutral_frames=0
            if self.latched:return False
            self.positive.append(stamp)
            self.active_frames=len(self.positive)
            if self.active_frames>=self.stable_frames and stamp-self.positive[0]>=self.min_hold_seconds:
                self.latched=True
                return True
        else:
            self.neutral.append(stamp)
            self.neutral_frames=len(self.neutral)
            self.active_frames=len(self.positive)
            if self.neutral_frames>=2 and stamp-self.neutral[0]>=self.release_seconds:
                self.latched=False
                self.positive.clear();self.active_frames=0
        return False

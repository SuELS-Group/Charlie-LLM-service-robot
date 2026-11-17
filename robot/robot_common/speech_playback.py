"""Ordered PCM playback with events timed to the audio device's DAC clock."""
from collections import deque
import threading
import time

import numpy as np


class PlaybackBuffer:
    def __init__(self, sample_rate):
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._segments = deque()
        self._events = deque()
        self._active = {}

    def enqueue(self, samples, metadata):
        with self._lock:
            self._segments.append([np.asarray(samples, dtype=np.float32), 0, metadata])
            self._active[metadata['request_id']] = metadata
            self._events.append((time.time(), {**metadata, 'event': 'queued'}))

    def take(self, frames, dac_at):
        output = np.zeros(frames, dtype=np.float32)
        written = 0
        with self._lock:
            while self._segments and written < frames:
                audio, offset, metadata = self._segments[0]
                if offset == 0:
                    self._events.append((dac_at + written / self.sample_rate,
                                         {**metadata, 'event': 'started'}))
                count = min(len(audio) - offset, frames - written)
                output[written:written + count] = audio[offset:offset + count]
                written += count
                offset += count
                if offset == len(audio):
                    self._segments.popleft()
                    self._events.append((dac_at + written / self.sample_rate,
                                         {**metadata, 'event': 'finished'}))
                else:
                    self._segments[0][1] = offset
        return output, written > 0

    def stop(self):
        with self._lock:
            self._segments.clear()
            self._events.clear()
            now = time.time()
            for metadata in self._active.values():
                self._events.append((now, {**metadata, 'event': 'cancelled'}))
            self._active.clear()

    def events(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            ready, pending = [], deque()
            for at, event in self._events:
                if at <= now:
                    ready.append({**event, 'at': at})
                    if event['event'] == 'finished' and event['final_segment']:
                        self._active.pop(event['request_id'], None)
                else:
                    pending.append((at, event))
            self._events = pending
            return ready

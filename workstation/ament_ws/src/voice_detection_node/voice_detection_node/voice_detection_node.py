from __future__ import annotations

import json
import signal
import time
import math
import threading
from pathlib import Path
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4


def sigterm_handler(signum, frame):
    raise KeyboardInterrupt()


signal.signal(signal.SIGTERM, sigterm_handler)
import numpy as np
import rclpy
import sounddevice as sd
import torch
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Bool, Int16MultiArray, MultiArrayDimension, String
from global_stop.msg import GlobalStop
from workstation_common.speech_models import SmartTurn
from workstation_common import configure_logging, get_config, get_logger

config = get_config('voice_detection_node')
log = get_logger()


def load_vad():
    try:
        cached = Path(torch.hub.get_dir()) / 'snakers4_silero-vad_master'
        if cached.exists():
            model, utils = torch.hub.load(str(cached), config.vad.source.model, source='local')
        else:
            model, utils = torch.hub.load(repo_or_dir=config.vad.source.repo, model=config.vad.source.model, force_reload=False)
    except Exception as exc:
        raise RuntimeError(f'failed to load VAD model: {exc}') from exc
    return model, utils[3]


vad_model, VADIterator = load_vad()


def choose_microphone():
    for device in sd.query_devices():
        if device['max_input_channels'] <= 0:
            continue
        if config.audio.preferred_input_name in device['name'].lower():
            return device
    raise RuntimeError(f'input device containing {config.audio.preferred_input_name!r} was not found')


class VoiceDetectionNode(Node):

    def __init__(self, device_id: int) -> None:
        super().__init__('voice_detection_node')
        self.log = log
        if config.audio.sample_rate not in (8000, 16000):
            raise RuntimeError(f'unsupported sample rate for VAD: {config.audio.sample_rate}')
        audio_qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST, depth=128, )
        activity_qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL, history=HistoryPolicy.KEEP_LAST, depth=1, )
        self.audio_publisher = self.create_publisher(Int16MultiArray, config.topics.audio, audio_qos)
        self.led_publisher = self.create_publisher(String, config.topics.led, 10)
        self.activity_publisher = self.create_publisher(Bool, config.topics.activity, activity_qos)
        self.device_id = device_id
        self.speaking = False
        self.segment_sample_count = 0
        self.activity_active = False
        self.activity_hold_until = 0.0
        self.activity_enabled = bool(config.activity.enabled)
        self.activity_rms_threshold = max(0.0, float(config.activity.rms_threshold))
        self.activity_hold_seconds = max(0.05, float(config.activity.hold_seconds))
        self._turn_lock = threading.RLock()
        history_frames = math.ceil(config.audio.preroll_ms * config.audio.sample_rate / 1000 / config.audio.block_size)
        self._history = deque(maxlen=history_frames)
        self._turn_meta = None
        self._turn_audio = []
        self._pending_end = False
        self._silence_frames = 0
        self._next_check_sample = 0
        self._endpoint_future = None
        self._endpoint_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='smart-turn')
        self._endpoint = None
        if config.endpoint.enabled:
            try:
                self._endpoint = SmartTurn(config.endpoint)
                self.log.info('Smart Turn v3.2 ready on CPU')
            except Exception as exc:
                self.log.warning(f'Smart Turn unavailable; using fixed silence endpoint: {exc}')
        silence_ms = config.endpoint.candidate_silence_ms if self._endpoint else config.vad.min_silence_duration_ms
        self._candidate_silence_ms = silence_ms
        self.vad_iterator = VADIterator(vad_model, threshold=config.vad.threshold,
            min_silence_duration_ms=silence_ms, speech_pad_ms=config.vad.speech_pad_ms)
        self.create_subscription(GlobalStop, '/global_stop', self.stop_callback, 10)
        self.log.info(f'Voice detection started on device {device_id} at {config.audio.sample_rate} Hz '
                      f'(audio_topic={config.topics.audio}, activity_topic={config.topics.activity}, '
                      f'activity_enabled={self.activity_enabled}, '
                      f'activity_rms_threshold={self.activity_rms_threshold:.4f}, '
                      f'led_topic={config.topics.led})')
        self.publish_activity(False)

    def publish_led_status(self, status: str) -> None:
        self.led_publisher.publish(String(data=json.dumps({'status': status})))

    def publish_activity(self, active: bool) -> None:
        self.activity_publisher.publish(Bool(data=active))

    def update_microphone_activity(self, audio_float32: np.ndarray) -> None:
        if not self.activity_enabled:
            return
        rms = float(np.sqrt(np.mean(np.square(audio_float32)))) if audio_float32.size else 0.0
        now = time.monotonic()
        if rms >= self.activity_rms_threshold:
            self.activity_hold_until = now + self.activity_hold_seconds
            if not self.activity_active:
                self.activity_active = True
                self.publish_activity(True)
        elif self.activity_active and now >= self.activity_hold_until:
            self.activity_active = False
            self.publish_activity(False)

    def _publish_audio(self, samples, **state):
        metadata = {**self._turn_meta, **state}
        message = Int16MultiArray(data=samples.tolist())
        message.layout.dim = [MultiArrayDimension(label=json.dumps(metadata))]
        self.audio_publisher.publish(message)

    def _finish_turn(self, approved=True, reason='endpoint'):
        if self._turn_meta is not None:
            self._publish_audio(np.array([], dtype=np.int16), final=True, approved=approved,
                cancelled=not approved, reason=reason, ended_at=time.time())
            self.log.info(f'Audio turn {self._turn_meta["turn_id"]}: {reason}, '
                          f'{self.segment_sample_count / config.audio.sample_rate:.2f}s')
        self.speaking = False
        self._pending_end = False
        self._turn_meta = None
        self._turn_audio = []
        if self._endpoint_future is not None:
            self._endpoint_future.cancel()
            self._endpoint_future = None
        self.publish_led_status(config.led.status.idle)

    def stop_callback(self, _message):
        with self._turn_lock:
            self._finish_turn(False, 'global_stop')
            self._history.clear()
            self.vad_iterator.reset_states()

    def audio_callback(self, indata, _frames, _time_info, status) -> None:
        with self._turn_lock:
            self._process_audio(indata, status)

    def _process_audio(self, indata, status):
        if status:
            self.log.warning(f'Audio callback status: {status}')
        samples = indata[:, 0].astype(np.int16, copy=True)
        audio = samples.astype(np.float32) / 32768.
        captured_at = time.time() - len(samples) / config.audio.sample_rate
        self.update_microphone_activity(audio)
        self._history.append((samples, captured_at))
        activity = self.vad_iterator(torch.from_numpy(audio), return_seconds=False)
        new_start = bool(activity and 'start' in activity)
        if new_start:
            self._pending_end = False
            if self._endpoint_future is not None:
                self._endpoint_future.cancel()
                self._endpoint_future = None
            if not self.speaking:
                self.speaking = True
                self._turn_meta = {'turn_id': str(uuid4()), 'started_at': captured_at,
                                   'audio_started_at': self._history[0][1], 'sample_rate': config.audio.sample_rate}
                self._turn_audio = [frame for frame, _ in self._history]
                self.segment_sample_count = sum(len(frame) for frame in self._turn_audio)
                for frame in self._turn_audio:
                    self._publish_audio(frame, final=False)
                self.publish_led_status(config.led.status.speech)
                return
        if not self.speaking:
            return
        self._turn_audio.append(samples)
        self.segment_sample_count += len(samples)
        self._publish_audio(samples, final=False)
        if self.segment_sample_count > config.audio.max_turn_seconds * config.audio.sample_rate:
            self._finish_turn(False, 'maximum_turn_length')
            return
        if activity and 'end' in activity:
            self._pending_end = True
            self._silence_frames = 0
            self._next_check_sample = 0
        if not self._pending_end:
            return
        self._silence_frames += len(samples)
        silence_s = self._candidate_silence_ms / 1000 + self._silence_frames / config.audio.sample_rate
        if self._endpoint is None:
            if silence_s >= config.vad.min_silence_duration_ms / 1000:
                self._finish_turn(reason='fixed_silence')
            return
        if self._endpoint_future is None and self.segment_sample_count >= self._next_check_sample:
            segment = np.concatenate(self._turn_audio).astype(np.float32) / 32768.
            self._endpoint_future = self._endpoint_pool.submit(self._endpoint.predict, segment)
        if self._endpoint_future is not None and self._endpoint_future.done():
            try:
                result = self._endpoint_future.result()
                self.log.debug(f'Smart Turn endpoint: {result}')
                if result['probability'] >= config.endpoint.threshold:
                    self._finish_turn(reason='smart_turn')
                    return
            except Exception as exc:
                self.log.warning(f'Smart Turn failed; using silence endpoint: {exc}')
                self._endpoint.close()
                self._endpoint = None
            self._endpoint_future = None
            self._next_check_sample = self.segment_sample_count + int(.3 * config.audio.sample_rate)
        if silence_s >= config.endpoint.max_silence_s:
            self._finish_turn(reason='maximum_silence')

    def destroy_node(self):
        self._endpoint_pool.shutdown(wait=True, cancel_futures=True)
        if self._endpoint is not None:
            self._endpoint.close()
        super().destroy_node()

    def run(self) -> None:
        with sd.InputStream(device=self.device_id, samplerate=config.audio.sample_rate, channels=config.audio.channels, dtype=config.audio.dtype, blocksize=config.audio.block_size, callback=self.audio_callback, ):
            rclpy.spin(self)


def main(args=None) -> None:
    configure_logging()
    rclpy.init(args=args)
    node = None
    try:
        device = choose_microphone()
        log.info(f'Loaded VAD model {config.vad.source.model} from {config.vad.source.repo}')
        log.info(f'Using microphone {device["name"]!r} (index={device["index"]})')
        node = VoiceDetectionNode(device_id=device['index'])
        node.run()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        if node is not None:
            node.log.error(f'Voice detection failed: {exc}')
        else:
            log.error(f'Voice detection failed before node startup: {exc}')
            raise RuntimeError(str(exc)) from exc
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()

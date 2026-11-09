from __future__ import annotations

import json
import signal
import time


def sigterm_handler(signum, frame):
    raise KeyboardInterrupt()


signal.signal(signal.SIGTERM, sigterm_handler)
import numpy as np
import rclpy
import sounddevice as sd
import torch
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Bool, Int16MultiArray, String
from workstation_common import configure_logging, get_config, get_logger

config = get_config('voice_detection_node')
log = get_logger()


def load_vad():
    try:
        model, utils = torch.hub.load(repo_or_dir=config.vad.source.repo, model=config.vad.source.model, force_reload=False, )
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
        self.vad_iterator = VADIterator(vad_model, threshold=config.vad.threshold, min_silence_duration_ms=config.vad.min_silence_duration_ms, speech_pad_ms=config.vad.speech_pad_ms, )
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

    def audio_callback(self, indata, _frames, _time_info, status) -> None:
        if status:
            self.log.warning(f'Audio callback status: {status}')
        audio_chunk_int16 = indata[:, 0].flatten().astype(np.int16)
        audio_float32 = audio_chunk_int16.astype(np.float32) / 32768.0
        self.update_microphone_activity(audio_float32)
        audio_tensor = torch.from_numpy(audio_float32)
        speech_dict = self.vad_iterator(audio_tensor, return_seconds=False)
        if speech_dict and 'start' in speech_dict and not self.speaking:
            self.speaking = True
            self.segment_sample_count = 0
            self.log.info('Speech detected, streaming audio segment')
            self.publish_led_status(config.led.status.speech)
        if self.speaking:
            self.audio_publisher.publish(Int16MultiArray(data=audio_chunk_int16.tolist()))
            self.segment_sample_count += len(audio_chunk_int16)
        if speech_dict and 'end' in speech_dict:
            duration_s = self.segment_sample_count / float(config.audio.sample_rate)
            self.speaking = False
            self.publish_led_status(config.led.status.idle)
            self.audio_publisher.publish(Int16MultiArray(data=[]))
            self.log.info(f'Published speech segment ({duration_s:.2f}s) to {config.topics.audio}')

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

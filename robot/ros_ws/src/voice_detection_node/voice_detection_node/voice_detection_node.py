from __future__ import annotations

import json
import signal
from pathlib import Path


def sigterm_handler(signum, frame):
    raise KeyboardInterrupt()


signal.signal(signal.SIGTERM, sigterm_handler)
import numpy as np
import rclpy
import sounddevice as sd
import torch
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from robot_common import configure_logging, get_config, get_logger
from std_msgs.msg import Int16MultiArray, String

config = get_config('voice_detection_node')
log = get_logger()


def _silero_cache_dir() -> Path:
    hub_dir = Path(torch.hub.get_dir())
    repo = str(config.vad.source.repo).rstrip('/').replace('/', '_')
    candidates = [hub_dir / f'{repo}_master', hub_dir / f'{repo}_main', ]
    repo_name_only = str(config.vad.source.repo).rstrip('/').split('/')[-1]
    candidates.extend([hub_dir / f'{repo_name_only}_master', hub_dir / f'{repo_name_only}_main', ])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError('silero VAD cache directory is missing under '
                       f'{hub_dir}. Expected one of: {", ".join(str(path.name) for path in candidates)}')


def _silero_model_path() -> Path:
    model_path = _silero_cache_dir() / 'src' / 'silero_vad' / 'data' / 'silero_vad.jit'
    if not model_path.exists():
        raise RuntimeError('silero VAD cache is missing. Expected model file at '
                           f'{model_path}.')
    return model_path


class SileroVADIterator:

    def __init__(self, model, threshold: float = 0.5, sampling_rate: int = 16000, min_silence_duration_ms: int = 100, speech_pad_ms: int = 30, ) -> None:
        self.model = model
        self.threshold = threshold
        self.sampling_rate = sampling_rate
        if sampling_rate not in (8000, 16000):
            raise ValueError('VADIterator does not support sampling rates other than [8000, 16000]')
        self.min_silence_samples = sampling_rate * min_silence_duration_ms / 1000
        self.speech_pad_samples = sampling_rate * speech_pad_ms / 1000
        self.reset_states()

    def reset_states(self) -> None:
        self.model.reset_states()
        self.triggered = False
        self.temp_end = 0
        self.current_sample = 0

    @torch.no_grad()
    def __call__(self, x, return_seconds: bool = False, time_resolution: int = 1):
        if not torch.is_tensor(x):
            try:
                x = torch.Tensor(x)
            except Exception as exc:
                raise TypeError('Audio cannot be casted to tensor. Cast it manually') from exc
        window_size_samples = len(x[0]) if x.dim() == 2 else len(x)
        self.current_sample += window_size_samples
        speech_prob = self.model(x, self.sampling_rate).item()
        if speech_prob >= self.threshold and self.temp_end:
            self.temp_end = 0
        if speech_prob >= self.threshold and not self.triggered:
            self.triggered = True
            speech_start = max(0, self.current_sample - self.speech_pad_samples - window_size_samples)
            if return_seconds:
                return {'start': round(speech_start / self.sampling_rate, time_resolution)}
            return {'start': int(speech_start)}
        if speech_prob < self.threshold - 0.15 and self.triggered:
            if not self.temp_end:
                self.temp_end = self.current_sample
            if self.current_sample - self.temp_end < self.min_silence_samples:
                return None
            speech_end = self.temp_end + self.speech_pad_samples - window_size_samples
            self.temp_end = 0
            self.triggered = False
            if return_seconds:
                return {'end': round(speech_end / self.sampling_rate, time_resolution)}
            return {'end': int(speech_end)}
        return None


def load_vad():
    try:
        model = torch.jit.load(str(_silero_model_path()), map_location='cpu')
        model.eval()
    except Exception as exc:
        raise RuntimeError(f'failed to load VAD model: {exc}') from exc
    return model, SileroVADIterator


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
        self.audio_publisher = self.create_publisher(Int16MultiArray, config.topics.audio, audio_qos)
        self.led_publisher = self.create_publisher(String, config.topics.led, 10)
        self.device_id = device_id
        self.speaking = False
        self.segment_sample_count = 0
        self.vad_iterator = VADIterator(vad_model, threshold=config.vad.threshold, min_silence_duration_ms=config.vad.min_silence_duration_ms, speech_pad_ms=config.vad.speech_pad_ms, )
        self.log.info(f'Voice detection started on device {device_id} at {config.audio.sample_rate} Hz '
                      f'(audio_topic={config.topics.audio}, led_topic={config.topics.led})')

    def publish_led_status(self, status: str) -> None:
        self.led_publisher.publish(String(data=json.dumps({'status': status})))

    def audio_callback(self, indata, _frames, _time_info, status) -> None:
        if status:
            self.log.warning(f'Audio callback status: {status}')
        audio_chunk_int16 = indata[:, 0].flatten().astype(np.int16)
        audio_tensor = torch.from_numpy(audio_chunk_int16.astype(np.float32) / 32768.0)
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

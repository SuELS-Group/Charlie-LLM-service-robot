from __future__ import annotations

import io
import json
import re
import tarfile
import unicodedata
import urllib.request
import wave
import time
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import rclpy
import sherpa_onnx
from rclpy.node import Node
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from scipy.signal import resample_poly
from std_msgs.msg import String
from workstation_common import configure_logging, get_config, get_logger
from workstation_common.language import parse_tts_request, warmup_speech_language_detection

from tts_playback_interfaces.srv import PlayAudio
from global_stop.msg import GlobalStop

config = get_config('tts_synthesis_node')
log = get_logger()


class TTSSynthesisNode(Node):

    def __init__(self) -> None:
        super().__init__('tts_synthesis_node')
        self.log = log
        if warmup_speech_language_detection():
            self.log.info('Local CPU speech language detector ready')
        else:
            self.log.warning('Speech language detector unavailable; retaining supplied voice language')
        self._stop_lock = threading.Lock()
        self._last_stop_time = 0.0
        self._speech_group = MutuallyExclusiveCallbackGroup()
        self._stop_group = ReentrantCallbackGroup()
        self.audio_client = self.create_client(PlayAudio, config.services.play_audio, callback_group=self._stop_group)
        self.create_subscription(String, config.topics.input, self.listener_callback, 10, callback_group=self._speech_group)
        self.create_subscription(GlobalStop, '/global_stop', self.global_stop_callback, 10, callback_group=self._stop_group)
        self.log.info(f'Listening on {config.topics.input}, sending audio to service {config.services.play_audio} '
                      f'(provider={config.runtime.provider}, threads={config.runtime.num_threads})')
        self.engines = self.load_engines()
        if self.engines:
            loaded_languages = ', '.join(sorted(self.engines))
            self.log.info(f'Loaded TTS engines for: {loaded_languages}')
        else:
            self.log.error('No TTS models could be loaded.')

    def load_engines(self) -> dict[str, sherpa_onnx.OfflineTts]:
        self.log.info(f'Checking models in {config.runtime.model_dir}...')
        engines: dict[str, sherpa_onnx.OfflineTts] = {}
        for language, model in vars(config.models).items():
            model_path = self.ensure_model(model)
            if model_path is None:
                self.log.warning(f'Skipping {language}: model assets are unavailable')
                continue
            engine = self.init_engine(model_path)
            if engine is None:
                self.log.warning(f'Skipping {language}: failed to initialize engine from {model_path}')
                continue
            engines[language] = engine
        return engines

    def ensure_model(self, model) -> Path | None:
        model_root = Path(config.runtime.model_dir)
        language_dir = model_root / model.directory
        model_file = language_dir / model.file
        if model_file.exists():
            self.log.info(f'Model ready: {model.directory}')
            return language_dir
        self.log.info(f'Downloading model {model.directory}...')
        model_root.mkdir(parents=True, exist_ok=True)
        archive_path = model_root / f'{model.directory}.tar.bz2'
        try:
            urllib.request.urlretrieve(model.url, archive_path)
            with tarfile.open(archive_path, 'r:bz2') as archive:
                archive.extractall(path=model_root)
        except Exception as exc:
            self.log.error(f'Failed to download model {model.directory}: {exc}')
            return None
        finally:
            archive_path.unlink(missing_ok=True)
        return language_dir if model_file.exists() else None

    def init_engine(self, model_dir: Path) -> sherpa_onnx.OfflineTts | None:
        model_file = next(model_dir.glob('*.onnx'), None)
        if model_file is None:
            return None
        engine_config = sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=str(model_file), tokens=str(model_dir / 'tokens.txt'), data_dir=str(model_dir / 'espeak-ng-data'), ), provider=config.runtime.provider, num_threads=config.runtime.num_threads, ))
        return sherpa_onnx.OfflineTts(engine_config)

    def global_stop_callback(self, msg):
        stamp = msg.stamp.sec + msg.stamp.nanosec / 1e9
        with self._stop_lock:
            self._last_stop_time = max(self._last_stop_time, stamp or time.time())

    def listener_callback(self, msg: String) -> None:
        payload = self.parse_request(msg.data)
        if payload is None:
            self.log.warning('Ignoring invalid TTS request payload')
            return
        issued_at = payload.issued_at or time.time()
        with self._stop_lock:
            if issued_at <= self._last_stop_time:
                return
        engine = self.engines.get(payload.lang)
        if engine is None:
            self.log.warning(f'Unsupported language: {payload.lang}')
            return
        text = self.normalize_text(payload.text)
        if not text:
            self.log.warning('Ignoring empty TTS text after normalization')
            return
        if payload.lang != payload.language_hint:
            self.log.info(f'TTS language corrected from {payload.language_hint} to {payload.lang} based on text')
        self.log.info(f'Generating ({payload.lang}): {text}')
        try:
            audio = engine.generate(text, sid=0, speed=config.runtime.generation_speed)
        except Exception as exc:
            self.log.error(f'Generation failed: {exc}')
            return
        if not audio or len(audio.samples) == 0:
            self.log.warning(f'TTS engine returned no audio for language {payload.lang}')
            return
        wav = np.asarray(audio.samples, dtype=np.float32)
        if config.audio.model_sample_rate != config.audio.target_sample_rate:
            wav = resample_poly(wav, config.audio.target_sample_rate, config.audio.model_sample_rate)
        wav = np.clip(wav, -1.0, 1.0)
        wav_int16 = (wav * 32767.0).astype(np.int16)
        wav_data = self.encode_wav(wav_int16, config.audio.target_sample_rate)
        if not self.audio_client.wait_for_service(timeout_sec=2.0):
            self.log.error(f'TTS playback service unavailable: {config.services.play_audio}')
            return
        request = PlayAudio.Request()
        request.wav_data = list(wav_data)
        request.issued_at = issued_at
        with self._stop_lock:
            if issued_at <= self._last_stop_time:
                return
            future = self.audio_client.call_async(request)
        future.add_done_callback(lambda response_future: self.log_playback_response(response_future, len(wav_int16), len(wav_data)))

    @staticmethod
    def encode_wav(samples: np.ndarray, sample_rate: int) -> bytes:
        with io.BytesIO() as wav_buffer:
            with wave.open(wav_buffer, 'wb') as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(samples.astype('<i2', copy=False).tobytes())
            return wav_buffer.getvalue()

    def log_playback_response(self, future, sample_count: int, byte_count: int) -> None:
        try:
            response = future.result()
        except Exception as exc:
            self.log.error(f'Failed to send TTS WAV to playback service: {exc}')
            return
        if response.success:
            self.log.info(f'Sent {sample_count} samples as one {byte_count} byte WAV request: {response.message}')
        else:
            self.log.error(f'TTS playback rejected WAV request: {response.message}')

    @staticmethod
    def parse_request(raw_data: str):
        return parse_tts_request(raw_data)

    @staticmethod
    def normalize_text(text: str) -> str:
        normalized = unicodedata.normalize('NFKC', text or '')
        out = []
        for char in normalized:
            category = unicodedata.category(char)
            if category[0] in {'L', 'M', 'N'}:
                out.append(char)
            elif category[0] == 'Z' or category[0] == 'C':
                out.append(' ')
            elif category[0] == 'P':
                if char in '.!?;:,':
                    out.append(char)
                elif char in "'’`":
                    out.append("'")
                else:
                    out.append(' ')
            elif category[0] == 'S':
                out.append(' ')
            else:
                out.append(' ')
        cleaned = ''.join(out)
        cleaned = re.sub(r'\s+', ' ', cleaned)
        cleaned = re.sub(r'\s+([.!?;:,])', r'\1', cleaned)
        cleaned = re.sub(r'([.!?;:,]){2,}', r'\1', cleaned)
        cleaned = re.sub(r'([.!?;:,])([^\s.!?;:,])', r'\1 \2', cleaned)
        return cleaned.strip()


def main(args=None) -> None:
    configure_logging()
    rclpy.init(args=args)
    node = None
    executor = None
    try:
        node = TTSSynthesisNode()
        executor = MultiThreadedExecutor(num_threads=2)
        executor.add_node(node)
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        if executor is not None:
            executor.shutdown()
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()

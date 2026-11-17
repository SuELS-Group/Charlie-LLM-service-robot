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
from uuid import uuid4

import numpy as np
import rclpy
import sherpa_onnx
from rclpy.node import Node
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from std_msgs.msg import String
from workstation_common import configure_logging, get_config, get_logger
from workstation_common.speech_audio import condition_audio
from workstation_common.speech_text import spoken_text, speech_chunks
from workstation_common.speech_models import QwenTTS, QwenVoice
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
        self.status_publisher = self.create_publisher(String, '/speech/playback', 50)
        self.create_subscription(String, config.topics.input, self.listener_callback, 10, callback_group=self._speech_group)
        self.create_subscription(GlobalStop, '/global_stop', self.global_stop_callback, 10, callback_group=self._stop_group)
        self.log.info(f'Listening on {config.topics.input}, sending audio to service {config.services.play_audio} '
                      f'(provider={config.runtime.provider}, threads={config.runtime.num_threads})')
        self.engines = self.load_engines()
        self.qwen_model = None
        if config.runtime.backend not in ('qwen', 'piper'):
            raise ValueError('TTS backend must be qwen or piper')
        if config.runtime.backend == 'qwen':
            try:
                self.qwen_model = QwenTTS(config.qwen)
                self.engines = {language: QwenVoice(self.qwen_model, language,
                    self.engines.get(language) if config.runtime.piper_fallback else None, self.log)
                    for language in ('de', 'en')}
                self.log.info('Qwen TTS ready; fixed voice prompt and model are warm')
            except Exception as exc:
                if not config.runtime.piper_fallback:
                    raise
                self.log.error(f'Qwen startup failed; using Piper: {exc}')
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
        request_id = payload.speech_id or str(uuid4())
        engine = self.engines.get(payload.lang)
        if engine is None:
            self.report_failure(request_id, issued_at, f'Unsupported language: {payload.lang}')
            return
        text = self.normalize_text(spoken_text(payload.text, payload.lang))
        sentences = speech_chunks(text, config.runtime.chunk_target_chars)
        if not sentences:
            return
        if payload.lang != payload.language_hint:
            self.log.info(f'TTS language corrected from {payload.language_hint} to {payload.lang} based on text')
        for index, sentence in enumerate(sentences):
            with self._stop_lock:
                if issued_at <= self._last_stop_time:
                    return
            self.log.info(f'Generating ({payload.lang}, segment {index + 1}/{len(sentences)}): {sentence}')
            try:
                audio = engine.generate(sentence, sid=0, speed=config.runtime.generation_speed)
                with self._stop_lock:
                    if issued_at <= self._last_stop_time:
                        return
                if audio is None or len(audio.samples) == 0:
                    raise ValueError('TTS engine returned empty audio')
                source_rate = int(getattr(audio, 'sample_rate', config.audio.model_sample_rate))
                wav, metrics = condition_audio(audio.samples, source_rate, config.audio.target_sample_rate,
                                               config.audio.target_lufs, config.audio.peak_dbfs)
            except Exception as exc:
                self.report_failure(request_id, issued_at, f'Speech generation failed: {exc}')
                return
            self.log.info(f'Conditioned speech: {metrics}')
            wav_int16 = (wav * 32767.0).astype(np.int16)
            wav_data = self.encode_wav(wav_int16, config.audio.target_sample_rate)
            if not self.audio_client.wait_for_service(timeout_sec=2.0):
                self.report_failure(request_id, issued_at, 'TTS playback service unavailable')
                return
            request = PlayAudio.Request()
            request.wav_data = list(wav_data)
            request.issued_at = issued_at
            request.request_id = request_id
            request.segment_index = index
            request.final_segment = index == len(sentences) - 1
            with self._stop_lock:
                if issued_at <= self._last_stop_time:
                    return
                future = self.audio_client.call_async(request)
            future.add_done_callback(lambda response_future, count=len(wav_int16), size=len(wav_data):
                                    self.log_playback_response(response_future, count, size))
            deadline = time.monotonic() + 3.
            while not future.done():
                if time.monotonic() >= deadline:
                    self.report_failure(request_id, issued_at, 'Playback did not acknowledge the queued sentence')
                    return
                time.sleep(.01)
            try:
                if not future.result().success:
                    self.report_failure(request_id, issued_at, 'Playback rejected the audio')
                    return
            except Exception as exc:
                self.report_failure(request_id, issued_at, f'Playback request failed: {exc}')
                return

    def report_failure(self, request_id, issued_at, reason):
        self.log.error(reason)
        self.status_publisher.publish(String(data=json.dumps({'request_id': request_id,
            'issued_at': issued_at, 'event': 'failed', 'final_segment': True, 'at': time.time(), 'reason': reason})))

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


    def destroy_node(self):
        if self.qwen_model is not None:
            self.qwen_model.close()
        super().destroy_node()


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

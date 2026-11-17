"""Local speech backends; incompatible model libraries stay in separate processes."""
import base64
import json
import os
from pathlib import Path
import select
import subprocess
import threading
from types import SimpleNamespace
from uuid import uuid4

import numpy as np


class LocalSpeechModel:
    def __init__(self, python, worker_name, arguments, timeout_s):
        self._lock = threading.Lock()
        self._timeout = float(timeout_s)
        worker = Path(__file__).with_name(worker_name)
        environment = dict(os.environ, HF_HUB_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
        # The model environment supplies its own libraries and never imports ROS.
        environment.pop('PYTHONPATH', None)
        environment.pop('VIRTUAL_ENV', None)
        environment.pop('LD_LIBRARY_PATH', None)
        self.process = subprocess.Popen([str(Path(python).expanduser()), '-u', str(worker), *arguments],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8', env=environment)
        try:
            ready = self._read()
            if ready.get('ready') is not True:
                raise RuntimeError(f'Local speech model did not become ready: {ready}')
        except Exception:
            self.close()
            raise

    def _read(self):
        if not select.select([self.process.stdout], [], [], self._timeout)[0]:
            raise TimeoutError('Local speech model did not respond')
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError('Local speech model exited')
        return json.loads(line)

    def request(self, payload):
        with self._lock:
            ident = str(uuid4())
            try:
                self.process.stdin.write(json.dumps({**payload, 'id': ident}) + '\n')
                self.process.stdin.flush()
                response = self._read()
                if response.get('id') != ident or response.get('error'):
                    raise RuntimeError(response.get('error', 'Mismatched local speech response'))
                return response
            except Exception:
                # A timed-out response can never be consumed by the next request.
                self.close()
                raise

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()


class QwenTTS(LocalSpeechModel):
    def __init__(self, config):
        super().__init__(config.python, 'qwen_tts_worker.py', ['--model', config.model,
            '--revision', config.revision, '--voice-profile', str(Path(config.voice_profile).expanduser())], config.timeout_s)

    def generate(self, text, language):
        response = self.request({'text': text, 'language': language})
        audio = np.frombuffer(base64.b64decode(response['audio'], validate=True), dtype='<f4').copy()
        return SimpleNamespace(samples=audio, sample_rate=int(response['sample_rate']))


class SmartTurn(LocalSpeechModel):
    def __init__(self, config):
        super().__init__(config.python, 'smart_turn_worker.py', ['--model', config.model,
            '--revision', config.revision, '--file', config.file], config.timeout_s)

    def predict(self, audio):
        encoded = base64.b64encode(np.asarray(audio, dtype='<f4')[-128000:].tobytes()).decode('ascii')
        return self.request({'audio': encoded})


class QwenASR(LocalSpeechModel):
    def __init__(self, config):
        super().__init__(config.python, 'qwen_asr_worker.py', ['--model', config.model,
            '--revision', config.revision], config.timeout_s)

    def transcribe(self, audio, language=None):
        encoded = base64.b64encode(np.asarray(audio, dtype='<f4').tobytes()).decode('ascii')
        return self.request({'audio': encoded, 'language': language})


class WhisperASR:
    def __init__(self, config, languages):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(config.size, device=config.device, compute_type=config.compute_type)
        self.beam_size, self.languages = config.beam_size, languages

    def transcribe(self, audio, language=None):
        from .transcription import select_transcription, transcription_candidate
        candidates = []
        for language in [language] if language in self.languages else self.languages:
            segments, _ = self.model.transcribe(audio, beam_size=self.beam_size, language=language)
            candidates.append(transcription_candidate(list(segments), language))
        return select_transcription(candidates)

    def close(self):
        self.model = None


class SpeechRecognizer:
    def __init__(self, config, log):
        self.config, self.log = config, log
        self.backend = None
        self.name = config.runtime.backend
        if self.name not in ('qwen', 'whisper'):
            raise ValueError(f'Unknown ASR backend: {self.name}')
        try:
            self.backend = self._load()
        except Exception as exc:
            self._fallback(exc)

    def _load(self):
        self.log.info(f'Loading speech recognizer: {self.name}')
        if self.name == 'qwen':
            return QwenASR(self.config.qwen)
        return WhisperASR(self.config.model, self.config.language.candidates)

    def _fallback(self, error):
        if self.name != 'qwen' or not self.config.runtime.whisper_fallback:
            raise RuntimeError(f'{self.name} ASR unavailable: {error}') from error
        self.log.warning(f'Qwen ASR unavailable; loading Whisper fallback: {error}')
        if self.backend is not None:
            self.backend.close()
        self.backend = None
        self.name = 'whisper'
        self.backend = self._load()

    def transcribe(self, audio, language=None):
        try:
            return self.backend.transcribe(audio, language=language)
        except Exception as exc:
            self._fallback(exc)
            return self.backend.transcribe(audio, language=language)

    def close(self):
        if self.backend is not None:
            self.backend.close()


class QwenVoice:
    def __init__(self, model, language, fallback, log):
        self.model, self.language, self.fallback, self.log = model, language, fallback, log

    def generate(self, text, sid=0, speed=1.):
        try:
            return self.model.generate(text, self.language)
        except Exception as exc:
            if self.fallback is None:
                raise
            self.log.warning(f'Qwen TTS unavailable; using Piper for {self.language}: {exc}')
            return self.fallback.generate(text, sid=sid, speed=speed)

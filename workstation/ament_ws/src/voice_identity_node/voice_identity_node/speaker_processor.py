from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np


@dataclass(frozen=True)
class IdentificationResult:
    speaker_name: str
    speaker_score: float
    duration_seconds: float
    matched: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = {
            'speaker_name': self.speaker_name,
            'speaker_score': round(float(self.speaker_score), 3),
            'duration_seconds': round(float(self.duration_seconds), 3),
            'matched': bool(self.matched), }
        if self.reason:
            result['reason'] = self.reason
        return result


class SpeakerProcessor:
    """SpeechBrain ECAPA-TDNN speaker embedding helper.
    The processor mirrors the face recognition flow:
      prerecorded sample -> embedding -> JSON DB
      VAD speech segment -> embedding -> cosine match against JSON DB
    """

    def __init__(self, db_path: str | Path, *, model_source: str = 'speechbrain/spkrec-ecapa-voxceleb', model_savedir: str | Path | None = None, device: str = 'auto', threshold: float = 0.45, sample_rate: int = 16000, min_duration_seconds: float = 1.0, max_duration_seconds: float = 20.0, logger=None, ) -> None:
        self.db_path = self._expand_path(db_path)
        self.model_source = model_source
        self.model_savedir = self._expand_path(model_savedir) if model_savedir else None
        self.requested_device = device
        self.threshold = float(threshold)
        self.sample_rate = int(sample_rate)
        self.min_duration_seconds = float(min_duration_seconds)
        self.max_duration_seconds = float(max_duration_seconds)
        self.log = logger
        self._encoder = None
        self._torch = None
        self._torchaudio = None
        self.speakers: dict[str, dict[str, Any]] = {}
        self.load_db()

    @staticmethod
    def _expand_path(value: str | Path | None) -> Path | None:
        if value is None:
            return None
        return Path(os.path.expanduser(os.path.expandvars(str(value)))).resolve()

    @staticmethod
    def _empty_db() -> dict[str, Any]:
        return {'version': 1, 'speakers': {}}

    @staticmethod
    def speaker_name_from_path(path: Path) -> str:
        """Infer a speaker name from a sample filename.
        Recommended names:
          TestUserB.wav
          Lennert_02.wav
          Lennert__01.wav  # legacy separator also accepted
          TestUserB-01.wav
        """
        stem = path.stem.strip()
        if '__' in stem:
            stem = stem.split('__', 1)[0]
        else:
            stem = re.sub(r'([_-])(sample|voice)?\d+$', '', stem, flags=re.IGNORECASE)
        name = ' '.join(stem.replace('_', ' ').replace('-', ' ').split())
        return name or 'unknown'

    def load_db(self) -> None:
        if not self.db_path.exists():
            self.speakers = {}
            return
        with self.db_path.open('r', encoding='utf-8') as stream:
            data = json.load(stream)
        speakers = {}
        for name, entry in (data.get('speakers') or {}).items():
            embeddings = []
            for value in (entry or {}).get('embeddings', []):
                arr = np.asarray(value, dtype=np.float32)
                if arr.ndim == 1 and arr.size:
                    norm = np.linalg.norm(arr)
                    if norm > 0:
                        embeddings.append((arr / norm).astype(np.float32))
            if embeddings:
                speakers[str(name)] = {
                    'embeddings': embeddings,
                    'notes': (entry or {}).get('notes', ''),
                    'updated_at': (entry or {}).get('updated_at', ''), }
        self.speakers = speakers

    def _db_data(self) -> dict[str, Any]:
        data = self._empty_db()
        updated_at = datetime.now().isoformat()
        for name, entry in sorted(self.speakers.items(), key=lambda item: item[0].lower()):
            data['speakers'][name] = {
                'embeddings': [embedding.astype(float).tolist() for embedding in entry.get('embeddings', [])],
                'notes': entry.get('notes', ''),
                'updated_at': entry.get('updated_at') or updated_at, }
        return data

    def save_db(self) -> bool:
        data = self._db_data()
        next_text = json.dumps(data, indent=2)
        if self.db_path.exists():
            try:
                if self.db_path.read_text(encoding='utf-8') == next_text:
                    return False
            except OSError:
                pass
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.db_path.with_suffix(self.db_path.suffix + '.tmp')
        tmp_path.write_text(next_text, encoding='utf-8')
        tmp_path.replace(self.db_path)
        return True

    @staticmethod
    def _speaker_entries_match(existing: dict[str, Any] | None, candidate: dict[str, Any]) -> bool:
        if not existing:
            return False
        if existing.get('notes', '') != candidate.get('notes', ''):
            return False
        existing_embeddings = existing.get('embeddings', [])
        candidate_embeddings = candidate.get('embeddings', [])
        if len(existing_embeddings) != len(candidate_embeddings):
            return False
        for old, new in zip(existing_embeddings, candidate_embeddings):
            if np.asarray(old).shape != np.asarray(new).shape:
                return False
            if not np.allclose(old, new, rtol=1e-6, atol=1e-6):
                return False
        return True

    def _load_ml_stack(self):
        if self._encoder is not None:
            return self._torch, self._torchaudio, self._encoder
        try:
            import torch
            import torchaudio

            try:
                from speechbrain.inference.speaker import EncoderClassifier
            except ModuleNotFoundError:
                from speechbrain.pretrained import EncoderClassifier
        except Exception as exc:  # pragma: no cover - depends on workstation env
            raise RuntimeError('SpeechBrain speaker recognition dependencies are not available. '
                               'Run `uv sync` in workstation after adding speechbrain, then rebuild the ROS package.') from exc
        if self.requested_device == 'auto':
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
        else:
            device = self.requested_device
            if device == 'cuda' and not torch.cuda.is_available():
                device = 'cpu'
        savedir = str(self.model_savedir) if self.model_savedir else None
        if self.log:
            self.log.info(f'Loading speaker model {self.model_source} on {device}')
        kwargs = {'source': self.model_source, 'run_opts': {'device': device}}
        if savedir:
            kwargs['savedir'] = savedir
        encoder = EncoderClassifier.from_hparams(**kwargs)
        self._torch = torch
        self._torchaudio = torchaudio
        self._encoder = encoder
        return self._torch, self._torchaudio, self._encoder

    def _normalize_embedding(self, embedding) -> np.ndarray:
        torch, _, _ = self._load_ml_stack()
        emb = embedding.squeeze()
        emb = torch.nn.functional.normalize(emb, dim=0)
        return emb.detach().cpu().numpy().astype(np.float32)

    def _validate_duration(self, num_samples: int) -> float:
        duration_seconds = float(num_samples) / float(self.sample_rate)
        if duration_seconds < self.min_duration_seconds:
            raise ValueError(f'Audio segment is too short ({duration_seconds:.2f}s); '
                             f'minimum is {self.min_duration_seconds:.2f}s')
        return duration_seconds

    def embedding_from_int16(self, audio_int16: np.ndarray) -> tuple[np.ndarray, float]:
        torch, _, encoder = self._load_ml_stack()
        audio = np.asarray(audio_int16, dtype=np.int16)
        duration_seconds = self._validate_duration(audio.size)
        max_samples = int(self.max_duration_seconds * self.sample_rate)
        if max_samples > 0 and audio.size > max_samples:
            audio = audio[:max_samples]
            duration_seconds = float(audio.size) / float(self.sample_rate)
        waveform = torch.from_numpy(audio.astype(np.float32) / 32768.0).unsqueeze(0)
        waveform = waveform.to(next(encoder.mods.parameters()).device)
        with torch.no_grad():
            embedding = encoder.encode_batch(waveform)
        return self._normalize_embedding(embedding), duration_seconds

    def embedding_from_file(self, path: str | Path) -> tuple[np.ndarray, float]:
        torch, torchaudio, encoder = self._load_ml_stack()
        file_path = self._expand_path(path)
        if file_path is None or not file_path.exists():
            raise FileNotFoundError(f'Audio sample not found: {path}')
        waveform, source_rate = torchaudio.load(str(file_path))
        if waveform.ndim != 2 or waveform.shape[1] == 0:
            raise ValueError(f'Audio sample has no samples: {file_path}')
        if waveform.shape[0] > 1:
            waveform = waveform.mean(dim=0, keepdim=True)
        if int(source_rate) != self.sample_rate:
            waveform = torchaudio.functional.resample(waveform, int(source_rate), self.sample_rate)
        duration_seconds = self._validate_duration(int(waveform.shape[1]))
        max_samples = int(self.max_duration_seconds * self.sample_rate)
        if max_samples > 0 and waveform.shape[1] > max_samples:
            waveform = waveform[:, :max_samples]
            duration_seconds = float(waveform.shape[1]) / float(self.sample_rate)
        waveform = waveform.to(next(encoder.mods.parameters()).device)
        with torch.no_grad():
            embedding = encoder.encode_batch(waveform)
        return self._normalize_embedding(embedding), duration_seconds

    def learn_speaker_from_file(self, name: str, path: str | Path, *, notes: str | None = None) -> dict[str, Any]:
        clean_name = ' '.join((name or '').strip().split())
        if not clean_name:
            raise ValueError('Speaker name is required')
        embedding, duration_seconds = self.embedding_from_file(path)
        entry = self.speakers.setdefault(clean_name, {'embeddings': [], 'notes': '', 'updated_at': ''})
        entry['embeddings'].append(embedding)
        if notes is not None:
            entry['notes'] = notes
        elif not entry.get('notes'):
            entry['notes'] = f'Loaded from {Path(path).name}'
        entry['updated_at'] = datetime.now().isoformat()
        self.save_db()
        return {
            'status': 'success',
            'speaker': clean_name,
            'sample_count': len(entry['embeddings']),
            'duration_seconds': round(duration_seconds, 3), }

    def regenerate_from_directory(self, sample_dir: str | Path, *, extensions: set[str] | None = None, ignore_prefixes:
    tuple[str, ...] = ('_', '.'), ) -> dict[str, Any]:
        directory = self._expand_path(sample_dir)
        if directory is None:
            raise ValueError('Sample directory is required')
        extensions = extensions or {'.wav',
                                    '.flac',
                                    '.mp3',
                                    '.m4a',
                                    '.mp4',
                                    '.aac',
                                    '.ogg',
                                    '.oga',
                                    '.opus',
                                    '.webm',
                                    '.weba',
                                    '.aif',
                                    '.aiff',
                                    '.au',
                                    '.caf',
                                    '.wma'}
        existing_speakers = self.speakers
        next_speakers: dict[str, dict[str, Any]] = {}
        failed = []
        loaded_samples = 0
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
        for audio_path in sorted(directory.iterdir()):
            if not audio_path.is_file():
                continue
            if any(audio_path.name.startswith(prefix) for prefix in ignore_prefixes):
                continue
            if audio_path.suffix.lower() not in extensions:
                continue
            name = self.speaker_name_from_path(audio_path)
            try:
                embedding, duration_seconds = self.embedding_from_file(audio_path)
            except Exception as exc:
                failed.append({'file': audio_path.name, 'error': str(exc)})
                continue
            entry = next_speakers.setdefault(name, {
                'embeddings': [],
                'notes': f'Loaded from {directory.name}',
                'updated_at': '', })
            entry['embeddings'].append(embedding)
            loaded_samples += 1
            if self.log:
                self.log.info(f'Loaded voice sample {audio_path.name} for {name} ({duration_seconds:.2f}s)')
        updated_at = datetime.now().isoformat()
        for name, entry in next_speakers.items():
            existing = existing_speakers.get(name)
            if self._speaker_entries_match(existing, entry):
                entry['updated_at'] = existing.get('updated_at', '') or updated_at
            else:
                entry['updated_at'] = updated_at
        self.speakers = next_speakers
        changed = self.save_db()
        return {
            'status': 'success',
            'speaker_count': len(next_speakers),
            'sample_count': loaded_samples,
            'failed': failed,
            'db_path': str(self.db_path),
            'changed': changed, }

    def identify_int16(self, audio_int16: np.ndarray) -> IdentificationResult:
        try:
            embedding, duration_seconds = self.embedding_from_int16(audio_int16)
        except ValueError as exc:
            return IdentificationResult('unknown', 0.0, 0.0, False, reason=str(exc))
        except Exception as exc:
            return IdentificationResult('unknown', 0.0, 0.0, False, reason=f'Embedding failed: {exc}')
        if not self.speakers:
            return IdentificationResult('unknown', 0.0, duration_seconds, False, reason='voice DB is empty')
        best_name = 'unknown'
        best_score = -1.0
        for name, entry in self.speakers.items():
            for reference in entry.get('embeddings', []):
                score = float(np.dot(reference, embedding))
                if score > best_score:
                    best_score = score
                    best_name = name
        matched = best_score >= self.threshold
        return IdentificationResult(best_name if matched else 'unknown', best_score, duration_seconds, matched, None if matched else f'below threshold {self.threshold:.3f}', )

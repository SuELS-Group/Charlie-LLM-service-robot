"""Audio conditioning shared by the local TTS backends."""
from __future__ import annotations

import math

import numpy as np
import pyloudnorm as pyln
from scipy.signal import resample_poly


def resample_audio(samples, source_rate: int, target_rate: int) -> np.ndarray:
    audio = np.asarray(samples, dtype=np.float32)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    if audio.ndim != 1 or not np.isfinite(audio).all():
        raise ValueError('Expected finite mono or interleaved audio')
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError('Sample rates must be positive')
    if source_rate != target_rate and audio.size:
        divisor = math.gcd(int(source_rate), int(target_rate))
        audio = resample_poly(audio, target_rate // divisor, source_rate // divisor)
    return audio.astype(np.float32, copy=False)


def audio_metrics(samples, sample_rate: int) -> dict:
    audio = np.asarray(samples, dtype=np.float32)
    peak = float(np.max(np.abs(resample_poly(audio, 4, 1)))) if audio.size else 0.0
    loudness = -math.inf
    if len(audio) >= sample_rate // 20 and np.max(np.abs(audio)) > 1e-6:
        block = min(.4, (len(audio) - 1) / sample_rate)
        loudness = float(pyln.Meter(sample_rate, block_size=block).integrated_loudness(audio))
    return {'duration_s': len(audio) / sample_rate, 'lufs': loudness,
            'true_peak_dbfs': 20 * math.log10(peak) if peak else -math.inf}


def condition_audio(samples, source_rate: int, target_rate: int = 24000,
                    target_lufs: float = -20., peak_dbfs: float = -1.5) -> tuple[np.ndarray, dict]:
    audio = resample_audio(samples, source_rate, target_rate).copy()
    if not audio.size:
        raise ValueError('TTS returned empty audio')
    audio -= np.mean(audio)
    measured = audio_metrics(audio, target_rate)
    if math.isfinite(measured['lufs']):
        gain_db = min(18., max(-40., target_lufs - measured['lufs']))
        audio *= 10 ** (gain_db / 20.)
    ceiling = 10 ** (peak_dbfs / 20.)
    # Limit on the oversampled waveform, including peaks between PCM samples.
    upsampled = np.clip(resample_poly(audio, 4, 1), -ceiling, ceiling)
    audio = resample_poly(upsampled, 1, 4)[:len(audio)].astype(np.float32)
    fade = min(round(.004 * target_rate), len(audio) // 2)
    if fade:
        audio[:fade] *= np.linspace(0., 1., fade)
        audio[-fade:] *= np.linspace(1., 0., fade)
    true_peak = np.max(np.abs(resample_poly(audio, 4, 1)))
    if true_peak > ceiling:
        audio *= ceiling / true_peak
    return audio, audio_metrics(audio, target_rate)

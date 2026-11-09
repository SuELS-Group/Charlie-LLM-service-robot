"""Language handling shared by request processing and speech output."""
from __future__ import annotations

import json
import math
import re
from types import SimpleNamespace

from langdetect import DetectorFactory, LangDetectException, detect, detect_langs

SUPPORTED_LANGUAGES = frozenset({'de', 'en'})
SPEECH_LANGUAGE_CONFIDENCE = 0.99
SPEECH_LANGUAGE_MAX_CHARS = 2000


def supported_language(value: str | None, fallback: str = 'en') -> str:
    value = str(value or '').strip().lower()
    if value in SUPPORTED_LANGUAGES:
        return value
    return fallback if fallback in SUPPORTED_LANGUAGES else 'en'


def request_language(text: str, previous: str = 'en', *, language_hint: str | None = None) -> str:
    text = re.sub(r'^\s*\[User [^\]]+\]\s*:\s*', '', text or '', flags=re.I)
    text = re.sub(r'^\s*charlie\b[\s,:.!?-]*', '', text, flags=re.I).strip()
    if language_hint in SUPPORTED_LANGUAGES:
        # Forced ASR decoding language is only a hint: Whisper can emit English
        # text in its German pass (and vice versa). Correct clear mismatches.
        return speech_language(text, language_hint)
    fallback = supported_language(previous)
    # Short confirmations and names carry too little evidence for redetection.
    if len(text) < 6:
        return fallback
    try:
        DetectorFactory.seed = 0
        return supported_language(detect(text), fallback)
    except (LangDetectException, ValueError):
        return fallback


def warmup_speech_language_detection() -> bool:
    """Load the bundled CPU language profiles before the first speech request."""
    try:
        DetectorFactory.seed = 0
        detect_langs('This sentence initializes the local speech language detector.')
        return True
    except LangDetectException:
        return False


def _confident_speech_language(text: str) -> str | None:
    # Names, numbers and short confirmations are poor evidence for a voice switch.
    if sum(char.isalpha() for char in text) < 30 or len(re.findall(r'[^\W\d_]+', text)) < 6:
        return None
    candidates = detect_langs(text)
    if candidates and candidates[0].prob >= SPEECH_LANGUAGE_CONFIDENCE:
        return candidates[0].lang
    return None


def speech_language(text: str, language_hint: str | None = None) -> str:
    """Correct clear German/English mismatches; retain the hint when uncertain."""
    preferred = supported_language(language_hint)
    sample = (text or '')[:SPEECH_LANGUAGE_MAX_CHARS]
    try:
        DetectorFactory.seed = 0
        detected = _confident_speech_language(sample)
        if detected not in SUPPORTED_LANGUAGES or detected == preferred:
            return preferred
        # Whole-text scores can be high even for mixed speech. Only corrections
        # need these extra checks; a clearly different half vetoes the switch.
        words = sample.split()
        middle = len(words) // 2
        for part in (' '.join(words[:middle]), ' '.join(words[middle:])):
            part_language = _confident_speech_language(part)
            if part_language is not None and part_language != detected:
                return preferred
        return detected
    except LangDetectException:
        return preferred


def parse_tts_request(raw_data: str):
    try:
        data = json.loads(raw_data)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('text'), str):
        return None
    text = data['text'].strip()
    if not text:
        return None
    issued_at = data.get('issued_at', 0.0)
    if not isinstance(issued_at, (int, float)) or not math.isfinite(issued_at):
        return None
    language_hint = supported_language(data.get('lang'))
    return SimpleNamespace(text=text, lang=speech_language(text, language_hint),
                           language_hint=language_hint, issued_at=float(issued_at))

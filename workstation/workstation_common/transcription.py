"""Pure transcription selection and explicit voice-stop detection."""
from __future__ import annotations

import json
import math
import re

from .language import request_language


def transcription_candidate(segments, language: str) -> dict:
    segments = [segment for segment in segments if str(segment.text).strip()]
    scores = [float(segment.avg_logprob) for segment in segments]
    return {'language': language,
            'text': ' '.join(str(segment.text).strip() for segment in segments),
            'score': sum(scores) / len(scores) if scores else -math.inf}


def select_transcription(candidates) -> dict:
    valid = [candidate for candidate in candidates
             if candidate['text'].strip() and math.isfinite(candidate['score'])]
    best = max(valid, key=lambda candidate: candidate['score'],
               default={'text': '', 'language': '', 'score': -math.inf})
    if not best['text']:
        return best
    return {**best, 'language': request_language(best['text'], language_hint=best['language'])}


def is_voice_stop(text: str, keywords=('stop', 'stopp'), repeat_threshold: int = 2) -> bool:
    words = re.findall(r'\w+', text.lower())
    stop_words = set(keywords)
    hits = sum(word in stop_words for word in words)
    if hits >= repeat_threshold:
        return True
    # A single standalone command is explicit; ordinary sentences keep the
    # repeated-word rule so "please don't stop" does not accidentally trigger.
    return hits > 0 and all(word in stop_words | {'charlie', 'please', 'bitte'} for word in words)


def parse_user_message(raw: str) -> tuple[str, str | None]:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return raw, None
    if isinstance(data, dict) and isinstance(data.get('text'), str):
        language = data.get('lang')
        return data['text'], language if language in ('de', 'en') else None
    return raw, None

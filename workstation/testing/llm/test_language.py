import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langdetect import LangDetectException
from workstation_common.language import parse_tts_request, request_language, speech_language, supported_language

GERMAN_REPLY = ('In meinen lokalen Unterlagen finde ich dazu keine passende Information. '
    'Bitte frage das zuständige Beratungsteam, ob es dir bei dieser Frage weiterhelfen kann.')
ENGLISH_REPLY = ('I could not find specific information in the local knowledge base about this question. '
    'Please ask a member of staff for confirmation.')


@pytest.mark.parametrize('text', [
    'Ja',
    '[User TestUserC]:\n Charlie ja',
])
def test_short_replies_keep_conversation_language(text):
    assert request_language(text, 'de') == 'de'
    assert request_language(text, 'en') == 'en'


def test_detection_strips_identity_and_wake_word(monkeypatch):
    seen = []
    monkeypatch.setattr('workstation_common.language.detect', lambda text: seen.append(text) or 'de')
    assert request_language('[User TestUserC]: Charlie, wo ist das Büro?', 'en') == 'de'
    assert seen == ['wo ist das Büro?']


def test_unsupported_detection_keeps_supported_language(monkeypatch):
    monkeypatch.setattr('workstation_common.language.detect', lambda _: 'nl')
    assert request_language('long enough reply', 'de') == 'de'
    assert supported_language('', '') == 'en'


@pytest.mark.parametrize('lang', [
    'nl',
    None,
])
def test_nonempty_tts_reply_gets_supported_voice(lang):
    request = parse_tts_request(json.dumps({'text': 'Ready.', 'lang': lang}))
    assert request.text == 'Ready.'
    assert request.lang == 'en'


@pytest.mark.parametrize('payload', [
    'null',
    'invalid',
    '{"text": null}',
    '{"text": " "}',
])
def test_malformed_tts_requests_are_rejected(payload):
    assert parse_tts_request(payload) is None


@pytest.mark.parametrize(('text', 'hint', 'expected'), [
    (GERMAN_REPLY, 'en', 'de'), (ENGLISH_REPLY, 'de', 'en'),
    (GERMAN_REPLY, 'de', 'de'), (ENGLISH_REPLY, 'en', 'en'),
])
def test_tts_voice_matches_clear_text_without_translating_or_renewing_timestamp(text, hint, expected):
    request = parse_tts_request(json.dumps({'text': text, 'lang': hint, 'issued_at': 1234.5}))
    assert request.lang == expected
    assert request.language_hint == hint
    assert request.text == text and request.issued_at == 1234.5


@pytest.mark.parametrize('text', [
    'Ja',
    'Ready.',
    'Example Person',
])
@pytest.mark.parametrize('hint', ['de', 'en'])
def test_short_or_name_only_speech_keeps_hint_without_running_detector(monkeypatch, text, hint):
    monkeypatch.setattr('workstation_common.language.detect_langs',
        lambda _: pytest.fail('Short speech must not trigger language detection'))
    assert speech_language(text, hint) == hint


@pytest.mark.parametrize('candidates', [
    [SimpleNamespace(lang='de', prob=0.98)],
    [SimpleNamespace(lang='fr', prob=0.99999)],
])
def test_uncertain_or_unsupported_speech_language_keeps_hint(monkeypatch, candidates):
    monkeypatch.setattr('workstation_common.language.detect_langs', lambda _: candidates)
    assert speech_language(GERMAN_REPLY, 'en') == 'en'


def test_speech_detector_failure_keeps_hint(monkeypatch):
    monkeypatch.setattr('workstation_common.language.detect_langs',
        Mock(side_effect=LangDetectException(0, 'profiles unavailable')))
    assert speech_language(GERMAN_REPLY, 'en') == 'en'


@pytest.mark.parametrize('hint', ['de', 'en'])
def test_mixed_language_text_does_not_override_hint_even_with_high_overall_score(hint):
    text = ('Ich fahre jetzt zum Eingang und warte dort auf dich. '
            'I will go to the entrance now and wait there for you.')
    assert speech_language(text, hint) == hint


def test_name_heavy_text_keeps_explicit_hint():
    assert speech_language('Charlie, TestUserA, TestUserB, UserGamma, UserDelta, UserEpsilon.', 'de') == 'de'


def test_detection_input_is_bounded_but_spoken_text_is_preserved(monkeypatch):
    samples = []
    def detect(text):
        samples.append(text)
        return [SimpleNamespace(lang='de', prob=1.0)]
    monkeypatch.setattr('workstation_common.language.detect_langs', detect)
    text = GERMAN_REPLY * 30
    request = parse_tts_request(json.dumps({'text': text, 'lang': 'en'}))
    assert request.text == text and request.lang == 'de'
    assert samples and all(len(sample) <= 2000 for sample in samples)

import math
from types import SimpleNamespace

import pytest
from workstation_common.transcription import (is_voice_stop, parse_user_message,
    select_transcription, transcription_candidate)


def candidate(text, score=-.3, language='de'):
    return transcription_candidate([SimpleNamespace(text=text, avg_logprob=score)], language)


def test_empty_candidate_cannot_suppress_a_stop():
    result = select_transcription([candidate('', 0), candidate('Charlie stop stop', -.3, 'en')])
    assert result['text'] == 'Charlie stop stop'
    assert result['language'] == 'en'


def test_score_is_not_penalized_for_extra_segments():
    split = transcription_candidate([SimpleNamespace(text='Charlie', avg_logprob=-.2),
                                     SimpleNamespace(text='stopp', avg_logprob=-.2)], 'de')
    assert split['score'] == candidate('Charlie stopp', -.2)['score']
    assert select_transcription([candidate('less likely', -.3), split]) == split


def test_all_empty_or_invalid_scores_yield_no_text():
    assert select_transcription([candidate(''), candidate('noise', math.nan)])['text'] == ''


@pytest.mark.parametrize(('text', 'hint', 'expected'), [
    ('Okay, Charlie, please drive to the grabbing point and turn 90 degrees right, Charlie.', 'de', 'en'),
    ('Charlie, fahre bitte zum Eingang und warte dort auf mich.', 'en', 'de'),
    ('Charlie ja', 'de', 'de'),
])
def test_selected_transcript_language_matches_text_without_changing_words_or_score(text, hint, expected):
    result = select_transcription([candidate(text, -.2, hint), candidate('noise', -.9)])
    assert result == {'text': text, 'score': -.2, 'language': expected}


@pytest.mark.parametrize('text', [
    'stop',
    'stopp!',
    'Charlie, stop.',
])
def test_explicit_stop_requests_do_not_need_a_wake_word(text):
    assert is_voice_stop(text)


@pytest.mark.parametrize('text', [
    '',
    "please don't stop",
    'where is the bus stop?',
])
def test_ordinary_sentences_do_not_trigger_single_word_stop(text):
    assert not is_voice_stop(text)


def test_asr_language_metadata_and_legacy_text_inputs():
    assert parse_user_message('{"text": "Charlie ja", "lang": "de"}') == ('Charlie ja', 'de')
    assert parse_user_message('Charlie ja') == ('Charlie ja', None)
    assert parse_user_message('{"text": "hello", "lang": "nl"}') == ('hello', None)
    assert parse_user_message('[]') == ('[]', None)

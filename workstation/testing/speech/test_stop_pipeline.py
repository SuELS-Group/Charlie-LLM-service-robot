import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest
from workstation_common.config import load

root = Path(__file__).resolve().parents[3]
def source_config(package, name=None, **_kwargs):
    folder = 'robot/ros_ws/src' if package == 'tts_playback_node' else 'workstation/ament_ws/src'
    return load(root / folder / package / 'config' / ((name or package) + '.yaml'))

with patch('workstation_common.get_config', side_effect=source_config):
    from tts_synthesis_node.tts_synthesis_node import TTSSynthesisNode
    from transcriber_node.transcriber_node import TranscriberNode
    from battery_node.battery_node import BatteryNode
with patch('robot_common.get_config', side_effect=source_config):
    from tts_playback_node.tts_playback_node import TTSPlaybackNode


def stop(stamp=200):
    return SimpleNamespace(stamp=SimpleNamespace(sec=stamp, nanosec=0))


@pytest.mark.parametrize('text', ['Old response',
    'Ich fahre jetzt zum Eingang und warte dort auf dich. Bitte folge mir zum Eingang.'])
def test_stop_during_synthesis_discards_computed_audio(text):
    n = SimpleNamespace(log=Mock(), _stop_lock=threading.Lock(), _last_stop_time=100,
        parse_request=TTSSynthesisNode.parse_request, normalize_text=TTSSynthesisNode.normalize_text,
        encode_wav=TTSSynthesisNode.encode_wav, audio_client=Mock())
    engine = Mock()
    def generate(*_args, **_kwargs):
        TTSSynthesisNode.global_stop_callback(n, stop())
        return SimpleNamespace(samples=np.array([.1, -.1]))
    engine.generate.side_effect = generate
    n.engines = {'en': engine, 'de': engine}
    TTSSynthesisNode.listener_callback(n, SimpleNamespace(data=json.dumps(
        {'text': text, 'lang': 'en', 'issued_at': 150})))
    engine.generate.assert_called_once()
    n.audio_client.call_async.assert_not_called()


@pytest.mark.parametrize(('text', 'hint', 'expected'), [
    ('Ich habe keine passende Information in der lokalen Wissensdatenbank gefunden. Bitte frage im Büro nach.', 'en', 'de'),
    ('I could not find the requested information in the local knowledge base. Please ask a member of staff.', 'de', 'en'),
])
def test_synthesis_selects_corrected_voice_and_retains_request_time(text, hint, expected):
    engines = {'de': Mock(), 'en': Mock()}
    for engine in engines.values():
        engine.generate.return_value = SimpleNamespace(samples=np.array([.1, -.1]))
    n = SimpleNamespace(log=Mock(), _stop_lock=threading.Lock(), _last_stop_time=100,
        parse_request=TTSSynthesisNode.parse_request, normalize_text=TTSSynthesisNode.normalize_text,
        encode_wav=TTSSynthesisNode.encode_wav, audio_client=Mock(), engines=engines)
    TTSSynthesisNode.listener_callback(n, SimpleNamespace(data=json.dumps(
        {'text': text, 'lang': hint, 'issued_at': 150})))
    engines[expected].generate.assert_called_once()
    engines[hint].generate.assert_not_called()
    assert n.audio_client.call_async.call_args.args[0].issued_at == 150
    n.log.info.assert_any_call(f'TTS language corrected from {hint} to {expected} based on text')


def test_corrected_language_cannot_revive_speech_issued_before_stop():
    engines = {'de': Mock(), 'en': Mock()}
    n = SimpleNamespace(log=Mock(), _stop_lock=threading.Lock(), _last_stop_time=200,
        parse_request=TTSSynthesisNode.parse_request, engines=engines, audio_client=Mock())
    TTSSynthesisNode.listener_callback(n, SimpleNamespace(data=json.dumps({
        'text': 'Ich fahre jetzt zum Eingang und warte dort auf dich. Bitte folge mir zum Eingang.',
        'lang': 'en', 'issued_at': 150})))
    for engine in engines.values():
        engine.generate.assert_not_called()
    n.audio_client.call_async.assert_not_called()


def test_playback_rejects_audio_issued_before_stop():
    n = SimpleNamespace(log=Mock(), _last_stop_time=200, buffer_lock=threading.Lock(),
                        audio_buffer=np.array([], dtype=np.int16))
    wav = TTSSynthesisNode.encode_wav(np.array([10, 20], dtype=np.int16), 22050)
    response = TTSPlaybackNode.play_audio_callback(n, SimpleNamespace(wav_data=list(wav), issued_at=150), SimpleNamespace())
    assert not response.success and len(n.audio_buffer) == 0
    response = TTSPlaybackNode.play_audio_callback(n, SimpleNamespace(wav_data=list(wav), issued_at=250), SimpleNamespace())
    assert response.success and len(n.audio_buffer) > 0


def test_transcriber_stop_clears_buffer_and_queued_speech():
    n = SimpleNamespace(_input_lock=threading.Lock(), _stop_generation=0,
                        _last_stop_time=0,
                        speech_buffer=[np.array([1])], transcription_queue=queue.Queue())
    n.transcription_queue.put(('old', 0, 150))
    TranscriberNode.global_stop_callback(n, stop())
    assert n._stop_generation == 1 and not n.speech_buffer
    assert n.transcription_queue.empty() and n.transcription_queue.unfinished_tasks == 0


def transcriber(speaker=None):
    import time
    now = time.time()
    n = SimpleNamespace(log=Mock(), _input_lock=threading.Lock(), _stop_generation=0,
        _last_stop_time=0, _last_window_opened=0, _clarification_window={
            'request_id': 'task-1', 'speaker': speaker, 'opened_at': now - 1, 'expires_at': now + 20},
        publisher_=Mock(), global_stop_publisher=Mock(), transcription_queue=queue.Queue())
    segment = SimpleNamespace(text='Ja', avg_logprob=-.1)
    n.whisper_model = SimpleNamespace(transcribe=lambda *_a, **_k: ([segment], None))
    n._maybe_prefix_transcript = Mock(side_effect=lambda text, **_kw: f'[User {speaker}]: {text}' if speaker else text)
    n.clarification_reply_id = lambda wake, stamp: TranscriberNode.clarification_reply_id(n, wake, stamp)
    n.transcribe_segment = lambda *args: TranscriberNode.transcribe_segment(n, *args)
    return n, now


@pytest.mark.parametrize('speaker', ['testuserc', None])
def test_asr_requires_charlie_for_all_speakers_by_default(speaker):
    n, now = transcriber(speaker)
    n.transcribe_segment(np.array([1], dtype=np.int16), 0, now)
    n.publisher_.publish.assert_not_called()


@pytest.mark.parametrize('speaker', ['TestUserB', None])
def test_asr_binds_charlie_reply_without_waiting_for_a_matching_identity(speaker):
    n, now = transcriber('TestUserD')
    segment = SimpleNamespace(text='Hey, Charlie grabbing point', avg_logprob=-.1)
    n.whisper_model.transcribe = lambda *_a, **_k: ([segment], None)
    n._maybe_prefix_transcript = Mock(side_effect=lambda text, **_kw: f'[User {speaker}]: {text}' if speaker else text)
    n.transcribe_segment(np.array([1], dtype=np.int16), 0, now)
    payload = json.loads(n.publisher_.publish.call_args.args[0].data)
    assert payload['clarification_id'] == 'task-1'
    assert 'grabbing point' in payload['text']
    n._maybe_prefix_transcript.assert_called_once_with('Hey, Charlie grabbing point', wait_for_speaker=False)


def test_transcription_exception_does_not_kill_the_worker_or_leak_queue_tasks(monkeypatch):
    import transcriber_node.transcriber_node as module
    n, now = transcriber('testuserc')
    n._clarification_window['allow_without_wake_word'] = True
    n.transcription_queue.put((np.array([1], dtype=np.int16), 0, now))
    n.transcription_queue.put((np.array([2], dtype=np.int16), 0, now))
    original = n.transcribe_segment
    def segment(*args):
        if n.transcribe_segment.call_count == 1:
            raise RuntimeError('offline fault')
        return original(*args)
    n.transcribe_segment = Mock(side_effect=segment)
    running = iter([True, True, False])
    monkeypatch.setattr(module.rclpy, 'ok', lambda: next(running))
    TranscriberNode.transcription_worker(n)
    assert n.transcribe_segment.call_count == 2 and n.transcription_queue.unfinished_tasks == 0
    n.publisher_.publish.assert_called_once()
    n.log.exception.assert_called_once()


def test_delayed_pre_stop_clarification_window_cannot_reopen_listening():
    n, now = transcriber()
    n._last_stop_time = now
    window = {'active': True, 'request_id': 'old', 'speaker': None, 'issued_at': now - 2,
              'opened_at': now - 1, 'expires_at': now + 20}
    n._clarification_window = None
    TranscriberNode.clarification_callback(n, SimpleNamespace(data=json.dumps(window)))
    assert n._clarification_window is None


def test_battery_events_are_telemetry_without_a_user_request():
    n = SimpleNamespace(_status='unknown',log=Mock(),event_publisher=Mock(),
        _battery_snapshot=lambda:{'percentage':85.3,'voltage':12.,'sampled_at':123.,
            'present':True,'is_charging':False,'charging_state':'unknown'},
        compute_battery_status=lambda _pct:'green')
    BatteryNode.check_battery(n)
    raw = n.event_publisher.publish.call_args.args[0].data
    payload = json.loads(raw)
    assert payload['source'] == 'battery_monitor' and payload['issued_at'] > 0
    assert payload['type']=='battery_state' and payload['level']=='green'
    assert 'text' not in payload and payload['charger_connected'] is True

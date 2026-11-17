import json
import math
import queue
import re
import threading
import time
from dataclasses import dataclass

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Int16MultiArray, String
from workstation_common import configure_logging, get_config, get_logger, log_summary
from workstation_common.transcription import is_voice_stop
from workstation_common.interaction import matching_voice_window
from workstation_common.speech_models import SpeechRecognizer

from global_stop.msg import GlobalStop

config = get_config('transcriber_node', profile_key='model_profiles')
log = get_logger()


@dataclass
class AudioTurn:
    samples: np.ndarray
    generation: int
    issued_at: float
    turn_id: str
    during_playback: bool = False


class TranscriberNode(Node):

    def __init__(self) -> None:
        super().__init__('transcriber_node')
        self.log = log
        self.recognizer = SpeechRecognizer(config, self.log)
        self.log.info('Model loaded.')
        self.speech_buffer = []
        self._recording_turn_id = None
        self._capture_started_at = 0.
        self._closed_turn_id = None
        self._during_playback = False
        self._playback_requests = {}
        self.transcription_queue = queue.Queue()
        self._input_lock = threading.Lock()
        self._stop_generation = 0
        self._last_stop_time = 0.0
        self._clarification_window = None
        self._last_window_opened = 0.0
        self._speaker_lock = threading.Lock()
        self._speaker_update_event = threading.Event()
        self._last_speaker_name: str | None = None
        self._last_speaker_update_ts: float = 0.0
        self.publisher_ = self.create_publisher(String, config.topics.llm_input, 10)
        self.log.info(f'Listening on {config.topics.audio}, publishing transcriptions to {config.topics.llm_input}, '
                      f'global stop to {config.topics.global_stop}')
        self.log.info(f'Language candidates: {", ".join(config.language.candidates)}')
        audio_qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST, depth=128, )
        self.subscription = self.create_subscription(Int16MultiArray, config.topics.audio, self.audio_listener_callback, audio_qos, )
        self.speaker_subscription = self.create_subscription(String, config.topics.identified_speaker, self.speaker_callback, 10, )
        stop_qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST, depth=1, durability=DurabilityPolicy.VOLATILE, )
        self.global_stop_publisher = self.create_publisher(GlobalStop, config.topics.global_stop, stop_qos)
        self.global_stop_subscription = self.create_subscription(GlobalStop, config.topics.global_stop, self.global_stop_callback, stop_qos)
        self.clarification_subscription = self.create_subscription(String,
            getattr(config.topics, 'clarification', '/llm/clarification_window'), self.clarification_callback, 10)
        self.playback_subscription = self.create_subscription(String,
            config.topics.playback, self.playback_callback, 50)
        self.transcription_thread = threading.Thread(target=self.transcription_worker, daemon=True)
        self.transcription_thread.start()

    def speaker_callback(self, msg: String) -> None:
        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError:
            self.log.debug(f'Ignored non-JSON speaker payload: {msg.data!r}')
            return
        if not isinstance(payload, dict):
            return
        if not payload.get('matched'):
            return
        speaker_name = payload.get('speaker_name')
        if not speaker_name:
            return
        with self._speaker_lock:
            self._last_speaker_name = str(speaker_name)
            self._last_speaker_update_ts = time.monotonic()
        self._speaker_update_event.set()

    def _get_recent_speaker_name(self) -> str | None:
        with self._speaker_lock:
            if not self._last_speaker_name:
                return None
            age_s = time.monotonic() - self._last_speaker_update_ts
            if age_s > float(config.speaker.max_age_s):
                return None
            return self._last_speaker_name

    def _maybe_prefix_transcript(self, transcript: str, *, wait_for_speaker: bool = True) -> str:
        speaker_name = self._get_recent_speaker_name()
        if wait_for_speaker and speaker_name is None and float(config.speaker.wait_timeout_s) > 0:
            self._speaker_update_event.wait(timeout=float(config.speaker.wait_timeout_s))
            self._speaker_update_event.clear()
            speaker_name = self._get_recent_speaker_name()
        if speaker_name:
            return f"[User {speaker_name}]:\n {transcript}"
        return transcript

    def audio_listener_callback(self, msg: Int16MultiArray) -> None:
        try:
            metadata = json.loads(msg.layout.dim[0].label) if msg.layout.dim else {}
        except (ValueError, TypeError, IndexError):
            return
        if not isinstance(metadata, dict):
            return
        turn_id = metadata.get('turn_id')
        onset = metadata.get('started_at')
        if (not isinstance(turn_id, str) or not 0 < len(turn_id) <= 128 or
                metadata.get('sample_rate') != 16000 or
                not isinstance(onset, (int, float)) or not math.isfinite(onset) or onset <= 0):
            return
        with self._input_lock:
            if (turn_id == self._closed_turn_id or onset <= self._last_stop_time or
                    onset < self._capture_started_at):
                return
            if turn_id != self._recording_turn_id:
                self.speech_buffer = []
                self._recording_turn_id = turn_id
                self._stop_generation += 1
                self._capture_started_at = onset
                now = time.monotonic()
                self._playback_requests = {key: expiry for key, expiry in self._playback_requests.items() if expiry > now}
                self._during_playback = bool(self._playback_requests)
            if (metadata.get('cancelled') or
                    metadata.get('final') and metadata.get('approved') is not True):
                self.speech_buffer = []
                self._closed_turn_id = turn_id
                return
            if msg.data:
                self.speech_buffer.append(np.array(msg.data, dtype=np.int16))
                return
            if metadata.get('final') is not True:
                return
            if not self.speech_buffer:
                return
            full_speech = np.concatenate(self.speech_buffer)
            self.speech_buffer = []
            self._closed_turn_id = turn_id
            self.log.info(f'Queued speech segment ({len(full_speech) / 16000:.2f}s)')
            self.transcription_queue.put(AudioTurn(full_speech, self._stop_generation,
                self._capture_started_at, turn_id, self._during_playback))

    def playback_callback(self, message):
        try:
            event = json.loads(message.data)
        except (ValueError, TypeError):
            return
        if not isinstance(event, dict) or not isinstance(event.get('request_id'), str):
            return
        with self._input_lock:
            if event.get('event') == 'started':
                self._playback_requests[event['request_id']] = time.monotonic() + 120
            elif (event.get('event') in ('cancelled', 'failed') or
                    event.get('event') == 'finished' and event.get('final_segment') is True):
                self._playback_requests.pop(event['request_id'], None)

    def global_stop_callback(self, message):
        with self._input_lock:
            self._stop_generation += 1
            stamp = getattr(message, 'stamp', None)
            stopped_at = stamp.sec + stamp.nanosec / 1e9 if stamp is not None else time.time()
            self._last_stop_time = max(self._last_stop_time, stopped_at or time.time())
            self._clarification_window = None
            self.speech_buffer = []
            self._recording_turn_id = None
            self._closed_turn_id = None
            self._playback_requests.clear()
            while True:
                try:
                    self.transcription_queue.get_nowait()
                    self.transcription_queue.task_done()
                except queue.Empty:
                    break

    def clarification_callback(self, message):
        try:
            window = json.loads(message.data)
        except (ValueError, TypeError):
            return
        if not isinstance(window, dict) or not isinstance(window.get('request_id'), str):
            return
        with self._input_lock:
            if window.get('active') is not True:
                if self._clarification_window and self._clarification_window['request_id'] == window['request_id']:
                    self._clarification_window = None
                return
            times = [window.get(key) for key in ('issued_at', 'opened_at', 'expires_at')]
            if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in times):
                return
            issued_at, opened_at, expires_at = times
            if (issued_at <= self._last_stop_time or opened_at <= self._last_window_opened or
                    expires_at <= time.time() or expires_at > opened_at + 30):
                return
            self._clarification_window = window
            self._last_window_opened = opened_at

    def clarification_reply_id(self, wake_word_detected, issued_at):
        with self._input_lock:
            window = self._clarification_window
            if matching_voice_window(window, wake_word_detected, issued_at, time.time()):
                return window['request_id']
            return None

    def transcription_worker(self) -> None:
        while rclpy.ok():
            try:
                turn = self.transcription_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.transcribe_segment(turn)
            except Exception as exc:
                self.log.exception(f'Transcription failed for this segment: {exc}')
            finally:
                self.transcription_queue.task_done()

    def transcribe_segment(self, turn):
        if turn.generation != self._stop_generation:
            return
        with self._input_lock:
            window = self._clarification_window
            language = window.get('language') if matching_voice_window(
                window, False, turn.issued_at, time.time()) else None
        started = time.monotonic()
        best_result = self.recognizer.transcribe(turn.samples.astype(np.float32) / 32768., language=language)
        if turn.generation != self._stop_generation:
            return
        text = best_result['text']
        self.log.info(f'ASR {self.recognizer.name}: {time.monotonic() - started:.3f}s, '
                      f'language={best_result["language"]}, text={text!r}')
        words = re.findall(r'\w+', text.lower())
        if is_voice_stop(text, config.language.stop_keywords, config.language.global_stop_repeat_threshold):
            with self._input_lock:
                if turn.generation != self._stop_generation:
                    return
                self.global_stop_publisher.publish(self._build_global_stop_message(text))
            self.global_stop_callback(None)
            return
        if turn.during_playback:
            self.log.info('Ignored speech captured during robot playback')
            return
        if best_result['language'] not in config.language.candidates:
            self.log.info('Ignored transcript outside the configured conversation languages')
            return
        wake_word = any(word in words for word in config.language.wake_words)
        with self._input_lock:
            window = self._clarification_window
            awaiting = bool(window and window['expires_at'] > time.time())
        if not text or not (wake_word or awaiting):
            return
        reply_id = self.clarification_reply_id(wake_word, turn.issued_at)
        if not wake_word and reply_id is None:
            return
        llm_text = self._maybe_prefix_transcript(text, wait_for_speaker=reply_id is None)
        if reply_id:
            self.log.info(f'Clarification reply matched request_id={reply_id}')
        payload = {'text': llm_text, 'lang': best_result['language'], 'issued_at': turn.issued_at,
                   'source': 'speech', 'turn_id': turn.turn_id, 'final': True, 'approved': True}
        if reply_id:
            payload['clarification_id'] = reply_id
        with self._input_lock:
            if turn.generation == self._stop_generation:
                self.publisher_.publish(String(data=json.dumps(payload)))

    def destroy_node(self):
        self.recognizer.close()
        super().destroy_node()

    def _build_global_stop_message(self, transcript: str) -> GlobalStop:
        now = self.get_clock().now()
        message = GlobalStop()
        message.stamp = now.to_msg()
        message.reason = 'voice_stop'
        return message


def main(args=None) -> None:
    configure_logging()
    rclpy.init(args=args)
    log_summary(config, log, 'model.size', 'model.compute_type')
    node = TranscriberNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

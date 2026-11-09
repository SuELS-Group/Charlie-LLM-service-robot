import json
import os
import threading
import time
from io import BytesIO
from pathlib import Path

import numpy as np
import rclpy
import sounddevice as sd
import soundfile as sf
from ament_index_python.packages import get_package_share_directory
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from robot_common import get_config, get_logger
from scipy.signal import resample
from std_msgs.msg import String

from global_stop.msg import GlobalStop
from tts_playback_interfaces.srv import PlayAudio

config = get_config('tts_playback_node', Path(__file__).stem)


class TTSPlaybackNode(Node):

    def __init__(self):
        super().__init__('tts_playback_node')
        self.log = get_logger()
        self.create_service(PlayAudio, config.services.play_audio, self.play_audio_callback)
        stop_qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST, depth=config.qos.stop_depth, durability=DurabilityPolicy.VOLATILE)
        self.create_subscription(GlobalStop, config.topics.global_stop, self.stop_callback, stop_qos)
        self.create_subscription(String, config.topics.control_led, self.on_control_led, config.qos.control_led_depth)
        self.audio_buffer = np.array([], dtype=np.int16)
        self.buffer_lock = threading.Lock()
        self._last_stop_time = 0.0
        self._sound_looping = False
        self._effect_buffer = np.array([], dtype=np.float32)
        self._effect_lock = threading.Lock()
        self._thinking_buffer = np.array([], dtype=np.float32)
        self._thinking_lock = threading.Lock()
        fb = self._load_sound('feedback-sound.mp3')
        self._feedback_sound = fb if fb is not None else np.array([], dtype=np.float32)
        self._thinking_sounds_dict = {}
        for fname in ['thinking-sound.mp3']:
            snd = self._load_sound(fname)
            stem = fname[:-4]  # strip .mp3
            self._thinking_sounds_dict[stem] = snd if snd is not None else np.array([], dtype=np.float32)
        self._thinking_sound = self._resolve_thinking_sound()
        self.stream = None
        self.start_stream()
        self.log.info(f'TTS playback ready on service {config.services.play_audio} '
                      f'({config.audio.sample_rate} Hz, {config.audio.channels} channel)')

    def _resolve_thinking_sound(self):
        selected = getattr(config, 'thinking_sound', 'thinking-sound')
        snd = self._thinking_sounds_dict.get(selected)
        if snd is not None and len(snd) > 0:
            return snd
        self.log.warning(f'Configured thinking sound "{selected}" not found, falling back to first available')
        for s in self._thinking_sounds_dict.values():
            if len(s) > 0:
                return s
        return np.array([], dtype=np.float32)

    def _load_sound(self, filename):
        package_path = get_package_share_directory('tts_playback_node')
        filepath = os.path.join(package_path, filename)
        try:
            data, sr = sf.read(filepath, dtype='float32')
            if data.ndim > 1:
                data = data.mean(axis=1)
            target_sr = config.audio.sample_rate
            if sr != target_sr:
                num_samples = int(len(data) * target_sr / sr)
                data = resample(data, num_samples).astype(np.float32)
            self.log.info(f'Loaded {filename}: {sr}Hz -> {target_sr}Hz, {len(data)} samples')
            return data
        except Exception as e:
            self.log.warning(f'Failed to load {filename}: {e}')
            return None

    def play_audio_callback(self, request: PlayAudio.Request, response: PlayAudio.Response):
        if not request.wav_data:
            response.success = False
            response.message = 'No WAV data received'
            return response
        try:
            wav, sample_rate = sf.read(BytesIO(bytes(request.wav_data)), dtype='float32')
        except Exception as exc:
            response.success = False
            response.message = f'Failed to decode WAV data: {exc}'
            self.log.error(response.message)
            return response
        if wav.ndim > 1:
            wav = wav.mean(axis=1)
        target_sample_rate = config.audio.sample_rate
        if sample_rate != target_sample_rate:
            num_samples = int(len(wav) * target_sample_rate / sample_rate)
            wav = resample(wav, num_samples).astype(np.float32)
        wav = np.clip(wav, -1.0, 1.0)
        samples = (wav * 32767.0).astype(np.int16)
        if len(samples) == 0:
            response.success = False
            response.message = 'Decoded WAV data contained no samples'
            return response
        with self.buffer_lock:
            issued_at = getattr(request, 'issued_at', 0.0)
            if issued_at and issued_at <= self._last_stop_time:
                response.success = False
                response.message = 'Audio from before the global stop was discarded'
                return response
            self.audio_buffer = np.concatenate((self.audio_buffer, samples))
        response.success = True
        response.message = f'Queued {len(samples)} samples'
        self.log.info(response.message)
        return response

    def on_control_led(self, msg):
        json_string = msg.data
        json_data = json.loads(json_string)
        status = json_data["status"]
        self.log.info(f'Received LED status: {status}')
        match status:
            case "idle":
                self._stop_sound_loop()
                if config.enable_feedback_sounds:
                    self.play_sound()
            case "speech":
                self._stop_sound_loop()
                if config.enable_feedback_sounds:
                    self.play_sound()
            case "thinking":
                if config.enable_feedback_sounds:
                    self._start_sound_loop()
            case "think":
                if config.enable_feedback_sounds:
                    self._start_sound_loop()
            case "action":
                self._stop_sound_loop()
                if config.enable_feedback_sounds:
                    self.play_sound()
            case "unlock":
                self._stop_sound_loop()
            case _:
                self.log.info(f'Unsupported LED status: {status}')

    def _start_sound_loop(self):
        self._sound_looping = True

    def _stop_sound_loop(self):
        self._sound_looping = False
        with self._thinking_lock:
            self._thinking_buffer = np.array([], dtype=np.float32)

    def play_sound(self):
        with self._effect_lock:
            self._effect_buffer = self._feedback_sound.copy()

    def stop_callback(self, msg: GlobalStop):
        self.log.warning(f'Global stop received: {msg.reason}')
        with self.buffer_lock:
            stamp = msg.stamp.sec + msg.stamp.nanosec / 1e9
            self._last_stop_time = max(self._last_stop_time, stamp or time.time())
            self.audio_buffer = np.array([], dtype=np.int16)
        self._sound_looping = False
        with self._effect_lock:
            self._effect_buffer = np.array([], dtype=np.float32)
        with self._thinking_lock:
            self._thinking_buffer = np.array([], dtype=np.float32)

    def start_stream(self):
        def callback(outdata, frames, time_info, status):
            del time_info
            if status:
                self.log.warning(str(status))
            tts = np.zeros(frames, dtype=np.float32)
            tts_had_data = False
            with self.buffer_lock:
                if len(self.audio_buffer) > 0:
                    n = min(len(self.audio_buffer), frames)
                    tts[:n] = self.audio_buffer[:n].astype(np.float32) / 32768.0
                    self.audio_buffer = self.audio_buffer[n:]
                    tts_had_data = True
            effect = np.zeros(frames, dtype=np.float32)
            with self._effect_lock:
                if len(self._effect_buffer) > 0:
                    n = min(len(self._effect_buffer), frames)
                    vol = max(0.0, min(1.0, getattr(config, 'sounds_volume', 70) / 100.0))
                    effect[:n] = self._effect_buffer[:n] * vol
                    self._effect_buffer = self._effect_buffer[n:]
            thinking = np.zeros(frames, dtype=np.float32)
            if not tts_had_data:
                with self._thinking_lock:
                    if len(self._thinking_buffer) == 0 and self._sound_looping:
                        self._thinking_buffer = self._thinking_sound.copy()
                    if len(self._thinking_buffer) > 0:
                        n = min(len(self._thinking_buffer), frames)
                        vol = max(0.0, min(1.0, getattr(config, 'sounds_volume', 70) / 100.0))
                        thinking[:n] = self._thinking_buffer[:n] * vol
                        self._thinking_buffer = self._thinking_buffer[n:]
            outdata[:, 0] = np.clip(tts + effect + thinking, -1.0, 1.0)

        self.stream = sd.OutputStream(samplerate=config.audio.sample_rate, channels=config.audio.channels, dtype=config.audio.dtype, callback=callback)
        self.stream.start()
        self.log.info('Audio output stream started')

    def destroy_node(self):
        self.log.info('Shutting down TTS playback node')
        if self.stream:
            self.stream.stop()
            self.stream.close()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = TTSPlaybackNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

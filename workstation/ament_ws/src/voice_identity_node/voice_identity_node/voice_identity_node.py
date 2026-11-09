from __future__ import annotations

import json
import queue
import threading

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Int16MultiArray, String
from workstation_common import configure_logging, get_config, get_logger

from voice_identity_node.speaker_processor import SpeakerProcessor

config = get_config('voice_identity_node')
log = get_logger()


class VoiceIdentityNode(Node):
    """Identify known speakers from speech chunks emitted by voice_detection_node."""

    def __init__(self) -> None:
        super().__init__('voice_identity_node')
        self.log = log
        self.processor = SpeakerProcessor(config.voice_db.path, model_source=config.model.source, model_savedir=config.model.savedir, device=config.model.device, threshold=config.matching.threshold, sample_rate=config.audio.sample_rate, min_duration_seconds=config.audio.min_duration_seconds, max_duration_seconds=config.audio.max_duration_seconds, logger=self.log, )
        self._rebuild_voice_db_on_start()
        self.speech_buffer: list[np.ndarray] = []
        self.identity_queue: queue.Queue[np.ndarray] = queue.Queue()
        qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST, depth=128)
        self.subscription = self.create_subscription(Int16MultiArray, config.topics.audio, self.audio_listener_callback, qos, )
        self.publisher = self.create_publisher(String, config.topics.identified_speaker, 10)
        self.log.info(f'Listening on {config.topics.audio}, publishing speaker identities to '
                      f'{config.topics.identified_speaker}')
        self.log.info(f'Voice DB: {self.processor.db_path}')
        self.log.info(f'Loaded {len(self.processor.speakers)} known speakers')
        self.worker_thread = threading.Thread(target=self.identity_worker, daemon=True)
        self.worker_thread.start()

    def _rebuild_voice_db_on_start(self) -> None:
        """Build voice_db.json from sample files whenever the node starts.
        This keeps the runtime DB aligned with files managed through the web UI.
        Bad sample files are reported by the builder but do not prevent the node from starting.
        """
        sample_config = getattr(config, 'voice_samples', None)
        sample_dir = getattr(sample_config, 'dir', '$PROJECT_PATH/controller/static/voice-samples')
        extensions = set(getattr(sample_config, 'extensions', ['.wav',
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
                                                               '.wma']))
        ignore_prefixes = tuple(getattr(sample_config, 'ignore_prefixes', ['_', '.']))
        try:
            result = self.processor.regenerate_from_directory(sample_dir, extensions=extensions, ignore_prefixes=ignore_prefixes, )
        except Exception as exc:
            self.log.error(f'Voice DB auto-build failed: {exc}')
            self.processor.load_db()
            return
        failed = result.get('failed') or []
        if failed:
            self.log.warning(f"Voice DB auto-built with {result.get('sample_count', 0)} samples, "
                             f"{len(failed)} failed: {failed}")
        else:
            self.log.info(f"Voice DB auto-built from {sample_dir}: "
                          f"{result.get('speaker_count', 0)} speakers, {result.get('sample_count', 0)} samples")

    def audio_listener_callback(self, msg: Int16MultiArray) -> None:
        if msg.data:
            self.speech_buffer.append(np.asarray(msg.data, dtype=np.int16))
            return
        if not self.speech_buffer:
            return
        full_speech = np.concatenate(self.speech_buffer)
        self.speech_buffer = []
        self.log.info(f'Queued speaker-ID segment ({len(full_speech) / config.audio.sample_rate:.2f}s)')
        self.identity_queue.put(full_speech)

    def identity_worker(self) -> None:
        while rclpy.ok():
            try:
                audio_data = self.identity_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            result = self.processor.identify_int16(audio_data).as_dict()
            self.publisher.publish(String(data=json.dumps(result)))
            if result['matched']:
                self.log.info(f"Speaker identified: {result['speaker_name']} ({result['speaker_score']:.3f})")
            else:
                self.log.info(f"Speaker unknown ({result['speaker_score']:.3f}); "
                              f"reason={result.get('reason', 'no match')}")
            self.identity_queue.task_done()


def main(args=None) -> None:
    configure_logging()
    rclpy.init(args=args)
    node = VoiceIdentityNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

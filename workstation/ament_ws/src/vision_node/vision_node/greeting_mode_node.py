from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import rclpy
from action_interfaces.srv import ExecuteAction
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
from tool_status.msg import ToolStatus as RosToolStatus
from workstation_common import get_config, get_logger

config = get_config('vision_node', 'greeting_mode_node')
log = get_logger('vision.greeting')


class GreetingModeNode(Node):

    def __init__(self) -> None:
        super().__init__('greeting_mode_node')
        self.log = log
        self.enabled = False
        self.state_path = self._resolve_project_path(str(config.state.path))
        self.state_mtime = None
        self.last_greeted_by_name: dict[str, float] = {}
        self.detection_history: dict[str, list[float]] = {}
        self.last_global_greeting = 0.0
        self.greeting_active = False

        self.speech_pub = self.create_publisher(String, config.topics.speech, 10)
        self.action_client = self.create_client(ExecuteAction, config.services.execute_action)

        vision_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.BEST_EFFORT,
        )

        self.create_subscription(
            String,
            config.topics.vision,
            self._vision_callback,
            vision_qos,
        )

        self.create_timer(float(config.state.poll_period_s), self._poll_state)
        self._load_state(force=True)

        self.log.info(
            f'Greeting mode node ready: enabled={self.enabled} state={self.state_path} '
            f'vision={config.topics.vision} speech={config.topics.speech} action={config.services.execute_action}'
        )

    @staticmethod
    def _resolve_project_path(raw_path: str) -> Path:
        raw = Path(raw_path)
        if raw.is_absolute():
            return raw

        env_project = os.environ.get('PROJECT_PATH')
        if env_project:
            return Path(env_project).expanduser() / raw

        cwd_candidate = Path.cwd() / raw
        if cwd_candidate.exists() or cwd_candidate.parent.exists():
            return cwd_candidate

        here = Path(__file__).resolve()
        for parent in here.parents:
            candidate = parent / raw
            if candidate.exists() or candidate.parent.exists():
                return candidate

        return Path.home() / 'pg-llm-service-robot' / raw

    def _poll_state(self) -> None:
        self._load_state(force=False)

    def _load_state(self, force: bool = False) -> None:
        try:
            stat = self.state_path.stat()
        except FileNotFoundError:
            if force or self.enabled:
                self.enabled = False
                self.log.info('Greeting mode disabled because state file is missing')
            return
        except Exception as exc:
            self.log.warning(f'Could not stat greeting mode state file: {exc}')
            return

        if not force and self.state_mtime == stat.st_mtime:
            return

        self.state_mtime = stat.st_mtime

        try:
            data = json.loads(self.state_path.read_text(encoding='utf-8'))
        except Exception as exc:
            self.log.warning(f'Could not read greeting mode state file: {exc}')
            return

        previous = self.enabled
        self.enabled = bool(data.get('enabled', False))

        if force or previous != self.enabled:
            self.log.info(f'Greeting mode enabled={self.enabled}')

    def _vision_callback(self, msg: String) -> None:
        if not self.enabled or self.greeting_active:
            return

        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError:
            return

        detections = payload.get('detections') or []
        candidates = self._known_face_candidates(detections)

        if not candidates:
            return

        candidates.sort(key=lambda item: item.get('face_confidence', 1.0))

        for candidate in candidates:
            name = candidate['face_name']
            if self._confirm_detection(name):
                self._maybe_greet(name)
                return

    def _known_face_candidates(self, detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = []
        max_distance = float(config.recognition.max_face_distance)

        for det in detections:
            candidate_name = str(
                det.get('face_candidate_name') or '').strip()
            if candidate_name:
                name = candidate_name
                confidence = self._coerce_float(
                    det.get('face_candidate_distance'))
                if confidence is None:
                    continue
            else:
                name = str(det.get('face_name') or '').strip()
                confidence = self._coerce_float(
                    det.get('face_distance', det.get('face_confidence')))

            if not name or name.lower() == 'unknown':
                continue

            if confidence is not None and confidence > max_distance:
                continue

            item = dict(det)
            item['face_name'] = name
            item['face_confidence'] = confidence if confidence is not None else 1.0
            candidates.append(item)

        return candidates

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _confirm_detection(self, name: str) -> bool:
        now = time.monotonic()
        ttl = float(config.recognition.detection_ttl_s)
        min_confirmations = int(config.recognition.min_confirmations)

        history = [ts for ts in self.detection_history.get(name, []) if now - ts <= ttl]
        history.append(now)
        self.detection_history[name] = history

        return len(history) >= max(1, min_confirmations)

    def _maybe_greet(self, name: str) -> None:
        now = time.monotonic()
        global_cooldown = float(config.greeting.global_cooldown_s)
        person_cooldown = float(config.greeting.per_person_cooldown_s)

        if now - self.last_global_greeting < global_cooldown:
            return

        if now - self.last_greeted_by_name.get(name, 0.0) < person_cooldown:
            return

        self.last_global_greeting = now
        self.last_greeted_by_name[name] = now
        self.detection_history[name] = []

        self._publish_greeting(name)
        self._call_wave_action(name)

    def _publish_greeting(self, name: str) -> None:
        template = str(config.greeting.template)
        text = template.format(name=name)
        payload = {'text': text, 'lang': str(config.greeting.language), 'issued_at': time.time()}

        self.speech_pub.publish(String(data=json.dumps(payload)))
        self.log.info(f'Greeting published for {name}: {text}')

    def _call_wave_action(self, name: str) -> None:
        if not self.action_client.wait_for_service(timeout_sec=float(config.action.service_wait_timeout_s)):
            self.log.warning(f'Greeting for {name} skipped wave: action service unavailable')
            return

        request = ExecuteAction.Request()
        request.action_type = str(config.action.type)
        request.parameters = '{}'

        self.greeting_active = True
        future = self.action_client.call_async(request)
        future.add_done_callback(lambda done: self._wave_done(name, done))

        self.log.info(f'Wave action requested for greeting {name}')

    def _wave_done(self, name: str, future: Any) -> None:
        try:
            response = future.result()
            status = getattr(getattr(response, 'status', None), 'status', None)
            message = getattr(getattr(response, 'status', None), 'message', '')

            if status == RosToolStatus.STATUS_SUCCESS:
                self.log.info(f'Greeting wave completed for {name}: {message}')
            else:
                self.log.warning(f'Greeting wave failed for {name}: status={status} message={message}')
        except Exception as exc:
            self.log.warning(f'Greeting wave failed for {name}: {exc}')
        finally:
            self.greeting_active = False


def main(args=None) -> None:
    rclpy.init(args=args)
    node = GreetingModeNode()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

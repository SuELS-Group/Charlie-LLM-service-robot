from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

import rclpy
from diagnostic_msgs.msg import DiagnosticArray
from geometry_msgs.msg import Twist
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from sensor_msgs.msg import BatteryState
from std_msgs.msg import Bool, String
from std_srvs.srv import Trigger
from workstation_common.battery_announcements import (
    ANNOUNCEMENT_TOPIC, DEFAULT_SETTINGS_FILE, BatteryAnnouncementSettings,
)

from .battery_estimator import BatteryEstimator, EstimatorConfig
from .battery_diagnostics import charging_measurement

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except Exception:
    websockets = None
    ConnectionClosed = Exception
DIRECTION_TO_TWIST = {'forward': (1.0, 0.0), 'backward': (-1.0, 0.0), 'left': (0.0, 1.0), 'right': (0.0, -1.0), }
ALLOWED_MODES = {'navigation', 'position'}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ClientSession:
    client_id: str
    websocket: Any
    robot_id: str
    ros_domain_id: Optional[int]
    connected_at: str


class WebBridgeNode(Node):

    def __init__(self) -> None:
        super().__init__('web_bridge_node')
        expected_domain = os.getenv('ROS_DOMAIN_ID', '').strip()
        expected_domain_value = int(expected_domain) if expected_domain.isdigit() else -1
        self.declare_parameter('host', '0.0.0.0')
        self.declare_parameter('port', 8766)
        self.declare_parameter('websocket_path', '/ws/robot-control')
        self.declare_parameter('voice_activity_websocket_path', '/ws/voice-activity')
        self.declare_parameter('http_port', 8767)
        self.declare_parameter('llm_input_path', '/api/llm-input')
        self.declare_parameter('cmd_vel_topic', '/stretch/cmd_vel')
        self.declare_parameter('navigation_mode_service', '/switch_to_navigation_mode')
        self.declare_parameter('position_mode_service', '/switch_to_position_mode')
        self.declare_parameter('llm_input_topic', 'robot_to_llm_communication')
        self.declare_parameter('voice_activity_topic', 'voice_activity')
        self.declare_parameter('battery_topic', '/battery')
        self.declare_parameter('battery_diagnostics_topic', '/diagnostics')
        self.declare_parameter('battery_announcement_settings_file', DEFAULT_SETTINGS_FILE)
        for name, value in vars(EstimatorConfig()).items():
            if name == 'voltage_curve':
                value = [number for pair in value for number in pair]
            self.declare_parameter(f'battery_{name}', value)
        self.declare_parameter('linear_speed', 0.5)
        self.declare_parameter('angular_speed', 0.45)
        self.declare_parameter('publish_period_seconds', 0.033)
        self.declare_parameter('watchdog_timeout_seconds', 0.75)
        self.declare_parameter('mode_switch_timeout_seconds', 3.0)
        self.declare_parameter('expected_ros_domain_id', expected_domain_value)
        self.host = str(self.get_parameter('host').value).strip()
        self.port = int(self.get_parameter('port').value)
        self.websocket_path = str(self.get_parameter('websocket_path').value).strip()
        self.voice_activity_websocket_path = str(self.get_parameter('voice_activity_websocket_path').value).strip()
        self.http_port = int(self.get_parameter('http_port').value)
        self.llm_input_path = str(self.get_parameter('llm_input_path').value).strip()
        self.cmd_vel_topic = str(self.get_parameter('cmd_vel_topic').value).strip()
        self.navigation_mode_service = str(self.get_parameter('navigation_mode_service').value).strip()
        self.position_mode_service = str(self.get_parameter('position_mode_service').value).strip()
        self.llm_input_topic = str(self.get_parameter('llm_input_topic').value).strip()
        self.voice_activity_topic = str(self.get_parameter('voice_activity_topic').value).strip()
        self.linear_speed = float(self.get_parameter('linear_speed').value)
        self.angular_speed = float(self.get_parameter('angular_speed').value)
        self.publish_period_seconds = max(0.01, float(self.get_parameter('publish_period_seconds').value))
        self.watchdog_timeout_seconds = max(0.1, float(self.get_parameter('watchdog_timeout_seconds').value))
        self.mode_switch_timeout_seconds = max(0.5, float(self.get_parameter('mode_switch_timeout_seconds').value), )
        self.expected_ros_domain_id = int(self.get_parameter('expected_ros_domain_id').value)
        self._lock = threading.Lock()
        self._battery_announcement_settings = BatteryAnnouncementSettings(
            self.get_parameter('battery_announcement_settings_file').value)
        self._battery_announcement_publisher = self.create_publisher(String, ANNOUNCEMENT_TOPIC,
            QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                       durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self._battery_announcement_publisher.publish(String(data=self._battery_announcement_settings.mode))
        battery_config = {name: self.get_parameter(f'battery_{name}').value for name in vars(EstimatorConfig())}
        curve = battery_config['voltage_curve']
        if len(curve) % 2:
            raise ValueError('battery_voltage_curve must contain voltage/percentage pairs')
        battery_config['voltage_curve'] = tuple(zip(curve[::2], curve[1::2]))
        self._battery_estimator = BatteryEstimator(EstimatorConfig(**battery_config))
        self._last_battery_message = None
        self._last_charging_measurement = None
        self._battery_subscription = self.create_subscription(
            BatteryState, str(self.get_parameter('battery_topic').value), self._on_battery, 1)
        self._battery_diagnostics_subscription = self.create_subscription(
            DiagnosticArray, str(self.get_parameter('battery_diagnostics_topic').value),
            self._on_battery_diagnostics, qos_profile_sensor_data)
        self._voice_active = False
        self._voice_activity_received = False
        self._voice_activity_updated_at: Optional[str] = None
        self._last_voice_available = False
        self._publisher = self.create_publisher(Twist, self.cmd_vel_topic, 10)
        self._llm_input_publisher = self.create_publisher(String, self.llm_input_topic, 10)
        activity_qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL, history=HistoryPolicy.KEEP_LAST, depth=1, )
        self._voice_activity_subscription = self.create_subscription(Bool, self.voice_activity_topic, self._on_voice_activity, activity_qos)
        self._nav_mode_client = self.create_client(Trigger, self.navigation_mode_service)
        self._position_mode_client = self.create_client(Trigger, self.position_mode_service)
        self._sessions: dict[str, ClientSession] = {}
        self._voice_activity_clients: set[Any] = set()
        self._owner_client_id: Optional[str] = None
        self._active_direction: Optional[str] = None
        self._current_mode: Optional[str] = None
        self._last_command_monotonic = 0.0
        self._last_twist = Twist()
        self._status_level = 'info'
        self._status_message = 'Ready'
        self._status_updated_at = _utc_now_iso()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._http_server: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._ready = threading.Event()
        self.create_timer(self.publish_period_seconds, self._publish_active_command_tick)
        self.create_timer(0.05, self._watchdog_tick)
        self.create_timer(0.5, self._voice_activity_availability_tick)
        self.create_timer(1.0, self._battery_tick)
        self.get_logger().info(f'Web bridge ready on {self.cmd_vel_topic}, llm_input={self.llm_input_topic}, '
                               f'voice_activity={self.voice_activity_topic}; websocket '
                               f'ws://{self.host}:{self.port}{self.websocket_path}; http '
                               f'http://{self.host}:{self.http_port}{self.llm_input_path}')

    def _on_voice_activity(self, message: Bool) -> None:
        with self._lock:
            self._voice_active = bool(message.data)
            self._voice_activity_received = True
            self._voice_activity_updated_at = _utc_now_iso()
        self._broadcast_voice_activity()

    def _on_battery(self, message: BatteryState) -> None:
        with self._lock:
            self._last_battery_message = message

    def _battery_tick(self) -> None:
        with self._lock:
            message = self._last_battery_message
            if message is None:
                return
            states = {0: 'unknown', 1: 'charging', 2: 'discharging', 3: 'not charging', 4: 'full'}
            power = self._last_charging_measurement
            sampled_at = float(message.header.stamp.sec) + float(message.header.stamp.nanosec) * 1e-9
            fresh_power = (power and -.5 <= time.time() - power['sampled_at'] <= 3.
                           and abs(sampled_at - power['sampled_at']) <= 2.
                           and power['charger_connected'] == bool(message.present)
                           and power['charging'] == (message.power_supply_status == 1))
            self._battery_estimator.update(
                voltage=float(message.voltage), current=float(message.current),
                charger_connected=bool(message.present),
                charging_state=states.get(message.power_supply_status, 'unknown'),
                sampled_at=sampled_at,
                charging_current=power['charging_current'] if fresh_power else None)

    def _on_battery_diagnostics(self, message: DiagnosticArray) -> None:
        power = charging_measurement(message, now=time.time())
        if power is None:
            return
        with self._lock:
            if self._last_charging_measurement is None or power['sampled_at'] > self._last_charging_measurement['sampled_at']:
                self._last_charging_measurement = power

    def _serialize_battery(self) -> dict[str, Any]:
        with self._lock:
            return {**self._battery_estimator.snapshot(),
                    'announcement_mode': self._battery_announcement_settings.mode}

    def _set_battery_announcements(self, payload) -> dict[str, str]:
        if not isinstance(payload, dict) or set(payload) != {'mode'}:
            raise ValueError('Expected a battery announcement mode')
        with self._lock:
            self._battery_announcement_settings.save(payload['mode'])
            self._battery_announcement_publisher.publish(String(data=self._battery_announcement_settings.mode))
            return {'mode': self._battery_announcement_settings.mode}

    def start_server(self) -> None:
        if websockets is None:
            raise RuntimeError('The "websockets" package is not installed')
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_server, name='web-bridge-ws', daemon=True)
        self._thread.start()
        self._start_http_server()
        if not self._ready.wait(timeout=2.0):
            self.get_logger().warning('Web bridge websocket server did not report readiness within 2s')

    def shutdown(self) -> None:
        self._clear_owner_and_stop('Web bridge shutting down')
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
            self._http_server = None
        if self._http_thread is not None:
            self._http_thread.join(timeout=2.0)
            self._http_thread = None
        if self._loop and not self._loop.is_closed() and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _start_http_server(self) -> None:
        if self._http_thread and self._http_thread.is_alive():
            return
        node = self

        class Handler(BaseHTTPRequestHandler):

            def do_OPTIONS(self) -> None:
                self._send_json(HTTPStatus.NO_CONTENT, {})

            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                if path == '/api/health':
                    self._send_json(HTTPStatus.OK, {'status': 'ok', 'llm_input_path': node.llm_input_path})
                    return
                if path == '/api/voice-activity':
                    self._send_json(HTTPStatus.OK, node._serialize_voice_activity())
                    return
                if path == '/api/battery':
                    self._send_json(HTTPStatus.OK, node._serialize_battery())
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {'error': 'Not found'})

            def do_POST(self) -> None:
                path = urlsplit(self.path).path
                if path not in {node.llm_input_path, '/api/battery/announcements'}:
                    self._send_json(HTTPStatus.NOT_FOUND, {'error': 'Not found'})
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0') or '0')
                except ValueError:
                    self._send_json(HTTPStatus.BAD_REQUEST, {'error': 'Invalid Content-Length'})
                    return
                if length <= 0:
                    self._send_json(HTTPStatus.BAD_REQUEST, {'error': 'Request body is required'})
                    return
                if length > 65536:
                    self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {'error': 'Request body is too large'})
                    return
                try:
                    payload = json.loads(self.rfile.read(length).decode('utf-8'))
                except Exception:
                    self._send_json(HTTPStatus.BAD_REQUEST, {'error': 'Invalid JSON payload'})
                    return
                if path == '/api/battery/announcements':
                    try:
                        result = node._set_battery_announcements(payload)
                    except ValueError as exc:
                        self._send_json(HTTPStatus.BAD_REQUEST, {'error': str(exc)})
                        return
                    except OSError:
                        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {'error': 'Cannot save battery announcement mode'})
                        return
                    self._send_json(HTTPStatus.OK, result)
                    return
                if not isinstance(payload, dict):
                    self._send_json(HTTPStatus.BAD_REQUEST, {'error': 'Expected a JSON object'})
                    return
                error = node._publish_llm_input(str(payload.get('text', '')))
                if error:
                    self._send_json(HTTPStatus.BAD_REQUEST, {'error': error})
                    return
                self._send_json(HTTPStatus.OK, {'status': 'sent'})

            def log_message(self, _format: str, *args: Any) -> None:
                return

            def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                body = b'' if status == HTTPStatus.NO_CONTENT else json.dumps(payload).encode('utf-8')
                self.send_response(int(status))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type')
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

        self._http_server = ThreadingHTTPServer((self.host, self.http_port), Handler)
        self._http_thread = threading.Thread(target=self._http_server.serve_forever, name='web-bridge-http', daemon=True)
        self._http_thread.start()
        self.get_logger().info(f'Web bridge HTTP listening on http://{self.host}:{self.http_port}{self.llm_input_path}')

    def _run_server(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as exc:
            self.get_logger().error(f'Web bridge websocket server failed: {exc}')
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    async def _serve(self) -> None:
        self._stop_event = asyncio.Event()
        async with websockets.serve(self._handle_client, self.host, self.port, ping_interval=20, ping_timeout=20, max_size=32768, ):
            self.get_logger().info(f'Web bridge websocket listening on ws://{self.host}:{self.port}{self.websocket_path}')
            self._ready.set()
            await self._stop_event.wait()
            await self._close_all_clients()

    async def _close_all_clients(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            voice_activity_clients = list(self._voice_activity_clients)
        clients = [session.websocket for session in sessions] + voice_activity_clients
        for client in clients:
            try:
                await client.close(code=1001, reason='Server shutdown')
            except Exception:
                continue

    async def _handle_client(self, websocket: Any, path: Optional[str] = None) -> None:
        request = getattr(websocket, 'request', None)
        request_path = path or getattr(request, 'path', None) or getattr(websocket, 'path', '/')
        parsed = urlsplit(request_path)
        if parsed.path == self.voice_activity_websocket_path:
            await self._handle_voice_activity_client(websocket)
            return
        if parsed.path != self.websocket_path:
            await websocket.close(code=1008, reason='Unsupported path')
            return
        robot_id, ros_domain_id, error_message = self._parse_query(parsed.query)
        if error_message:
            await websocket.close(code=1008, reason=error_message)
            return
        client_id = secrets.token_urlsafe(8)
        session = ClientSession(client_id=client_id, websocket=websocket, robot_id=robot_id, ros_domain_id=ros_domain_id, connected_at=_utc_now_iso(), )
        with self._lock:
            self._sessions[client_id] = session
        await self._send_state(session)
        self._broadcast_state()
        try:
            async for raw_message in websocket:
                try:
                    payload = json.loads(raw_message)
                except json.JSONDecodeError:
                    await websocket.send(json.dumps({'type': 'error', 'message': 'Invalid JSON payload'}))
                    continue
                error = await asyncio.to_thread(self._apply_message, client_id, payload)
                if error:
                    await websocket.send(json.dumps({'type': 'error', 'message': error}))
                elif str(payload.get('type') or '').strip() in ('llm_input', 'llm-input', 'llmInput'):
                    await websocket.send(json.dumps({'type': 'llm_input_sent'}))
                await self._send_state(session)
                self._broadcast_state(exclude_client_id=client_id)
        except ConnectionClosed:
            pass
        except Exception as exc:
            self.get_logger().warning(f'Web bridge client error: {exc}')
        finally:
            self._disconnect_client(client_id)

    async def _handle_voice_activity_client(self, websocket: Any) -> None:
        with self._lock:
            self._voice_activity_clients.add(websocket)
        try:
            await self._send_voice_activity(websocket)
            async for _message in websocket:
                pass
        except ConnectionClosed:
            pass
        except Exception as exc:
            self.get_logger().debug(f'Voice activity client error: {exc}')
        finally:
            with self._lock:
                self._voice_activity_clients.discard(websocket)

    def _parse_query(self, query: str) -> tuple[str, Optional[int], Optional[str]]:
        params = parse_qs(query)
        robot_id = ((params.get('robot_id') or params.get('robotId') or [''])[0]).strip()
        ros_domain_raw = ((params.get('ros_domain_id') or params.get('rosDomainId') or [''])[0]).strip()
        if not robot_id:
            return '', None, 'robot_id is required'
        ros_domain_id: Optional[int] = None
        if ros_domain_raw:
            try:
                ros_domain_id = int(ros_domain_raw)
            except ValueError:
                return '', None, 'ros_domain_id must be an integer'
        if self.expected_ros_domain_id >= 0 and ros_domain_id not in (None, self.expected_ros_domain_id):
            return '', None, f'ROS domain mismatch, expected {self.expected_ros_domain_id}'
        return robot_id, ros_domain_id, None

    def _apply_message(self, client_id: str, payload: dict[str, Any]) -> Optional[str]:
        message_type = str(payload.get('type') or '').strip()
        if message_type == 'heartbeat':
            return self._touch_owner(client_id)
        if message_type == 'stop':
            return self._stop_for_client(client_id, 'Stopped')
        if message_type == 'motion':
            direction = payload.get('direction')
            if direction is None:
                return self._stop_for_client(client_id, 'Stopped')
            return self._set_direction_for_client(client_id, str(direction))
        if message_type == 'mode':
            mode = payload.get('mode')
            if mode is None:
                return 'mode is required'
            return self._set_mode_for_client(client_id, str(mode))
        if message_type in ('llm_input', 'llm-input', 'llmInput'):
            text = payload.get('text')
            if text is None:
                return 'text is required'
            return self._publish_llm_input(str(text))
        return 'Unsupported message type'

    def _publish_llm_input(self, text: str) -> Optional[str]:
        message = text.strip()
        if not message:
            return 'Message is empty'
        if len(message) > 8000:
            return 'Message is too long'
        self._llm_input_publisher.publish(String(data=json.dumps({'text': message, 'issued_at': time.time()})))
        with self._lock:
            self._set_status_locked('info', 'Sent message to LLM')
        self.get_logger().info(f'Published web UI LLM input: {message}')
        return None

    def _serialize_voice_activity(self) -> dict[str, Any]:
        publisher_available = self.count_publishers(self.voice_activity_topic) > 0
        with self._lock:
            received = self._voice_activity_received
            active = self._voice_active
            updated_at = self._voice_activity_updated_at
        available = publisher_available and received
        return {
            'type': 'voice_activity',
            'available': available,
            'active': active if available else False,
            'topic': self.voice_activity_topic,
            'updated_at': updated_at, }

    def _voice_activity_availability_tick(self) -> None:
        available = self._serialize_voice_activity()['available']
        with self._lock:
            availability_changed = available != self._last_voice_available
            self._last_voice_available = available
        if availability_changed:
            self._broadcast_voice_activity()

    async def _send_voice_activity(self, websocket: Any) -> None:
        try:
            await websocket.send(json.dumps(self._serialize_voice_activity()))
        except Exception:
            pass

    def _broadcast_voice_activity(self) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._send_voice_activity_to_all(), self._loop)

    async def _send_voice_activity_to_all(self) -> None:
        with self._lock:
            clients = list(self._voice_activity_clients)
        for client in clients:
            await self._send_voice_activity(client)

    def _touch_owner(self, client_id: str) -> Optional[str]:
        with self._lock:
            if self._owner_client_id not in (None, client_id):
                return 'Another browser session is controlling this robot'
            self._owner_client_id = client_id
            self._last_command_monotonic = time.monotonic()
        return None

    def _set_direction_for_client(self, client_id: str, direction: str) -> Optional[str]:
        if direction not in DIRECTION_TO_TWIST:
            return f'Unsupported direction: {direction}'
        twist = self._make_twist(direction)
        with self._lock:
            error = self._claim_control_locked(client_id)
            if error:
                return error
            self._last_twist = twist
            self._active_direction = direction
            self._last_command_monotonic = time.monotonic()
            self._set_status_locked('active', f'Moving {direction}')
        self._publisher.publish(twist)
        return None

    def _stop_for_client(self, client_id: str, message: str) -> Optional[str]:
        with self._lock:
            if self._owner_client_id not in (None, client_id):
                return 'Another browser session is controlling this robot'
        self._stop_motion(message)
        return None

    def _set_mode_for_client(self, client_id: str, mode: str) -> Optional[str]:
        if mode not in ALLOWED_MODES:
            return f'Unsupported mode: {mode}'
        with self._lock:
            error = self._claim_control_locked(client_id)
            if error:
                return error
        self._stop_motion(f'Switching to {mode} mode')
        ok, message = self._call_mode_service(mode)
        with self._lock:
            if ok:
                self._current_mode = mode
                self._set_status_locked('info', message)
            else:
                self._set_status_locked('error', message)
                return message
        return None

    def _claim_control_locked(self, client_id: str) -> Optional[str]:
        if self._owner_client_id in (None, client_id):
            self._owner_client_id = client_id
            return None
        return 'Another browser session is controlling this robot'

    def _call_mode_service(self, mode: str) -> tuple[bool, str]:
        if mode == 'navigation':
            client = self._nav_mode_client
            service_name = self.navigation_mode_service
        else:
            client = self._position_mode_client
            service_name = self.position_mode_service
        deadline = time.monotonic() + self.mode_switch_timeout_seconds
        while time.monotonic() < deadline:
            try:
                if client.wait_for_service(timeout_sec=0.25):
                    break
            except Exception:
                pass
            time.sleep(0.05)
        try:
            if not client.service_is_ready():
                return False, f'{service_name} service is not available'
        except Exception:
            return False, f'{service_name} service is not available'
        done = threading.Event()
        result_box: dict[str, Any] = {}
        try:
            future = client.call_async(Trigger.Request())
        except Exception as exc:
            return False, f'Failed to call {service_name}: {exc}'

        def _on_done(done_future: Any) -> None:
            try:
                result_box['response'] = done_future.result()
            except Exception as exc:
                result_box['error'] = exc
            finally:
                done.set()

        future.add_done_callback(_on_done)
        if not done.wait(timeout=self.mode_switch_timeout_seconds):
            return False, f'Timed out waiting for {service_name}'
        if 'error' in result_box:
            return False, f'Failed to switch mode: {result_box["error"]}'
        response = result_box.get('response')
        if response is None:
            return False, f'{service_name} returned no response'
        success = bool(getattr(response, 'success', True))
        message = getattr(response, 'message', '') or f'Switched to {mode} mode'
        return success, message

    def _make_twist(self, direction: str) -> Twist:
        linear_scale, angular_scale = DIRECTION_TO_TWIST[direction]
        twist = Twist()
        twist.linear.x = linear_scale * self.linear_speed
        twist.angular.z = angular_scale * self.angular_speed
        return twist

    def _publish_active_command_tick(self) -> None:
        with self._lock:
            active_direction = self._active_direction
            twist = self._last_twist
        if active_direction is not None:
            self._publisher.publish(twist)

    def _watchdog_tick(self) -> None:
        with self._lock:
            active_direction = self._active_direction
            age = time.monotonic() - self._last_command_monotonic
        if active_direction and age > self.watchdog_timeout_seconds:
            self.get_logger().warning('Robot control heartbeat timed out, stopping robot')
            self._stop_motion('Connection timed out, robot stopped')
            self._broadcast_state()

    def _stop_motion(self, message: str) -> None:
        zero_twist = Twist()
        with self._lock:
            self._last_twist = zero_twist
            self._active_direction = None
            self._last_command_monotonic = time.monotonic()
            self._set_status_locked('info', message)
        for _ in range(3):
            self._publisher.publish(zero_twist)

    def _clear_owner_and_stop(self, message: str) -> None:
        with self._lock:
            self._owner_client_id = None
        self._stop_motion(message)

    def _disconnect_client(self, client_id: str) -> None:
        owner_left = False
        with self._lock:
            self._sessions.pop(client_id, None)
            if self._owner_client_id == client_id:
                self._owner_client_id = None
                owner_left = True
        if owner_left:
            self._stop_motion('Control session ended')
        self._broadcast_state()

    async def _send_state(self, session: ClientSession) -> None:
        try:
            await session.websocket.send(json.dumps(self._serialize_state(session.client_id)))
        except Exception:
            pass

    def _broadcast_state(self, exclude_client_id: Optional[str] = None) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._send_state_to_all(exclude_client_id), self._loop)

    async def _send_state_to_all(self, exclude_client_id: Optional[str]) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if exclude_client_id and session.client_id == exclude_client_id:
                continue
            await self._send_state(session)

    def _serialize_state(self, client_id: str) -> dict[str, Any]:
        with self._lock:
            owner_client_id = self._owner_client_id
            owner_session = self._sessions.get(owner_client_id) if owner_client_id else None
            client_session = self._sessions.get(client_id)
            has_control = owner_client_id == client_id
            controller_busy = owner_client_id is not None and owner_client_id != client_id
            target_robot_id = owner_session.robot_id if owner_session else getattr(client_session, 'robot_id', None)
            ros_domain_id = owner_session.ros_domain_id if owner_session else getattr(client_session, 'ros_domain_id', None)
            status_level = self._status_level
            status_message = self._status_message
            if controller_busy:
                status_level = 'warning'
                status_message = 'Another browser session is controlling this robot'
            elif owner_client_id is None and self._active_direction is None and status_level != 'error':
                status_level = 'info'
                status_message = 'Ready to control'
            connection_count = len(self._sessions)
            active_direction = self._active_direction
            current_mode = self._current_mode
            status_updated_at = self._status_updated_at
        return {
            'type': 'state',
            'clientId': client_id,
            'connectionCount': connection_count,
            'hasControl': has_control,
            'canControl': not controller_busy,
            'controllerBusy': controller_busy,
            'activeDirection': active_direction,
            'currentMode': current_mode,
            'statusLevel': status_level,
            'statusMessage': status_message,
            'robotId': target_robot_id,
            'rosDomainId': ros_domain_id,
            'watchdogTimeoutMs': int(self.watchdog_timeout_seconds * 1000),
            'updatedAt': status_updated_at, }

    def _set_status_locked(self, level: str, message: str) -> None:
        self._status_level = level
        self._status_message = message
        self._status_updated_at = _utc_now_iso()


def main(args: Optional[list[str]] = None) -> None:
    rclpy.init(args=args)
    node = WebBridgeNode()
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    try:
        node.start_server()
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.shutdown()
        except Exception:
            pass
        try:
            executor.shutdown()
        except Exception:
            pass
        try:
            node.destroy_node()
        except Exception:
            pass
        if rclpy.ok():
            rclpy.shutdown()

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL = 1.0
HISTORY_SIZE = 120
STATS_FILE = '/tmp/monitoring_stats.json'

SERVICE_CHECKS: List[Dict[str, str]] = [
    {'unit': 'ros2-stretch-driver', 'label': 'Stretch Driver', 'category': 'heartbeat',
     'description': 'Robot hardware heartbeat — publishes joint_states, TF, odom'},
    {'unit': 'ros2-rplidar', 'label': 'RPLidar Scanner', 'category': 'heartbeat',
     'description': 'Nav2 obstacle layer input — dropout freezes navigation'},
    {'unit': 'ros2-bringup-all', 'label': 'Nav2 Stack', 'category': 'heartbeat',
     'description': 'SLAM + AMCL + Nav2 — dead means no autonomous movement'},
    {'unit': 'ros2-camera', 'label': 'Camera Node', 'category': 'camera',
     'description': 'RealSense cameras + nav camera publishing'},
    {'unit': 'ros2-depth-mapping', 'label': 'Depth Mapping', 'category': 'camera',
     'description': '3D projection for objects and Aruco markers'},
    {'unit': 'ros2-vision', 'label': 'Vision Node', 'category': 'camera',
     'description': 'YOLO object detection pipeline'},
    {'unit': 'ros2-action-service', 'label': 'Action Service', 'category': 'communication',
     'description': 'Robot action execution service'},
    {'unit': 'ros2-person-tracking', 'label': 'Person Tracking', 'category': 'communication',
     'description': 'All person-following actions depend on this'},
    {'unit': 'ros2-hand-gesture', 'label': 'Gesture Detection', 'category': 'communication',
     'description': 'Thumbs-up and pointing detection'},
    {'unit': 'ros2-voice-detection', 'label': 'Voice Detection', 'category': 'communication',
     'description': 'Microphone voice activity detection'},
    {'unit': 'ros2-transcriber', 'label': 'Transcriber', 'category': 'communication',
     'description': 'Speech-to-text transcription (Whisper)'},
    {'unit': 'ros2-llm-agent', 'label': 'LLM Agent', 'category': 'communication',
     'description': 'Large language model planning agent'},
]


def _read_sys_stat(path: str) -> int:
    try:
        with open(path, 'r') as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def _read_sys_str(path: str) -> str:
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except OSError:
        return ''


def _query_service(unit: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    try:
        proc = subprocess.run(
            ['systemctl', '--user', 'show', unit,
             '--property=ActiveState,SubState,ActiveEnterTimestampMonotonic'],
            capture_output=True, text=True, timeout=2,
        )
        for line in proc.stdout.strip().splitlines():
            if '=' in line:
                key, _, value = line.partition('=')
                result[key] = value
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return result


class Monitor:
    def __init__(self, interface: str) -> None:
        self._interface = str(interface)
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self._net_history: deque[Dict[str, Any]] = deque(maxlen=HISTORY_SIZE)
        self._services: List[Dict[str, Any]] = []
        self._counters: Dict[str, Any] = {}
        self._prev_rx_bytes = 0
        self._prev_tx_bytes = 0
        self._prev_sample_time: float = 0.0
        self._last_active: Dict[str, float] = {}

    def _sample_network(self, now: float) -> None:
        if not self._interface:
            self._counters = {
                'interface': '', 'is_up': False, 'speed_mbps': 0,
                'rx_bytes': 0, 'tx_bytes': 0, 'rx_packets': 0, 'tx_packets': 0,
                'rx_dropped': 0, 'tx_dropped': 0, 'rx_errors': 0, 'tx_errors': 0,
            }
            return

        base = f'/sys/class/net/{self._interface}/statistics'
        rx_bytes = _read_sys_stat(f'{base}/rx_bytes')
        tx_bytes = _read_sys_stat(f'{base}/tx_bytes')
        rx_dropped = _read_sys_stat(f'{base}/rx_dropped')
        tx_dropped = _read_sys_stat(f'{base}/tx_dropped')
        rx_errors = _read_sys_stat(f'{base}/rx_errors')
        tx_errors = _read_sys_stat(f'{base}/tx_errors')
        rx_packets = _read_sys_stat(f'{base}/rx_packets')
        tx_packets = _read_sys_stat(f'{base}/tx_packets')

        is_up = _read_sys_str(f'/sys/class/net/{self._interface}/operstate').lower() == 'up'
        speed_mbps = _read_sys_stat(f'/sys/class/net/{self._interface}/speed')
        if speed_mbps < 0:
            speed_mbps = 0

        self._counters = {
            'interface': self._interface,
            'rx_bytes': rx_bytes, 'tx_bytes': tx_bytes,
            'rx_packets': rx_packets, 'tx_packets': tx_packets,
            'rx_dropped': rx_dropped, 'tx_dropped': tx_dropped,
            'rx_errors': rx_errors, 'tx_errors': tx_errors,
            'is_up': is_up, 'speed_mbps': speed_mbps,
        }

        if self._prev_sample_time > 0:
            elapsed = max(now - self._prev_sample_time, 0.001)
            rx_bytes_s = max(0.0, (rx_bytes - self._prev_rx_bytes) / elapsed)
            tx_bytes_s = max(0.0, (tx_bytes - self._prev_tx_bytes) / elapsed)
            self._net_history.append({
                'ts': now,
                'interface': self._interface,
                'is_up': is_up,
                'speed_mbps': speed_mbps,
                'rx_bytes_per_sec': round(rx_bytes_s, 1),
                'tx_bytes_per_sec': round(tx_bytes_s, 1),
                'rx_dropped': rx_dropped,
                'tx_dropped': tx_dropped,
                'rx_errors': rx_errors,
                'tx_errors': tx_errors,
            })

        self._prev_rx_bytes = rx_bytes
        self._prev_tx_bytes = tx_bytes
        self._prev_sample_time = now

    def _sample_services(self, now: float) -> None:
        results: List[Dict[str, Any]] = []
        for svc in SERVICE_CHECKS:
            unit = svc['unit']
            info = _query_service(unit)
            active = info.get('ActiveState', '') == 'active'
            substate = info.get('SubState', 'unknown')
            if active:
                self._last_active[unit] = now
            last_active = self._last_active.get(unit, 0.0)
            results.append({
                'name': unit,
                'label': svc['label'],
                'category': svc['category'],
                'description': svc['description'],
                'active': active,
                'status': substate if not active else 'active',
                'age_s': max(0.0, now - last_active) if not active else 0.0,
            })
        self._services = results

    def _write_stats_file(self) -> None:
        try:
            with open(STATS_FILE, 'w') as fh:
                json.dump(self.get_stats(), fh)
        except OSError:
            pass

    def _loop(self) -> None:
        while self._running:
            try:
                now = time.monotonic()
                with self._lock:
                    self._sample_network(now)
                    self._sample_services(now)
                self._write_stats_file()
            except Exception as exc:
                logger.debug(f'Monitor sample error: {exc}')
            time.sleep(SAMPLE_INTERVAL)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name='monitor')
        self._thread.start()
        logger.info(f'Local monitor started on {self._interface or "(no interface)"}')

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info('Local monitor stopped')

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'services': list(self._services),
                'network_history': list(self._net_history),
            }

    def get_network_counters(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._counters)


_monitor: Optional[Monitor] = None


def get_monitor() -> Optional[Monitor]:
    return _monitor


def start_monitor(interface: str) -> None:
    global _monitor
    _monitor = Monitor(interface)
    _monitor.start()


def stop_monitor() -> None:
    global _monitor
    if _monitor is not None:
        _monitor.stop()
        _monitor = None

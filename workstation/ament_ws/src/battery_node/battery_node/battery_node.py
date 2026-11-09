from __future__ import annotations

import math
import json
import time
from typing import Optional

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import BatteryState
from std_msgs.msg import String
from workstation_common import configure_logging, get_config, get_logger

from battery_interfaces.srv import GetBatteryStatus
from tool_status.msg import ToolStatus as RosToolStatus

config = get_config('battery_node')
log = get_logger()


class BatteryNode(Node):

    def __init__(self) -> None:
        super().__init__('battery_node')
        self.log = log
        self._last_battery: Optional[BatteryState] = None
        self._last_received = 0.
        self._status = 'unknown'
        self._received_battery = False
        self._warned_missing_battery = False
        self._warned_missing_percentage = False
        self.create_subscription(BatteryState, config.topics.battery, self.battery_callback, 10)
        self.create_timer(config.timers.check_period_seconds, self.check_battery)
        self.create_service(GetBatteryStatus, config.services.get_status, self.get_status_callback)
        self.event_publisher = self.create_publisher(String, config.topics.events, 1)
        self.log.info('battery_node started. '
                      f'battery_topic={config.topics.battery} '
                      f'period={config.timers.check_period_seconds:.2f}s '
                      f'anti_chatter_margin={config.thresholds.anti_chatter_margin_pct:.1f}% '
                      f'event_topic={config.topics.events} '
                      f'get_status_service={config.services.get_status}')

    def battery_callback(self, msg: BatteryState) -> None:
        self._last_battery = msg
        self._last_received = time.monotonic()
        if not self._received_battery:
            self._received_battery = True
            self._warned_missing_battery = False
            self.log.info(f'Battery stream active on {config.topics.battery} '
                          f'(voltage={float(msg.voltage):.2f}V, present={bool(msg.present)})')

    def check_battery(self) -> None:
        snapshot=self._battery_snapshot()
        status=self.compute_battery_status(snapshot['percentage']) if snapshot else 'unknown'
        if status != self._status:
            level_log = self.log.warning if status in {'orange', 'red'} else self.log.info
            level_log(f'Battery status changed: {self._status} -> {status}; snapshot={snapshot}')
            self._status = status
        event={'type':'battery_state','source':'battery_monitor','level':status,'issued_at':time.time(),
            'sampled_at':snapshot['sampled_at'] if snapshot else 0.,'sample_valid':snapshot is not None,
            'estimated_percentage':snapshot['percentage'] if snapshot else None,
            'voltage':snapshot['voltage'] if snapshot else None,
            'charger_connected':snapshot['present'] if snapshot else None,
            'charging':snapshot['is_charging'] if snapshot else None,
            'charging_state':snapshot['charging_state'] if snapshot else 'unknown'}
        self.event_publisher.publish(String(data=json.dumps(event,allow_nan=False)))

    def get_status_callback(self, _request, response):
        snapshot = self._battery_snapshot()
        if snapshot is None:
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, 'Fresh battery data is unavailable')
        self._set_status(response, RosToolStatus.STATUS_SUCCESS, 'Battery status returned')
        response.percentage = float(snapshot['percentage'])
        response.voltage = float(snapshot['voltage'])
        response.current = float(snapshot['current'])
        response.is_charging = bool(snapshot['is_charging'])
        response.present = bool(snapshot['present'])
        response.status_text = snapshot['status_text']
        response.health_check = snapshot['health_check']
        return response

    def _battery_snapshot(self) -> Optional[dict]:
        msg = self._last_battery
        if msg is None:
            return None
        captured=float(msg.header.stamp.sec)+float(msg.header.stamp.nanosec)*1e-9
        if (not 0<=time.monotonic()-self._last_received<=float(config.timers.max_sample_age_seconds) or
                not -.5<=time.time()-captured<=float(config.timers.max_sample_age_seconds)):
            return None
        pct = self.compute_percentage(msg)
        if pct is None:
            return None
        voltage = float(msg.voltage)
        try:
            current = float(msg.current)
        except (TypeError, ValueError):
            current = 0.0
        if not math.isfinite(current):
            current = 0.0
        status_map = {0: 'Unknown', 1: 'Charging', 2: 'Discharging', 3: 'Not Charging', 4: 'Full'}
        power_status = status_map.get(msg.power_supply_status, 'Unknown')
        is_charging = bool(msg.present) and msg.power_supply_status == BatteryState.POWER_SUPPLY_STATUS_CHARGING
        return {
            'percentage': round(pct, 1),
            'voltage': round(voltage, 2),
            'current': round(current, 2),
            'is_charging': is_charging,
            'sampled_at':captured,
            'charging_state':power_status.casefold(),
            'present': bool(msg.present),
            'status_text': f'{round(pct, 1)}% ({power_status})',
            'health_check': 'CRITICAL LOW' if voltage < 11.0 else 'Normal', }

    @staticmethod
    def _set_status(response, status_code: int, message: str):
        response.status.status = status_code
        response.status.message = message
        return response

    def compute_percentage(self, msg: BatteryState) -> Optional[float]:
        try:
            voltage = float(msg.voltage)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(voltage):
            return None
        if voltage >= config.voltage.green.top:
            return 100.0
        if voltage >= config.voltage.green.min:
            return 80.0 + (voltage - config.voltage.green.min) * (
                    20.0 / (config.voltage.green.top - config.voltage.green.min))
        if voltage >= config.voltage.yellow.min:
            return 60.0 + (voltage - config.voltage.yellow.min) * (
                    20.0 / (config.voltage.green.min - config.voltage.yellow.min))
        if voltage >= config.voltage.orange.min:
            return 40.0 + (voltage - config.voltage.orange.min) * (
                    20.0 / (config.voltage.yellow.min - config.voltage.orange.min))
        if voltage <= config.voltage.floor:
            return 0.0
        return (voltage - config.voltage.floor) * (40.0 / (config.voltage.orange.min - config.voltage.floor))

    def compute_battery_status(self, pct: float) -> str:
        margin = config.thresholds.anti_chatter_margin_pct
        if self._status == 'unknown':
            return self.classify_status(pct)
        if self._status == 'green':
            return 'green' if pct >= config.thresholds.levels.green_min_pct else 'yellow'
        if self._status == 'yellow':
            if pct >= config.thresholds.levels.green_min_pct + margin:
                return 'green'
            return 'yellow' if pct >= config.thresholds.levels.yellow_min_pct else 'orange'
        if self._status == 'orange':
            if pct >= config.thresholds.levels.yellow_min_pct + margin:
                return 'yellow'
            return 'orange' if pct >= config.thresholds.levels.orange_min_pct else 'red'
        if self._status == 'red':
            return 'orange' if pct >= config.thresholds.levels.orange_min_pct + margin else 'red'
        return self.classify_status(pct)

    @staticmethod
    def classify_status(pct: float) -> str:
        if pct >= config.thresholds.levels.green_min_pct:
            return 'green'
        if pct >= config.thresholds.levels.yellow_min_pct:
            return 'yellow'
        if pct >= config.thresholds.levels.orange_min_pct:
            return 'orange'
        return 'red'


def main(args=None) -> None:
    configure_logging()
    rclpy.init(args=args)
    node = BatteryNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()

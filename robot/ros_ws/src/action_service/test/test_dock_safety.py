import threading
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import action_service.dock_action as dock_module
from action_service.dock_action import DockAction
from sensor_msgs.msg import BatteryState
import pytest


def action():
    node = SimpleNamespace(log=Mock(), cb_sub=None, create_subscription=Mock())
    result = DockAction(node)
    result._stop_base = Mock()
    result._sleep_with_stop = Mock()
    return result


def sample(present=True, status=BatteryState.POWER_SUPPLY_STATUS_DISCHARGING):
    return SimpleNamespace(present=present, voltage=14.0, power_supply_status=status)


def test_contact_and_high_voltage_do_not_establish_charging(monkeypatch):
    a = action()
    a._battery_callback(sample())
    assert a._charging_evidence() == 'connected'
    monkeypatch.setattr(dock_module.config.timeouts, 'verify_seconds', 0.0)
    assert a._verify_charging() is False


def test_charging_requires_fresh_connected_evidence():
    a = action()
    a._battery_callback(sample(status=BatteryState.POWER_SUPPLY_STATUS_CHARGING))
    assert a._verify_charging()
    assert a.charge_outcome == 'charging'
    a.last_battery_monotonic = time.monotonic() - 10
    assert a._charging_evidence() == 'unavailable'


def test_full_battery_has_a_distinct_outcome():
    a = action()
    a._battery_callback(sample(status=BatteryState.POWER_SUPPLY_STATUS_FULL))
    assert a._verify_charging() and a.charge_outcome == 'full'
    a._battery_callback(sample(present=False, status=BatteryState.POWER_SUPPLY_STATUS_FULL))
    assert a._charging_evidence() == 'disconnected'


def test_missing_stow_prevents_search_and_drive(monkeypatch):
    a = action()
    a._search_for_dock = Mock()
    a._approach_dock = Mock()
    a._switch_to_navigation_mode = Mock()
    monkeypatch.setattr(dock_module.time, 'sleep', lambda _: None)
    a._run_dock_sequence(120)
    assert a.dock_result == 'stow_failed'
    a._search_for_dock.assert_not_called()
    a._approach_dock.assert_not_called()


def test_docking_waits_for_head_cleanup_before_success(monkeypatch):
    a = action()
    a._stow_robot = Mock(return_value=True)
    a._set_head_for_search = Mock()
    for name in ['_search_for_dock', '_approach_dock', '_align_with_dock', '_back_into_dock',
                 '_verify_charging', '_switch_to_navigation_mode']:
        setattr(a, name, Mock(return_value=True))
    a.motion.move_head = Mock(return_value=False)
    monkeypatch.setattr(dock_module.time, 'sleep', lambda _: None)
    a._run_dock_sequence(120)
    a.motion.move_head.assert_called_once_with(0.0, 0.0, duration=1.0, wait=True)
    assert a.dock_result == 'cleanup_failed'


def test_failed_stow_response_is_not_ignored():
    a = action()
    a.node.stow_client = SimpleNamespace(wait_for_service=lambda **_: True,
        service_is_ready=lambda: True, call_async=lambda _: SimpleNamespace(
            done=lambda: True, result=lambda: SimpleNamespace(success=False)))
    assert a._stow_robot() is False


def test_overall_deadline_stops_waiting():
    a = action()
    a._deadline = time.monotonic() - 1
    DockAction._sleep_with_stop(a, .1)
    assert a._timed_out and a._stop_evt.is_set()


@pytest.mark.parametrize('status,key', [
    (BatteryState.POWER_SUPPLY_STATUS_CHARGING, 'charging'),
    (BatteryState.POWER_SUPPLY_STATUS_FULL, 'battery_full')])
def test_dock_response_retains_charge_evidence_through_real_status_setter(status, key):
    from action_interfaces.srv import ExecuteAction
    from action_service.action_service_node import ActionServiceNode
    from tool_status.msg import ToolStatus
    a = action()
    a._battery_callback(sample(status=status))
    a.node.set_success = lambda response, message, data='': ActionServiceNode.set_status(
        response, ToolStatus.STATUS_SUCCESS, message, data)
    def finish(_timeout):
        a.dock_result = 'success'
        a._done_evt.set()
    a._run_dock_sequence = finish
    response = a.execute(SimpleNamespace(), ExecuteAction.Response())
    a._op_thread.join(timeout=1)
    assert response.status.status == ToolStatus.STATUS_SUCCESS
    assert json.loads(response.result_data)[key] is True

import json
import time
from unittest.mock import Mock

import pytest
from sensor_msgs.msg import BatteryState
from battery_node.battery_node import BatteryNode


def node():
    n=object.__new__(BatteryNode)
    n._last_battery=None;n._last_received=0.;n._status='unknown'
    n._received_battery=False;n.log=Mock();n.event_publisher=Mock()
    return n


def sample(present=False,status=BatteryState.POWER_SUPPLY_STATUS_UNKNOWN,voltage=10.98):
    msg=BatteryState()
    stamp=time.time()
    msg.header.stamp.sec=int(stamp);msg.header.stamp.nanosec=int((stamp%1)*1e9)
    msg.present=present;msg.power_supply_status=status;msg.voltage=voltage
    return msg


def test_events_are_telemetry_without_user_instructions():
    n=node();n.battery_callback(sample());n.check_battery()
    event=json.loads(n.event_publisher.publish.call_args.args[0].data)
    assert event['level']=='orange' and event['sample_valid'] and event['charger_connected'] is False
    assert event['type']=='battery_state' and 'text' not in event


def test_stale_battery_never_reports_a_fresh_status():
    n=node();n.battery_callback(sample());n._last_received-=10
    n.check_battery()
    event=json.loads(n.event_publisher.publish.call_args.args[0].data)
    assert event['level']=='unknown' and not event['sample_valid']
    assert n._battery_snapshot() is None


@pytest.mark.parametrize('present,status,expected',[(True,0,False),(True,1,True),(False,1,False),(True,4,False)])
def test_charge_flag_is_not_inferred_from_high_voltage(present,status,expected):
    n=node();n.battery_callback(sample(present,status,voltage=13.5))
    assert n._battery_snapshot()['is_charging'] is expected

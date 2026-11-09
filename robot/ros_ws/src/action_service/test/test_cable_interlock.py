from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sensor_msgs.msg import BatteryState
from stretch_core.battery_state import charging_status
from action_service.dock_action import DockCableConnected
from test_k2_motion_coordination import runtime
from test_dock_safety import action,sample


def test_charger_contact_is_telemetry_and_does_not_block_departure():
    state=runtime()
    msg=BatteryState();msg.header.stamp.sec=100;msg.present=True
    state.update_battery(msg)
    lease, error = state.acquire_base('',100.1)
    assert lease and error is None
    state.release_base(lease)
    msg.header.stamp.sec=99;msg.present=False
    state.update_battery(msg)
    assert state.charger_connected is True
    msg.header.stamp.sec=101
    state.update_battery(msg)
    assert state.acquire_base('',100.1)[0]


def test_docking_never_sends_velocity_with_connected_cable():
    a=action();a.motion=Mock();a._battery_callback(sample(present=True))
    with pytest.raises(DockCableConnected):a._send_velocity(.1,0.)
    a.motion.publish_base_velocity.assert_not_called()


@pytest.mark.parametrize('protocol,connected,charging,expected',[
    ('p5',True,True,1),('p6',True,False,3),('p6',False,True,2),('p5',None,True,0)])
def test_new_firmware_uses_real_charger_flag(protocol,connected,charging,expected):
    value=charging_status({'charger_connected':connected,'charger_is_charging':charging},
        {'hardware_id':3,'protocol_version':protocol},0,0)
    assert value==expected


def test_legacy_hardware_three_is_not_missing_from_status_update():
    assert charging_status({'charger_connected':True},{'hardware_id':3,'protocol_version':'p4'},1,0)==1


@pytest.mark.parametrize('kind', ['take_from_hand', 'give_to_person'])
def test_charger_contact_does_not_refuse_handoff(kind):
    from test_action_admission import node
    from action_interfaces.srv import ExecuteAction
    from action_service.action_service_node import ActionServiceNode
    n=node()
    handler=Mock(side_effect=lambda req, resp: n.set_failure(resp, 'Reached handler'))
    n._action_handlers[kind]=handler
    setattr(n, kind + '_action', SimpleNamespace(_arm_motion_started=False, _arm_retracted=False,
        _orientation_restored=True, _navigation_mode_restored=True))
    if kind == 'give_to_person':
        n.object_state.set('holding_assumed', 'test')
    n.k2_runtime.charger_connected=True
    request=SimpleNamespace(action_type=kind,parameters='{}')
    result=ActionServiceNode.handle_action(n,request,ExecuteAction.Response())
    handler.assert_called_once()
    assert result.status.message == 'Reached handler'


def test_charger_contact_during_a_base_lease_does_not_trigger_a_stop():
    from action_service.action_service_node import ActionServiceNode
    state = runtime()
    state.lease = 'active'
    state.charger_connected = True
    n = SimpleNamespace(k2_runtime=state, _safety_fault_lease='', log=Mock(), global_stop_pub=Mock(),
        get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=100_100_000_000)))
    ActionServiceNode._k2_watchdog(n)
    n.global_stop_pub.publish.assert_not_called()


def test_battery_callback_does_not_read_joint_fields():
    from test_action_admission import node
    from action_service.action_service_node import ActionServiceNode
    n=node();n._latest_joint_positions={'joint_lift':.4};n.grab_item_action=Mock()
    message=BatteryState();message.header.stamp.sec=110;message.present=True
    ActionServiceNode._battery_callback(n,message)
    assert n.k2_runtime.charger_connected is True
    assert n._latest_joint_positions=={'joint_lift':.4}
    n.grab_item_action.handle_joint_state.assert_not_called()


def test_joint_callback_still_updates_manipulation_feedback():
    from sensor_msgs.msg import JointState
    from test_action_admission import node
    from action_service.action_service_node import ActionServiceNode
    n=node();n.grab_item_action=Mock()
    message=JointState();message.header.stamp.sec=110
    message.name=['joint_lift'];message.position=[.5]
    ActionServiceNode._joint_callback(n,message)
    assert n._latest_joint_positions=={'joint_lift':.5}
    n.grab_item_action.handle_joint_state.assert_called_once_with(message)

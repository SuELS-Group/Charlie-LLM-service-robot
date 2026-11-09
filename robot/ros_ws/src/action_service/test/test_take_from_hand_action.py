from types import SimpleNamespace
from unittest.mock import Mock, call

import action_service.take_from_hand_action as handoff_module
import yaml
from action_service.take_from_hand_action import TakeFromHandAction


def _node():
    node = SimpleNamespace(log=Mock())
    node.log.sub.return_value = Mock()
    node.handoff_gesture_control_pub = Mock()
    node.handoff_gesture_control_pub.get_subscription_count.return_value = 1
    node.announce_handoff_ready = Mock()
    node.get_clock = lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=100000000000))
    node.set_success = Mock(side_effect=lambda response, message: ('success', message))
    node.set_failure = Mock(side_effect=lambda response, message: ('failure', message))
    node.set_stopped = Mock(side_effect=lambda response, message: ('stopped', message))
    node.set_timeout = Mock(side_effect=lambda response, message: ('timeout', message))
    return node


def _mark_retracted(action, result=True):
    action._arm_cleanup_attempted = True
    action._arm_retracted = bool(result)
    return result


def _ready_action(stow_result=True):
    node = _node()
    action = TakeFromHandAction(node)
    action.motion = Mock()
    action.motion.wait_for_traj_server.return_value = True
    action.motion.switch_to_navigation_mode.return_value = True
    action.motion.switch_to_position_mode.return_value = True
    action._wait_for_joints = Mock(return_value=True)
    action._turn_base_for_handoff = Mock(return_value=True)
    action._restore_handoff_orientation = Mock()
    action._aim_head_for_handoff = Mock(return_value=True)
    action._move_to_handoff_pose = Mock(return_value=True)
    action._wait_for_gesture = Mock(return_value={'gesture': 'thumbs_up', 'confidence': 0.99, })
    action._close_gripper = Mock(return_value=True)
    action._stow_with_object = Mock(side_effect=lambda: _mark_retracted(action, stow_result))
    action._abort_open_and_retract = Mock(side_effect=lambda: _mark_retracted(action))
    action._move_to_carry_pose = Mock(side_effect=lambda **_kwargs: _mark_retracted(action))
    return node, action


def test_pickup_clears_gestures_after_presentation():
    _node_instance, action = _ready_action()
    events = []
    real_clear = action._clear_gesture

    def clear_gesture():
        events.append('clear')
        real_clear()

    action._clear_gesture = Mock(side_effect=clear_gesture)
    action._move_to_handoff_pose.side_effect = lambda: (events.append('pose') or True)
    action._arm_gesture_detection = Mock(side_effect=lambda: events.append('arm') or True)
    _node_instance.announce_handoff_ready.side_effect = lambda _action: events.append('ready')
    action._wait_for_gesture.side_effect = lambda timeout: (
            events.append('wait') or {'gesture': 'thumbs_up', 'confidence': 0.99})
    result = action.execute(SimpleNamespace(), SimpleNamespace(), {})
    assert result == ('success', 'Gripper close completed; possession is assumed')
    assert events == ['clear', 'pose', 'clear', 'arm', 'ready', 'wait']
    action._close_gripper.assert_called_once_with()


def test_unavailable_gesture_detector_retracts_without_closing():
    _node_instance, action = _ready_action()
    action._arm_gesture_detection = Mock(return_value=False)
    result = action.execute(SimpleNamespace(), SimpleNamespace(), {})
    assert result == ('failure', 'Hand pickup failed: gesture detector unavailable')
    action._close_gripper.assert_not_called()
    _node_instance.announce_handoff_ready.assert_not_called()
    action._abort_open_and_retract.assert_called_once_with()


def test_pickup_stows_after_grasp(monkeypatch):
    _node_instance, action = _ready_action(stow_result=True)
    monkeypatch.setattr(handoff_module.config.handoff, 'stow_after_pickup', True, raising=False)
    result = action.execute(SimpleNamespace(), SimpleNamespace(), {})
    assert result == ('success', 'Gripper close completed; possession is assumed')
    action._turn_base_for_handoff.assert_called_once_with()
    action._stow_with_object.assert_called_once_with()
    action._restore_handoff_orientation.assert_called_once_with()


def test_pickup_reports_failure_when_stow_is_unconfirmed(monkeypatch):
    node, action = _ready_action(stow_result=False)
    monkeypatch.setattr(handoff_module.config.handoff, 'stow_after_pickup', True, raising=False)
    result = action.execute(SimpleNamespace(), SimpleNamespace(), {})
    assert result == ('failure', 'Gripper close accepted, but arm retraction was not confirmed')
    action._stow_with_object.assert_called_once_with()
    node.set_failure.assert_called_once()
    action._restore_handoff_orientation.assert_not_called()
    action.motion.switch_to_navigation_mode.assert_called_once_with(timeout_s=3.0)


def test_new_pickup_never_reuses_evidence_from_a_previous_close():
    _, action = _ready_action()
    action.motion.last_gripper_completion = {'gripper_contact_indicated':True, 'gripper_goal_settled':True}
    action.motion.wait_for_traj_server.return_value = False
    assert action.execute(None,SimpleNamespace(),{})[0] == 'failure'
    assert action.motion.last_gripper_completion is None
    action._close_gripper.assert_not_called()


def test_failed_retraction_does_not_rotate_extended_pickup_arm():
    _node_instance, action = _ready_action()
    action._wait_for_gesture.return_value = None
    action._abort_open_and_retract.side_effect = lambda: _mark_retracted(action, result=False)
    result = action.execute(SimpleNamespace(), SimpleNamespace(), {'timeout_seconds': 5.0})
    assert result[0] == 'timeout'
    action._restore_handoff_orientation.assert_not_called()
    action.motion.switch_to_navigation_mode.assert_called_once_with(timeout_s=3.0)


def test_handoff_turn_fails_before_motion_when_heading_is_unavailable():
    action = TakeFromHandAction(_node())
    action._get_base_heading = Mock(return_value=None)
    action._turn_base_relative = Mock()
    assert not action._turn_base_for_handoff()
    assert action._base_turn_attempted is False
    action._turn_base_relative.assert_not_called()


def test_restore_turns_to_captured_absolute_heading_and_resets_head(monkeypatch):
    action = TakeFromHandAction(_node())
    action.motion = Mock()
    action.motion.switch_to_navigation_mode.return_value = True
    action.motion.move_head.return_value = True
    action._turn_base_absolute = Mock(return_value=True)
    action._base_turned_for_handoff = True
    action._base_turn_attempted = True
    action._original_base_heading = 37.0
    action._head_moved_for_handoff = True
    base = SimpleNamespace(rotation_degrees=90.0, turn_timeout_seconds=60.0)
    restore = SimpleNamespace(pan=0.0, tilt=-0.2, move_duration=1.5)
    monkeypatch.setattr(handoff_module.config.handoff, 'base', base, raising=False)
    monkeypatch.setattr(handoff_module.config.handoff.head, 'restore', restore, raising=False)
    action._restore_handoff_orientation()
    action._turn_base_absolute.assert_called_once_with(37.0)
    action.motion.move_head.assert_called_once_with(pan=0.0, tilt=-0.2, duration=1.5, wait=True)
    action.motion.switch_to_position_mode.assert_called_once_with(timeout_s=3.0)
    assert (
            action.motion.method_calls.index(call.switch_to_position_mode(timeout_s=3.0)) < action.motion.method_calls.index(call.move_head(pan=0.0, tilt=-0.2, duration=1.5, wait=True)))
    assert not action._base_turned_for_handoff
    assert not action._base_turn_attempted
    assert not action._head_moved_for_handoff


def test_global_stop_suppresses_orientation_restore():
    action = TakeFromHandAction(_node())
    action.motion = Mock()
    action._turn_base_absolute = Mock()
    action._base_turned_for_handoff = True
    action._base_turn_attempted = True
    action._original_base_heading = 37.0
    action._head_moved_for_handoff = True
    action._stop_evt.set()
    action._restore_handoff_orientation()
    action.motion.switch_to_navigation_mode.assert_not_called()
    action.motion.move_head.assert_not_called()
    action._turn_base_absolute.assert_not_called()

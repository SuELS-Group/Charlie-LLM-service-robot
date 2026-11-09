from types import SimpleNamespace
from unittest.mock import Mock

import action_service.give_to_person_action as give_module
import yaml
from action_service.give_to_person_action import GiveToPersonAction


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


def _ready_action():
    node = _node()
    action = GiveToPersonAction(node)
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

    def open_gripper():
        action._release_attempted = True
        return True

    action._open_gripper = Mock(side_effect=open_gripper)
    action._stow_after_release = Mock(side_effect=lambda: _mark_retracted(action))
    action._retract_with_object = Mock(side_effect=lambda: _mark_retracted(action))
    return node, action


def test_handoff_presentation_preserves_object_blocked_gripper():
    action = GiveToPersonAction(_node())
    action.motion = Mock()
    action.motion.send_full_trajectory.return_value = True
    assert action._move_to_handoff_pose()
    call = action.motion.send_full_trajectory.call_args
    positions = call.args[0]
    assert give_module.config.gripper.joint not in positions
    assert call.kwargs['omit_joints'] == (give_module.config.gripper.joint,)


def test_success_clears_gestures_after_presentation_and_releases():
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
    assert result == ('success', 'Item handed to person')
    assert events == ['clear', 'pose', 'clear', 'arm', 'ready', 'wait']
    action._open_gripper.assert_called_once_with()
    action._stow_after_release.assert_called_once_with()
    action._retract_with_object.assert_not_called()
    action._restore_handoff_orientation.assert_called_once_with()


def test_unavailable_gesture_detector_retracts_without_releasing():
    _node_instance, action = _ready_action()
    action._arm_gesture_detection = Mock(return_value=False)
    result = action.execute(SimpleNamespace(), SimpleNamespace(), {})
    assert result == ('failure', 'Item handoff failed: gesture detector unavailable')
    action._open_gripper.assert_not_called()
    _node_instance.announce_handoff_ready.assert_not_called()
    action._retract_with_object.assert_called_once_with()


def test_timeout_retracts_without_opening_gripper():
    _node_instance, action = _ready_action()
    action._wait_for_gesture.return_value = None
    result = action.execute(SimpleNamespace(), SimpleNamespace(), {'timeout_seconds': 5.0})
    assert result == ('timeout', 'Item handoff timed out after 5.0s; item retained')
    action._open_gripper.assert_not_called()
    action._retract_with_object.assert_called_once_with()
    action._stow_after_release.assert_not_called()


def test_stop_while_waiting_never_opens_gripper():
    _node_instance, action = _ready_action()

    def stop_while_waiting(_timeout):
        action._stop_evt.set()
        return None

    action._wait_for_gesture.side_effect = stop_while_waiting
    result = action.execute(SimpleNamespace(), SimpleNamespace(), {})
    assert result == ('stopped', 'Item handoff stopped')
    action._open_gripper.assert_not_called()
    action._retract_with_object.assert_not_called()
    action._stow_after_release.assert_not_called()


def test_failed_retraction_does_not_rotate_extended_arm():
    _node_instance, action = _ready_action()
    action._wait_for_gesture.return_value = None
    action._retract_with_object.side_effect = lambda: _mark_retracted(action, result=False)
    result = action.execute(SimpleNamespace(), SimpleNamespace(), {})
    assert result[0] == 'timeout'
    action._restore_handoff_orientation.assert_not_called()
    action.motion.switch_to_navigation_mode.assert_called_once_with(timeout_s=3.0)

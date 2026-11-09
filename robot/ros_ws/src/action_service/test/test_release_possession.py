from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from action_service.place_item_action import PlaceItemAction
from robot_common.object_state import ObjectState
from test_take_from_hand_action import _node


def ready_place():
    action = PlaceItemAction(_node())
    action.node.set_failure.side_effect = lambda response, message, data='': ('failure', message, data)
    action.motion = Mock()
    action.motion.wait_for_traj_server.return_value = True
    action.motion.switch_to_position_mode.return_value = True
    action.motion.switch_to_navigation_mode.return_value = True
    action.motion.open_gripper.return_value = True
    action.motion.send_full_trajectory.return_value = True
    action.motion.safe_stow_arm.return_value = True
    action.motion.stow_empty_gripper.return_value = True
    action._look_at_surface_and_refresh_hint = Mock(return_value=None)
    action._hint_in_base_frame = Mock(return_value=None)
    action._final_gripper_alignment = Mock(side_effect=lambda surface, height, extension: extension)
    return action


def held_state():
    state = ObjectState()
    state.set('holding_assumed', 'completed_pickup')
    return state


@pytest.mark.parametrize('surface', ['table', 'trash_can'])
def test_confirmed_placement_and_trash_release_clear_possession(surface):
    action = ready_place()
    if surface == 'trash_can':
        action._look_at_surface_and_refresh_hint.return_value = .55
        action._hint_in_base_frame.return_value = [0., -.68, .55]
    result = action.execute(None, SimpleNamespace(), {'surface':surface})
    assert result[0] == 'success' and action._gripper_opened
    state = held_state()
    state.complete('place_item', success=True, action=action)
    assert state.snapshot()['holding_item'] is False
    action.motion.open_gripper.assert_called_once()
    action.motion.stow_empty_gripper.assert_called_once_with()
    for call in action.motion.send_full_trajectory.call_args_list:
        assert call.kwargs['omit_joints']
        assert all(joint not in call.args[0] for joint in call.kwargs['omit_joints'])


def test_failed_placement_preparation_keeps_object_and_never_opens_as_cleanup():
    action = ready_place()
    action.motion.send_full_trajectory.return_value = False
    result = action.execute(None, SimpleNamespace(), {})
    assert result[0] == 'failure' and not action._release_attempted
    action.motion.open_gripper.assert_not_called()
    action.motion.safe_stow_arm.assert_not_called()
    state = held_state()
    state.complete('place_item', success=False, action=action)
    assert state.snapshot()['holding_item'] is True


def test_place_failure_preserves_contact_reason_without_opening_or_recovery_motion():
    import json
    action = ready_place()
    action.motion.send_full_trajectory.return_value = False
    action.motion.last_trajectory_error = {'code': 'CONTACT_DETECTED',
        'message': 'wrist_extension contact detected.', 'controller_error_code': 100}
    result = action.execute(None, SimpleNamespace(), {})
    assert result[0] == 'failure' and 'wrist_extension contact detected' in result[1]
    details = json.loads(result[2])
    assert details['controller_error'] == action.motion.last_trajectory_error
    assert details['error_code'] == 'CONTACT_DETECTED'
    action.motion.open_gripper.assert_not_called()
    action.motion.safe_stow_arm.assert_not_called()


@pytest.mark.parametrize('seed', [{}, {'original_x': 0., 'original_y': -.68, 'z': .55}])
def test_trash_without_local_detection_never_uses_table_defaults_or_opens(seed):
    import json
    action = ready_place()
    result = action.execute(None, SimpleNamespace(), {'surface': 'trash_can', **seed})
    assert result[0] == 'failure'
    details = json.loads(result[2])
    assert details['error_code'] == 'TRASH_TARGET_NOT_FOUND'
    assert not details['release_attempted'] and not details['arm_motion_started']
    action.motion.send_full_trajectory.assert_not_called()
    action.motion.open_gripper.assert_not_called()
    action.motion.safe_stow_arm.assert_not_called()
    action.motion.stow_empty_gripper.assert_not_called()
    assert not action.place_active
    state = held_state()
    state.complete('place_item', success=False, action=action)
    assert state.snapshot()['holding_item'] is True


@pytest.mark.parametrize('hint,z', [([0., -1.5, .55], .55), ([0., -.2, .55], .55),
    ([0., -.68, 1.2], 1.2), ([.68, 0., .55], .55), ([0., -.68, .05], .05)])
def test_unreachable_trash_target_cannot_be_clamped_into_a_successful_drop(hint, z):
    import json
    action = ready_place()
    action._look_at_surface_and_refresh_hint.return_value = z
    action._hint_in_base_frame.return_value = hint
    action._align_base_to_target = Mock(return_value=False)
    result = action.execute(None, SimpleNamespace(), {'surface': 'trash_can'})
    assert json.loads(result[2])['error_code'] == 'TRASH_TARGET_UNREACHABLE'
    action.motion.send_full_trajectory.assert_not_called()
    action.motion.open_gripper.assert_not_called()


def test_unknown_open_is_not_repeated_by_stow_or_followed_by_retraction():
    action = ready_place()
    action.motion.open_gripper.return_value = False
    action._retract_arm = Mock()
    result = action.execute(None, SimpleNamespace(), {})
    assert result[0] == 'failure' and action._release_attempted and not action._gripper_opened
    action._retract_arm.assert_not_called()
    action.motion.safe_stow_arm.assert_not_called()
    state = held_state()
    state.complete('place_item', success=False, action=action)
    assert state.snapshot()['holding_item'] is None


def test_opened_gripper_remains_empty_when_stowing_fails():
    action = ready_place()
    action.motion.stow_empty_gripper.return_value = False
    result = action.execute(None, SimpleNamespace(), {})
    assert result[0] == 'failure'
    state = held_state()
    state.complete('place_item', success=False, action=action)
    assert state.snapshot()['holding_item'] is False


def test_stop_after_confirmed_open_preserves_empty_without_further_arm_motion():
    action = ready_place()
    action._retract_arm = Mock()
    def sleep(seconds):
        if action._gripper_opened:
            action._stop_evt.set()
    action.motion.sleep.side_effect = sleep
    assert action.execute(None, SimpleNamespace(), {})[0] == 'stopped'
    action._retract_arm.assert_not_called()
    action.motion.safe_stow_arm.assert_not_called()
    state = held_state()
    state.complete('place_item', success=False, action=action)
    assert state.snapshot()['holding_item'] is False


@pytest.mark.parametrize('attempted,success,expected', [(False,False,False),
    (True,False,None), (True,True,True)])
def test_generic_grasp_distinguishes_preparation_failure_from_uncertain_pickup(attempted, success, expected):
    state = ObjectState()
    state.begin('grab_item')
    state.complete('grab_item', success=success, action=SimpleNamespace(_grasp_attempted=attempted))
    assert state.snapshot()['holding_item'] is expected

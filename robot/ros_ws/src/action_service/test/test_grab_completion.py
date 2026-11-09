import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from action_service.grab_item_action import GrabItemAction
from action_service.common.math_utils import GrabberState
from robot_common.object_state import ObjectState


def ready_grab(*, stowed=True, settled=True, navigation=True):
    node = SimpleNamespace(log=Mock(), get_clock=lambda: SimpleNamespace(now=lambda: object()))
    for outcome in ('success', 'failure', 'stopped', 'timeout'):
        setattr(node, 'set_' + outcome,
            Mock(side_effect=lambda response, message, data='', outcome=outcome: (outcome, message, data)))
    action = GrabItemAction(node)
    action.motion = Mock()
    action._wait_for_traj_server = Mock(return_value=True)
    action._wait_for_joints = Mock(return_value=True)
    action._switch_to_position_mode = Mock(return_value=True)
    action._sleep_with_stop = Mock()
    action._stow_arm = Mock()
    action._move_arm_up = Mock()
    action._initialize_grab_pose = Mock()
    order = []
    action.motion.safe_stow_arm.side_effect = lambda **_: order.append('stow') or stowed
    action.motion.wait_for_motion_completion.side_effect = lambda **_: order.append('settle') or settled
    action.motion.switch_to_navigation_mode.side_effect = lambda **_: order.append('navigation') or navigation
    def grasp():
        action._grasp_attempted = True
        action.grab_result = 'success'
        action.state = GrabberState.FINISHED
    action._control_loop = grasp
    return action, order


def test_grab_stows_with_active_grip_before_settling_and_switching_modes():
    action, order = ready_grab()
    result = action.execute(None, object(), {'item': 'bottle'})
    assert result[0] == 'success'
    assert order == ['stow', 'settle', 'navigation']
    action.motion.safe_stow_arm.assert_called_once_with(gripper_pos=-.35, preserve_gripper=True)
    details = json.loads(result[2])
    assert details['grasp_completed'] and details['arm_stowed'] and details['navigation_mode_restored']


@pytest.mark.parametrize('fault', ['stow', 'settle', 'navigation', 'exception'])
def test_grasp_success_does_not_hide_cleanup_failure(fault):
    action, order = ready_grab(stowed=fault != 'stow', settled=fault != 'settle', navigation=fault != 'navigation')
    if fault == 'exception':
        action.motion.safe_stow_arm.side_effect = RuntimeError('controller unavailable')
    result = action.execute(None, object(), {'item': 'bottle'})
    assert result[0] == 'failure' and 'Grasp completed, but' in result[1]
    assert action._grasp_completed and json.loads(result[2])['cleanup_error']
    if fault == 'settle':
        assert order == ['stow', 'settle']
        action.motion.switch_to_navigation_mode.assert_not_called()


def test_completed_grasp_keeps_possession_assumption_when_stow_fails():
    action, _ = ready_grab(stowed=False)
    assert action.execute(None, object(), {'item': 'bottle'})[0] == 'failure'
    state = ObjectState()
    state.begin('grab_item')
    state.complete('grab_item', success=False, action=action, object_id='bottle')
    assert state.snapshot()['possession'] == 'holding_assumed'
    assert state.snapshot()['assumed_object_id'] == 'bottle'
    assert not state.snapshot()['possession_sensor_verified']


def test_stopped_grab_does_not_start_stowing():
    action, order = ready_grab()
    action._wait_for_traj_server.side_effect = lambda **_: action._stop_evt.set() or False
    assert action.execute(None, object(), {'item': 'bottle'})[0] == 'stopped'
    action.motion.safe_stow_arm.assert_not_called()
    assert order == ['settle', 'navigation']


def test_a_new_grab_does_not_reuse_previous_pickup_evidence():
    action, _ = ready_grab()
    assert action.execute(None, object(), {'item': 'bottle'})[0] == 'success'
    action._wait_for_traj_server.return_value = False
    assert action.execute(None, object(), {'item': 'cup'})[0] == 'failure'
    assert not action._grasp_completed

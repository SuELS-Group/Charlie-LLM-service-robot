from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from rclpy.task import Future
from robot_common.object_state import ObjectState
from robot_common.motion import MotionHelper


def test_fresh_start_assumes_empty_without_an_operator_initialization(tmp_path):
    state = ObjectState(tmp_path / 'object-state.json')
    assert state.snapshot()['possession'] == 'empty'
    assert state.snapshot()['possession_source'] == 'startup_assumed_empty'
    assert state.snapshot()['possession_sensor_verified'] is False
    assert state.admission_error('take_from_hand') is None
    assert state.admission_error('give_to_person')


def test_new_robot_boot_discards_old_possession_but_node_restart_does_not(tmp_path):
    path = tmp_path / '{boot_id}' / 'object-state.json'
    first_boot = '00000000-0000-0000-0000-000000000001'
    next_boot = '00000000-0000-0000-0000-000000000002'
    first = ObjectState(path, boot_id=first_boot)
    first.set('holding_assumed', 'completed_grab_workflow')
    first.label_assumed_object('trial_cup')
    restarted = ObjectState(path, boot_id=first_boot)
    assert restarted.snapshot()['holding_item'] is True
    assert restarted.snapshot()['assumed_object_id'] == 'trial_cup'
    next_start = ObjectState(path, boot_id=next_boot)
    assert next_start.snapshot()['holding_item'] is False
    assert next_start.snapshot()['possession_source'] == 'startup_assumed_empty'
    assert next_start.snapshot()['assumed_object_id'] is None
    assert ObjectState(path, boot_id=first_boot).snapshot()['holding_item'] is True


def test_completed_pickup_survives_restart_and_keeps_the_object_label(tmp_path):
    path = tmp_path / 'object-state.json'
    state = ObjectState(path)
    state.begin('take_from_hand')
    state.complete('take_from_hand', success=True, action=SimpleNamespace(_object_secured=True))
    state.label_assumed_object('trial_cup')
    restored = ObjectState(path)
    assert restored.snapshot()['possession'] == 'holding_assumed'
    assert restored.snapshot()['assumed_object_id'] == 'trial_cup'
    assert restored.admission_error('take_from_hand')
    assert restored.admission_error('give_to_person') is None


def test_completed_release_survives_restart(tmp_path):
    path = tmp_path / 'object-state.json'
    state = ObjectState(path)
    state.set('holding_assumed', 'completed_pickup')
    state.begin('give_to_person')
    state.complete('give_to_person', success=True, action=SimpleNamespace(_gripper_opened=True))
    assert ObjectState(path).snapshot()['possession'] == 'empty'


@pytest.mark.parametrize('action_name,previous', [('take_from_hand', 'empty'), ('give_to_person', 'holding_assumed')])
def test_restart_during_a_workflow_keeps_unknown_instead_of_assuming_empty(tmp_path, action_name, previous):
    path = tmp_path / 'object-state.json'
    state = ObjectState(path)
    state.set(previous, 'previous_completed_action')
    state.begin(action_name)
    assert state.snapshot()['possession'] == previous
    restored = ObjectState(path)
    assert restored.snapshot()['possession'] == 'unknown'
    assert restored.admission_error('take_from_hand') and restored.admission_error('give_to_person')


@pytest.mark.parametrize('content', ['not JSON', '[]', '{"version":99}',
    '{"version":1,"possession":"empty","possession_source":"","assumed_object_id":null}'])
def test_invalid_saved_state_requires_a_check_instead_of_assuming_empty(tmp_path, content):
    path = tmp_path / 'object-state.json'
    path.write_text(content)
    state = ObjectState(path)
    assert state.snapshot()['possession'] == 'unknown'
    assert state.admission_error('take_from_hand')


def test_failed_atomic_replace_preserves_the_last_record_and_marks_memory_unknown(tmp_path, monkeypatch):
    import robot_common.object_state as module
    path = tmp_path / 'object-state.json'
    state = ObjectState(path)
    state.begin('take_from_hand')
    previous = path.read_bytes()
    monkeypatch.setattr(module.os, 'replace', Mock(side_effect=OSError('simulated storage failure')))
    with pytest.raises(OSError):
        state.complete('take_from_hand', success=True, action=SimpleNamespace(_object_secured=True))
    assert state.snapshot()['possession'] == 'unknown'
    assert path.read_bytes() == previous
    assert ObjectState(path).snapshot()['possession'] == 'unknown'
    assert list(tmp_path.iterdir()) == [path]


def test_pickup_then_release_tracks_assumptions_without_sensor_claims():
    state = ObjectState()
    assert state.snapshot()['possession_source'] == 'startup_assumed_empty'
    assert state.admission_error('take_from_hand') is None
    state.complete('take_from_hand', success=True, action=SimpleNamespace(_object_secured=True))
    assert state.snapshot()['possession'] == 'holding_assumed'
    assert state.snapshot()['possession_sensor_verified'] is False
    assert state.admission_error('take_from_hand')
    assert state.admission_error('give_to_person') is None
    state.complete('give_to_person', success=True, action=SimpleNamespace(_gripper_opened=True))
    assert state.snapshot()['possession'] == 'empty'
    assert state.admission_error('give_to_person')


@pytest.mark.parametrize('operation,report_state,source,action', [
    ('take_from_hand', 'empty', 'explicit_user_empty_report', SimpleNamespace(_object_secured=True)),
    ('give_to_person', 'holding_assumed', 'explicit_user_holding_report', SimpleNamespace(_gripper_opened=True)),
    ('grab_item', 'empty', 'explicit_user_empty_report', None),
    ('place_item', 'holding_assumed', 'explicit_user_holding_report', None),
    ('release_item', 'holding_assumed', 'explicit_user_holding_report', None)])
def test_newer_user_correction_wins_over_an_older_workflows_completion(tmp_path, operation, report_state, source, action):
    path = tmp_path / 'state.json'
    state = ObjectState(path)
    state.begin(operation)
    state.set(report_state, source)
    assert state.complete(operation, success=True, action=action) is False
    assert state.invalidate(operation, 'late_workflow_exception') is False
    assert ObjectState(path).snapshot()['possession'] == report_state
    assert state.snapshot()['possession_source'] == source


@pytest.mark.parametrize('closed,attempted,expected', [(False, False, 'empty'),
    (False, True, 'unknown'), (True, True, 'holding_assumed')])
def test_failed_pickup_distinguishes_no_close_from_uncertain_close_and_failed_stow(closed, attempted, expected):
    state = ObjectState()
    state.set('empty', 'operator')
    state.complete('take_from_hand', success=False, action=SimpleNamespace(
        _object_secured=closed, _grasp_attempted=attempted))
    assert state.snapshot()['possession'] == expected


@pytest.mark.parametrize('opened,attempted,expected', [(False, False, 'holding_assumed'),
    (False, True, 'unknown'), (True, True, 'empty')])
def test_failed_handover_preserves_or_invalidates_the_right_state(opened, attempted, expected):
    state = ObjectState()
    state.set('holding_assumed', 'pickup')
    state.complete('give_to_person', success=False, action=SimpleNamespace(
        _gripper_opened=opened, _release_attempted=attempted))
    assert state.snapshot()['possession'] == expected


def test_failed_close_never_opens_or_retracts_an_object_automatically():
    from test_take_from_hand_action import _ready_action
    _, action = _ready_action()
    def interrupted_close():
        action._grasp_attempted = True
        return False
    action._close_gripper.side_effect = interrupted_close
    result = action.execute(None, SimpleNamespace(), {})
    assert result[0] == 'failure'
    action._abort_open_and_retract.assert_not_called()
    action._stow_with_object.assert_not_called()
    action._restore_handoff_orientation.assert_not_called()


def completed(value):
    future = Future()
    future.set_result(value)
    return future


@pytest.mark.parametrize('status,code,expected', [(4, 0, True), (6, -4, False), (4, -1, False)])
def test_gripper_driver_error_never_becomes_success(status, code, expected):
    motion = MotionHelper(SimpleNamespace(log=Mock()))
    motion.last_goal_handle = SimpleNamespace(cancel_goal_async=Mock())
    motion.last_goal_result = completed(SimpleNamespace(status=status, result=SimpleNamespace(error_code=code)))
    assert motion.finish_gripper_close(timeout_s=0) is expected


def test_nonterminating_gripper_cannot_be_used_for_transport():
    motion = MotionHelper(SimpleNamespace(log=Mock()))
    motion.last_goal_handle = SimpleNamespace(cancel_goal_async=lambda: completed(
        SimpleNamespace(return_code=0, goals_canceling=[object()])))
    motion.last_goal_result = Future()
    assert not motion.finish_gripper_close(timeout_s=0, cancel_timeout_s=0)


def test_timer_driven_cancellation_does_not_claim_a_pickup():
    motion = MotionHelper(SimpleNamespace(log=Mock()))
    terminal = Future()
    def cancel():
        terminal.set_result(SimpleNamespace(status=5, result=SimpleNamespace(error_code=0)))
        return completed(SimpleNamespace(return_code=0, goals_canceling=[object()]))
    motion.last_goal_handle = SimpleNamespace(cancel_goal_async=cancel)
    motion.last_goal_result = terminal
    assert not motion.finish_gripper_close(timeout_s=0)
    assert motion.last_gripper_completion['gripper_goal_settled']
    assert not motion.last_gripper_completion['gripper_contact_indicated']


def test_unknown_open_never_replays_opening_as_stow():
    from test_give_to_person_action import _ready_action
    _, action = _ready_action()
    def interrupted_open():
        action._release_attempted = True
        return False
    action._open_gripper.side_effect = interrupted_open
    result = action.execute(None, SimpleNamespace(), {})
    assert result[0] == 'failure'
    action._stow_after_release.assert_not_called()
    action._retract_with_object.assert_not_called()


def test_fallback_carry_pose_does_not_reissue_gripper_close():
    from test_take_from_hand_action import _node
    from action_service.take_from_hand_action import TakeFromHandAction
    action = TakeFromHandAction(_node())
    action.motion = Mock()
    action.motion.send_full_trajectory.return_value = True
    assert action._move_to_carry_pose(closed=True)
    args = action.motion.send_full_trajectory.call_args
    assert 'joint_gripper_finger_left' not in args.args[0]
    assert args.kwargs['omit_joints'] == ('joint_gripper_finger_left',)

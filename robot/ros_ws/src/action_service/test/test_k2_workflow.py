import json
from types import SimpleNamespace
from unittest.mock import Mock

from action_interfaces.srv import ExecuteAction
from action_service.action_service_node import ActionServiceNode
from robot_common.study_policy import StudyPolicy
from test_action_admission import node
from test_k2_motion_coordination import runtime
from test_study_policy import profile


def operation(kind, **params):
    return SimpleNamespace(action_type=kind, parameters=json.dumps(params))


def setup():
    n = node()
    n.study_policy = StudyPolicy(profile(), 'study')
    n.k2_runtime = runtime()
    n.current_workflow_id = ''
    n.get_clock = lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=100_100_000_000))
    return n


def test_completed_pickup_approved_transport_and_handover_use_actual_robot_gates():
    n = setup()
    n.take_from_hand_action = SimpleNamespace(_arm_motion_started=True, _arm_retracted=True,
        _object_secured=True, _grasp_attempted=True, _orientation_restored=True, _navigation_mode_restored=True)
    n.give_to_person_action = SimpleNamespace(_arm_motion_started=True, _arm_retracted=True,
        _gripper_opened=True, _release_attempted=True, _orientation_restored=True, _navigation_mode_restored=True)
    def completed(_request, response):
        return n.set_success(response, 'workflow completed')
    n._action_handlers.update(take_from_hand=completed, give_to_person=completed)
    pickup = ActionServiceNode.handle_action(n, operation('take_from_hand', _issued_at=110, item='cup'), ExecuteAction.Response())
    data = json.loads(pickup.result_data)
    assert pickup.status.status == 1 and data['travel_ready']
    assert data['possession'] == 'holding_assumed' and data['assumed_object_id'] == 'cup'
    lease = ActionServiceNode._handle_base_lease(n, operation('acquire', mode='study', action='move_to_waypoint',
        request_tool='move_to_waypoint', target='service b', current=[1., 2., 0.], goal=[5., 2., 180.]), ExecuteAction.Response())
    assert lease.status.status == 1
    held = ActionServiceNode.handle_action(n, operation('give_to_person', _issued_at=110), ExecuteAction.Response())
    assert held.status.status != 1  # Arm cannot start while transport owns the base.
    lease_id = json.loads(lease.result_data)['lease_id']
    released = ActionServiceNode._handle_base_lease(n, operation('release', lease_id=lease_id), ExecuteAction.Response())
    assert released.status.status == 1
    handover = ActionServiceNode.handle_action(n, operation('give_to_person', _issued_at=110), ExecuteAction.Response())
    assert handover.status.status == 1
    assert json.loads(handover.result_data)['possession'] == 'empty'


def test_operator_cannot_reset_possession_during_owned_motion():
    n = setup()
    n.k2_runtime.begin_action('active')
    result = ActionServiceNode._handle_object_state(n, operation('operator_set', possession='empty'), ExecuteAction.Response())
    assert result.status.status != 1


def test_unknown_possession_and_mode_mismatch_block_native_base():
    n = setup()
    args = dict(mode='study', action='move_to_waypoint', request_tool='move_to_waypoint',
        target='service b', current=[1., 2., 0.], goal=[5., 2., 180.])
    n.object_state.set('unknown', 'interrupted_close')
    assert ActionServiceNode._handle_base_lease(n, operation('acquire', **args), ExecuteAction.Response()).status.status != 1
    n.object_state.set('empty', 'operator')
    args['mode'] = 'development'
    assert ActionServiceNode._handle_base_lease(n, operation('acquire', **args), ExecuteAction.Response()).status.status != 1


def test_lost_release_response_can_be_repeated_without_releasing_new_work():
    state = runtime()
    first, _ = state.acquire_base('', 100.1)
    assert state.release_base(first)
    second, _ = state.acquire_base('', 100.1)
    assert state.release_base(first)
    assert state.lease == second


def test_pending_internal_turn_is_not_mistaken_for_pending_arm_motion():
    n = setup()
    n.current_workflow_id = 'owner'
    n.k2_runtime.begin_action('owner')
    turn = SimpleNamespace(has_pending_motion=lambda: True)
    arm = SimpleNamespace(has_pending_motion=lambda: False)
    n._stoppable_actions = [('take_from_hand', SimpleNamespace(motion=arm, turn_motion=turn), 'handoff_active')]
    result = ActionServiceNode._handle_base_lease(n, operation('acquire', mode='study', action='turn_to_relative',
        workflow_id='owner', request_tool='take_from_hand', current=[1., 2., 0.], goal=[1., 2., 90.]), ExecuteAction.Response())
    assert result.status.status == 1


def test_invalid_service_parameter_objects_are_reported_without_throwing():
    n = setup()
    for handler in (ActionServiceNode._handle_base_lease, ActionServiceNode._handle_study_policy):
        assert handler(n, SimpleNamespace(action_type='acquire', parameters='[]'), ExecuteAction.Response()).status.status != 1


def test_action_service_journals_before_handler_and_clears_a_no_grip_timeout(tmp_path):
    from robot_common.object_state import ObjectState
    path = tmp_path / 'object-state.json'
    n = setup()
    n.object_state = ObjectState(path)
    n.take_from_hand_action = SimpleNamespace(_arm_motion_started=False, _arm_retracted=False,
        _object_secured=False, _grasp_attempted=False, _orientation_restored=True, _navigation_mode_restored=True)
    def handler(_request, response):
        assert ObjectState(path).snapshot()['possession'] == 'unknown'
        return n.set_timeout(response, 'No confirmation received')
    n._action_handlers['take_from_hand'] = handler
    result = ActionServiceNode.handle_action(n, operation('take_from_hand', _issued_at=110), ExecuteAction.Response())
    assert result.status.status == 4
    assert ObjectState(path).snapshot()['possession'] == 'empty'


def test_state_storage_failure_blocks_handler_before_any_gripper_command(tmp_path):
    from robot_common.object_state import ObjectState
    n = setup()
    n.object_state = ObjectState(tmp_path / 'object-state.json')
    n.object_state._persist = Mock(side_effect=OSError('state disk unavailable'))
    n.stop_current_action = Mock()
    n._action_handlers['take_from_hand'] = Mock()
    result = ActionServiceNode.handle_action(n, operation('take_from_hand', _issued_at=110), ExecuteAction.Response())
    assert result.status.status != 1
    n._action_handlers['take_from_hand'].assert_not_called()
    assert n.object_state.snapshot()['possession'] == 'unknown'

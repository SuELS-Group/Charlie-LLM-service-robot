import json
from types import SimpleNamespace

import pytest
from action_interfaces.srv import ExecuteAction
from action_service.action_service_node import ActionServiceNode
from test_action_admission import node
from test_k2_motion_coordination import runtime


def recovery_node():
    n = node()
    n.k2_runtime = runtime()
    n._startup_time = 90.
    n.get_clock = lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=100_100_000_000))
    return n


def read(n):
    response = ActionServiceNode._handle_object_state(n,
        SimpleNamespace(action_type='read', parameters='{}'), ExecuteAction.Response())
    assert response.status.status == 1
    return json.loads(response.result_data)


def test_completed_stop_can_be_rechecked_without_resetting_any_state():
    n = recovery_node()
    n._action_stop_evt.set()
    before = n.object_state.snapshot()
    result = read(n)
    assert result['recovery_ready'] and result['controller_work_finished']
    assert result['driver_stop_acknowledged'] and result['joint_feedback_error'] is None
    assert result['last_stop_at'] == 100. and result['runtime_started_at'] == 90.
    assert n._action_stop_evt.is_set() and n.object_state.snapshot() == before


@pytest.mark.parametrize('fault', ['lease', 'workflow', 'locked', 'controller', 'worker',
                                  'driver_unconfirmed', 'stale_joints', 'unknown_object'])
def test_incomplete_or_unknown_state_cannot_release_recovery(fault):
    n = recovery_node()
    if fault == 'lease': n.k2_runtime.lease = 'base'
    if fault == 'workflow': n.k2_runtime.workflow = 'arm'
    if fault == 'locked': n._action_lock.acquire()
    if fault == 'controller': n._motion_helper.has_pending_motion = lambda: True
    if fault == 'worker':
        n._stoppable_actions = [('handoff', SimpleNamespace(_op_thread=SimpleNamespace(is_alive=lambda: True)), 'active')]
    if fault == 'driver_unconfirmed': n._driver_stop_confirmed = False
    if fault == 'stale_joints': n.k2_runtime.joint_captured = 90.
    if fault == 'unknown_object': n.object_state.set('unknown', 'interrupted_close')
    assert not read(n)['recovery_ready']


def test_ended_stop_can_allow_an_explicit_stow_without_claiming_travel_readiness():
    n = recovery_node()
    n.k2_runtime.joints['wrist_extension'] = .1
    result = read(n)
    assert result['recovery_ready']
    assert 'wrist_extension' in result['travel_posture_error']
    assert not n.k2_runtime.acquire_base('', 100.1)[0]

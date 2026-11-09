import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from action_interfaces.srv import ExecuteAction
from action_service.action_service_node import ActionServiceNode
from test_recovery_readiness import recovery_node


def report(n, operation='report_empty', **changes):
    params = {'_issued_at':100.05, '_request_id':'fresh-report', **changes}
    return ActionServiceNode._handle_object_state(n,
        SimpleNamespace(action_type=operation, parameters=json.dumps(params)), ExecuteAction.Response())


@pytest.mark.parametrize('mode', ['development', 'study'])
@pytest.mark.parametrize('previous', ['empty', 'holding_assumed', 'unknown'])
@pytest.mark.parametrize('operation,state,source', [('report_empty', 'empty', 'explicit_user_empty_report'),
    ('report_holding', 'holding_assumed', 'explicit_user_holding_report')])
def test_human_report_can_correct_possession_in_every_mode_without_motion(mode, previous, operation, state, source):
    n = recovery_node()
    n.study_policy.mode = mode
    n.object_state.set(previous, 'previous_workflow')
    n.stop_current_action = Mock()
    result = report(n, operation)
    data = json.loads(result.result_data)
    assert result.status.status == 1 and data['possession'] == state
    assert data['possession_source'] == source
    assert data['holding_item'] is (state == 'holding_assumed')
    assert data['human_report_recorded'] and data['possession_sensor_verified'] is False
    n.stop_current_action.assert_not_called()


@pytest.mark.parametrize('fault', ['before_stop', 'before_state_change', 'before_startup', 'old', 'future', 'no_request_id'])
def test_old_report_cannot_overwrite_newer_state(fault):
    n = recovery_node()
    n.object_state.set('holding_assumed', 'completed_hand_pickup_command')
    params = {}
    if fault == 'before_stop': n._last_stop_time = 100.06
    if fault == 'before_state_change': n._last_object_state_time = 100.06
    if fault == 'before_startup': n._startup_time = 100.06
    if fault == 'old': params['_issued_at'] = 50.
    if fault == 'future': params['_issued_at'] = 101.
    if fault == 'no_request_id': params['_request_id'] = ''
    result = report(n, **params)
    assert result.status.status != 1
    assert n.object_state.snapshot()['possession'] == 'holding_assumed'


@pytest.mark.parametrize('fault', ['lease', 'workflow', 'locked', 'pending', 'driver', 'stale_joints'])
@pytest.mark.parametrize('operation', ['report_empty', 'report_holding'])
def test_human_possession_report_does_not_depend_on_or_release_motion_checks(fault, operation):
    from test_recovery_readiness import read
    n = recovery_node()
    n.stop_current_action = Mock()
    if fault == 'lease': n.k2_runtime.lease = 'active'
    if fault == 'workflow': n.k2_runtime.workflow = 'active'
    if fault == 'locked': n._action_lock.acquire()
    if fault == 'pending': n._motion_helper.has_pending_motion = lambda: True
    if fault == 'driver': n._driver_stop_confirmed = False
    if fault == 'stale_joints': n.k2_runtime.joint_captured = 90.
    assert report(n, operation).status.status == 1
    assert not read(n)['recovery_ready']
    n.stop_current_action.assert_not_called()


def test_human_holding_report_does_not_inherit_the_old_objects_identity():
    n = recovery_node()
    n.object_state.set('holding_assumed', 'old_pickup')
    n.object_state.label_assumed_object('old_cup')
    assert report(n, 'report_holding').status.status == 1
    assert n.object_state.snapshot()['assumed_object_id'] is None


def test_replayed_report_cannot_erase_a_subsequent_pickup():
    n = recovery_node()
    assert report(n).status.status == 1
    n.object_state.set('holding_assumed', 'completed_hand_pickup_command')
    n._last_object_state_time = 100.08
    assert report(n).status.status != 1
    assert n.object_state.snapshot()['possession'] == 'holding_assumed'


def test_storage_failure_never_acknowledges_the_report():
    n = recovery_node()
    n.object_state._persist = Mock(side_effect=OSError('disk unavailable'))
    assert report(n).status.status != 1

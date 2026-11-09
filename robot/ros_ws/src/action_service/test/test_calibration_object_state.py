import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from action_interfaces.srv import ExecuteAction
from action_service.action_service_node import ActionServiceNode
from test_recovery_readiness import recovery_node


def complete(n, **changes):
    params = {'_started_at':100.01, '_issued_at':100.09, '_request_id':'calibration', **changes}
    return ActionServiceNode._handle_object_state(n,
        SimpleNamespace(action_type='calibration_completed', parameters=json.dumps(params)), ExecuteAction.Response())


@pytest.mark.parametrize('previous', ['holding_assumed', 'unknown', 'empty'])
def test_completed_calibration_records_empty_without_moving_any_motor(previous):
    n = recovery_node()
    n.object_state.set(previous, 'old_workflow')
    n.stop_current_action = Mock()
    response = complete(n)
    state = json.loads(response.result_data)
    assert response.status.status == 1 and state['calibration_state_reset']
    assert state['possession'] == 'empty' and state['holding_item'] is False
    assert state['possession_source'] == 'completed_arm_calibration'
    assert not state['possession_sensor_verified']
    n.stop_current_action.assert_not_called()


def test_late_calibration_reply_preserves_a_newer_human_correction():
    n = recovery_node()
    n.object_state.set('holding_assumed', 'explicit_user_holding_report')
    n._last_object_state_time = 100.05
    response = complete(n)
    state = json.loads(response.result_data)
    assert response.status.status == 1 and state['newer_state_preserved']
    assert not state['calibration_state_reset']
    assert state['possession'] == 'holding_assumed'


@pytest.mark.parametrize('params', [dict(_started_at=99.), dict(_started_at=101.),
    dict(_started_at=True), dict(_issued_at=101.), dict(_request_id='')])
def test_invalid_or_pre_stop_calibration_completion_does_not_erase_state(params):
    n = recovery_node()
    n.object_state.set('holding_assumed', 'pickup')
    assert complete(n, **params).status.status != 1
    assert n.object_state.snapshot()['possession'] == 'holding_assumed'


def test_calibration_state_reset_cannot_bypass_unfinished_controller_work():
    n = recovery_node()
    n._driver_stop_confirmed = False
    n.object_state.set('unknown', 'unfinished_action')
    assert complete(n).status.status != 1
    assert n.object_state.snapshot()['possession'] == 'unknown'


@pytest.mark.parametrize('issued', [100.12, 100.199])
def test_small_inter_host_clock_offset_allows_only_the_completed_state_report(issued):
    n = recovery_node()  # Robot clock is 100.1 s.
    n.object_state.set('unknown', 'old_workflow')
    response = complete(n, _issued_at=issued)
    assert response.status.status == 1
    assert json.loads(response.result_data)['calibration_state_reset'] is True


@pytest.mark.parametrize('issued', [100.201, 101., float('inf')])
def test_future_allowance_stays_bounded(issued):
    n = recovery_node()
    n.object_state.set('holding_assumed', 'pickup')
    assert complete(n, _issued_at=issued).status.status != 1
    assert n.object_state.snapshot()['possession'] == 'holding_assumed'


def test_small_future_offset_does_not_release_a_report_started_before_stop():
    n = recovery_node()
    n.object_state.set('holding_assumed', 'pickup')
    assert complete(n, _started_at=99.99, _issued_at=100.12).status.status != 1
    assert n.object_state.snapshot()['possession'] == 'holding_assumed'


@pytest.mark.parametrize(('issued', 'accepted'), [(100.12, True), (100.201, False)])
def test_explicit_human_state_reports_use_the_same_bounded_clock_allowance(issued, accepted):
    n = recovery_node()
    n.object_state.set('holding_assumed', 'pickup')
    request = SimpleNamespace(action_type='report_empty', parameters=json.dumps({
        '_issued_at': issued, '_request_id': 'fresh-user-report'}))
    response = ActionServiceNode._handle_object_state(n, request, ExecuteAction.Response())
    assert (response.status.status == 1) is accepted
    assert n.object_state.snapshot()['possession'] == ('empty' if accepted else 'holding_assumed')

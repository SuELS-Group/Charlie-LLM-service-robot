import json
from types import SimpleNamespace

import pytest
from rclpy.task import Future
from command_interfaces.action import MoveTo
from tool_status.msg import ToolStatus as RosToolStatus
from mcp_server import ros_utils
from mcp_server.tools import navigation


@pytest.mark.parametrize('data', ['', '{"completion_confirmed":false,"execution_uncertain":true}', '[]'])
def test_terminal_navigation_with_missing_or_unknown_owned_work_is_not_confirmed(data):
    result = MoveTo.Result()
    result.result_data = data
    future = Future()
    future.set_result(SimpleNamespace(status=5, result=result))
    completion = ros_utils._action_completion(future)
    assert not completion['completion_confirmed'] and completion['execution_uncertain']


def test_move_result_uses_actual_coordinate_fields(monkeypatch):
    result = MoveTo.Result()
    result.status.status = navigation.STATUS_SUCCESS
    result.x, result.y, result.orientation = 1.2, 2.3, 90.0
    result.result_data = json.dumps({'completion_confirmed': True, 'execution_uncertain': False})
    monkeypatch.setattr(navigation, 'call_action', lambda *_a, **_k: {
        'accepted': True, 'timed_out': False, 'result': result, 'execution_uncertain': False})
    output = navigation._run_navigation_action('/test', MoveTo, MoveTo.Goal(), 10,
        failure_code='FAILED', timeout_code='TIMEOUT')
    assert output['status'] == 'success'
    assert (output['result']['goal_x'], output['result']['goal_y'], output['result']['goal_orientation']) == (1.2, 2.3, 90.0)


def test_transit_posture_failure_is_distinguished_from_path_planning_failure(monkeypatch):
    result = MoveTo.Result()
    result.status.status = RosToolStatus.STATUS_FAILURE
    result.status.message = 'Transit posture not confirmed: joint_lift=0.97 outside [0.2,0.9]'
    result.result_data = json.dumps({'phase': 'transit_posture', 'base_started': False,
        'completion_confirmed': True, 'execution_uncertain': False})
    monkeypatch.setattr(navigation, 'call_action', lambda *_a, **_k: {
        'accepted': True, 'timed_out': False, 'result': result, 'execution_uncertain': False})
    output = navigation._run_navigation_action('/test', MoveTo, MoveTo.Goal(), 10,
        failure_code='NAV_FAILED', timeout_code='NAV_TIMEOUT')
    assert output['error']['code'] == 'ARM_NOT_STOWED'
    assert not output['error']['retryable'] and not output['error']['execution_uncertain']
    assert output['result']['base_started'] is False


def test_stopped_navigation_preserves_partial_arrival_and_uncertainty(monkeypatch):
    result = MoveTo.Result()
    result.status.status = navigation.STATUS_STOPPED
    result.result_data = json.dumps({'base_reached': True, 'completion_confirmed': False, 'execution_uncertain': True})
    monkeypatch.setattr(navigation, 'call_action', lambda *_a, **_k: {
        'accepted': True, 'timed_out': False, 'result': result, 'execution_uncertain': True})
    output = navigation._run_navigation_action('/test', MoveTo, MoveTo.Goal(), 10,
        failure_code='FAILED', timeout_code='TIMEOUT')
    assert output['status'] == 'stopped' and output['result']['base_reached']
    assert output['error']['execution_uncertain'] and not output['error']['retryable']


@pytest.mark.parametrize('controller_error,expected_code', [
    ({'code': 'RUNSTOP_ACTIVE', 'message': 'Cannot execute goals while in mode=runstopped'}, 'RUNSTOP_ACTIVE'),
    ({'code': 'TRAJECTORY_FAILED', 'message': 'Invalid joint target'}, 'HEAD_PREPARATION_FAILED'),
    ({}, 'HEAD_PREPARATION_FAILED')])
def test_head_preparation_failure_preserves_driver_reason_and_is_not_retried(monkeypatch, controller_error, expected_code):
    result = MoveTo.Result()
    result.status.status = RosToolStatus.STATUS_FAILURE
    result.status.message = 'Head preparation failed: ' + controller_error.get('message', 'unavailable')
    result.result_data = json.dumps({'phase': 'head_preparation', 'base_started': False,
        'completion_confirmed': True, 'execution_uncertain': False, 'controller_error': controller_error})
    monkeypatch.setattr(navigation, 'call_action', lambda *_a, **_k: {
        'accepted': True, 'timed_out': False, 'result': result, 'execution_uncertain': False})
    output = navigation._run_navigation_action('/test', MoveTo, MoveTo.Goal(), 10,
        failure_code='TURN_FAILED', timeout_code='TURN_TIMEOUT')
    assert output['status'] == 'failure' and output['error']['code'] == expected_code
    assert output['result']['base_started'] is False and output['error']['retryable'] is False
    assert output['error']['execution_uncertain'] is False

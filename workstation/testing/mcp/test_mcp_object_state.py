import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mcp_server.tools import manipulation
from mcp_server.tools import system
from mcp_server.decorators import TOOL_REGISTRY


@pytest.mark.parametrize('tool,state', [(manipulation.take_from_hand, 'holding_assumed'),
    (manipulation.take_from_hand, 'unknown'), (manipulation.give_to_person, 'empty'),
    (manipulation.give_to_person, 'unknown')])
def test_invalid_object_state_blocks_before_approach(monkeypatch, tool, state):
    monkeypatch.setattr(manipulation, '_read_object_state', lambda: {'possession': state})
    approach = Mock()
    monkeypatch.setattr(manipulation, '_prepare_for_person_handoff', approach)
    result = tool()
    assert result['error']['code'] == 'OBJECT_STATE_INVALID'
    assert not result['error']['execution_uncertain']
    approach.assert_not_called()


def test_state_tool_can_only_read(monkeypatch):
    service = Mock(return_value=SimpleNamespace(status=SimpleNamespace(status=manipulation.STATUS_SUCCESS),
        result_data=json.dumps({'possession': 'holding_assumed', 'possession_sensor_verified': False})))
    monkeypatch.setattr(manipulation, 'call_service', service)
    result = manipulation.get_transport_state()
    assert result['status'] == 'success' and not result['result']['possession_sensor_verified']
    assert service.call_args.args[0] == '/k2/object_state'
    assert service.call_args.args[2].action_type == 'read'


def test_success_status_without_a_saved_human_report_is_not_accepted(monkeypatch):
    monkeypatch.setattr(system,'call_service',lambda *_a,**_k:SimpleNamespace(
        status=SimpleNamespace(status=manipulation.STATUS_SUCCESS), result_data='{"possession":"empty"}'))
    assert system.report_empty_gripper()['status'] == 'failure'


def test_holding_report_is_also_internal_and_persists_only_a_human_assertion(monkeypatch):
    service = Mock(return_value=SimpleNamespace(status=SimpleNamespace(status=manipulation.STATUS_SUCCESS),
        result_data=json.dumps({'possession':'holding_assumed',
            'possession_source':'explicit_user_holding_report','human_report_recorded':True})))
    monkeypatch.setattr(system,'call_service',service)
    assert system.report_holding_gripper(_request_time=100., _request_id='human-report')['status'] == 'success'
    assert TOOL_REGISTRY['report_holding_gripper']['meta']['category'] == 'system'
    assert service.call_args.args[2].action_type == 'report_holding'


def test_successful_calibration_resets_central_state_only_after_driver_success(monkeypatch):
    events = []
    def home(*args, **kwargs):
        events.append('home')
        return {'success': True}
    def update(*args, **kwargs):
        events.append('state')
        request = args[2]
        assert request.action_type == 'calibration_completed'
        params = json.loads(request.parameters)
        assert 0 < params['_started_at'] <= params['_issued_at'] and params['_request_id']
        return SimpleNamespace(status=SimpleNamespace(status=manipulation.STATUS_SUCCESS),
            result_data=json.dumps({'possession':'empty', 'holding_item':False,
                'calibration_completed':True, 'calibration_state_reset':True}))
    monkeypatch.setattr(manipulation, 'call_trigger_service', home)
    monkeypatch.setattr(manipulation, 'call_service', update)
    result = manipulation.calibrate_arm()
    assert result['status'] == 'success' and result['result']['holding_item'] is False
    assert events == ['home', 'state']


@pytest.mark.parametrize('driver_result', [None, {'success':False}, {'success':False, 'execution_uncertain':True}])
def test_failed_or_unconfirmed_calibration_does_not_reset_possession(monkeypatch, driver_result):
    monkeypatch.setattr(manipulation, 'call_trigger_service', lambda *a, **kw: driver_result)
    service = Mock()
    monkeypatch.setattr(manipulation, 'call_service', service)
    assert manipulation.calibrate_arm()['status'] == 'failure'
    service.assert_not_called()


def test_stowing_alone_preserves_possession(monkeypatch):
    monkeypatch.setattr(manipulation, 'call_trigger_service', lambda *a, **kw: {'success':True})
    service = Mock()
    monkeypatch.setattr(manipulation, 'call_service', service)
    assert manipulation.stow_arm()['status'] == 'success'
    service.assert_not_called()


@pytest.mark.parametrize('stow_success', [True, False])
def test_release_retains_the_confirmed_empty_state_even_when_post_stow_fails(monkeypatch, stow_success):
    monkeypatch.setattr(manipulation, '_run_execute_action', lambda *a, **kw: {'status':'success' if stow_success else 'failure',
        'result':{'possession':'empty', 'holding_item':False, 'gripper_opened':True}})
    trigger = Mock(side_effect=AssertionError('Release cleanup belongs to the native action'))
    monkeypatch.setattr(manipulation, 'call_trigger_service', trigger)
    result = manipulation.release_item()
    assert result['result']['possession'] == 'empty' and result['result']['holding_item'] is False
    assert (result['status'] == 'success') is stow_success
    trigger.assert_not_called()

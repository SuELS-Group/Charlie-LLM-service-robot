from unittest.mock import Mock

import pytest

import mcp_server.tools.manipulation as manipulation
import mcp_server.tools.perception as perception_tools
from mcp_server.decorators import ToolStatus


@pytest.fixture(autouse=True)
def prepared_object_state(monkeypatch):
    # Routing tests assume staff initialized the independent object state.
    monkeypatch.setattr(manipulation, '_require_object_state', lambda _: None)


def test_named_handoff_uses_successful_approach_without_redetection(monkeypatch):
    drive = Mock(return_value={
        'status': ToolStatus.SUCCESS.value,
        'result': {'approached': True, 'target': 'person_1', 'person_name': 'TestUserB', 'person_distance': 3.0, }, })
    monkeypatch.setattr(manipulation.navigation_tools, 'drive_to_person', drive)
    visible = Mock()
    monkeypatch.setattr(perception_tools, 'get_visible_objects', visible)
    result = manipulation._prepare_for_person_handoff('testuserb')
    assert result['status'] == ToolStatus.SUCCESS.value
    assert result['result']['target'] == 'person_1'
    assert result['result']['recipient'] == 'TestUserB'
    assert result['result']['target_verified'] is True
    assert result['result']['verification_source'] == 'drive_to_person'
    assert result['result']['preparation'] == 'drive_to_person'
    drive.assert_called_once_with(distance=1.3, person='testuserb')
    visible.assert_not_called()


def test_unnamed_handoff_uses_shared_front_facing_drive(monkeypatch):
    drive = Mock(return_value={
        'status': ToolStatus.SUCCESS.value,
        'result': {'approached': False, 'target': 'person_0', 'person_distance': 1.4, }, })
    monkeypatch.setattr(manipulation.navigation_tools, 'drive_to_person', drive)
    visible = Mock()
    monkeypatch.setattr(perception_tools, 'get_visible_objects', visible)
    result = manipulation._prepare_for_person_handoff()
    assert result['status'] == ToolStatus.SUCCESS.value
    assert result['result']['approached'] is False
    assert result['result']['preparation'] == 'drive_to_person'
    drive.assert_called_once_with(distance=1.3, person=None)
    visible.assert_not_called()


@pytest.mark.parametrize('tool,expected', [(manipulation.take_from_hand, 'empty'),
                                        (manipulation.give_to_person, 'holding_assumed')])
def test_explicit_handover_in_place_skips_search_and_drive_but_keeps_state_guard(monkeypatch, tool, expected):
    drive = Mock(side_effect=AssertionError('In-place handover must not search or drive'))
    monkeypatch.setattr(manipulation.navigation_tools, 'drive_to_person', drive)
    guard = Mock(return_value=None)
    monkeypatch.setattr(manipulation, '_require_object_state', guard)
    execute = Mock(return_value={'status': 'success', 'result': {}})
    monkeypatch.setattr(manipulation, '_run_execute_action', execute)
    assert tool(approach=False)['status'] == 'success'
    guard.assert_called_once_with(expected)
    drive.assert_not_called()
    assert execute.call_args.args[1]['recipient_policy'] == 'gesture_only'
    assert execute.call_args.kwargs['success_result']['preparation'] == 'handover_in_place'
    execute.reset_mock()
    guard.return_value = {'status': 'failure', 'error': {'code': 'OBJECT_STATE_INVALID'}}
    assert tool(approach=False)['status'] == 'failure'
    execute.assert_not_called()


@pytest.mark.parametrize('tool',[manipulation.take_from_hand,manipulation.give_to_person])
@pytest.mark.parametrize('resolved_name',[None,'TestUserA'])
def test_incidental_name_does_not_constrain_anonymous_handover(monkeypatch, tool, resolved_name):
    monkeypatch.setattr(manipulation,'_prepare_for_person_handoff',lambda person:{'status':'success','result':{
        'recipient_track':8, **({'recipient':resolved_name} if resolved_name else {})}})
    execute = Mock(return_value={'status':'success','result':{}})
    monkeypatch.setattr(manipulation,'_run_execute_action',execute)
    assert tool()['status'] == 'success'
    params = execute.call_args.args[1]
    assert params == {'timeout_seconds':45., 'person':None, 'recipient_policy':'gesture_only'}
    assert 'recipient_track' not in params


def test_take_from_hand_does_not_start_action_after_failed_preparation(monkeypatch):
    monkeypatch.setattr(manipulation, '_prepare_for_person_handoff', Mock(return_value={
        'status': ToolStatus.FAILURE.value,
        'error': {'code': 'TARGET_PERSON_NOT_FOUND', 'message': 'Could not find TestUserA', }, }))
    execute = Mock()
    monkeypatch.setattr(manipulation, '_run_execute_action', execute)
    result = manipulation.take_from_hand(person='TestUserA')
    assert result['status'] == ToolStatus.FAILURE.value
    execute.assert_not_called()


def test_give_to_person_does_not_start_action_after_failed_preparation(monkeypatch):
    monkeypatch.setattr(manipulation, '_prepare_for_person_handoff', Mock(return_value={
        'status': ToolStatus.FAILURE.value,
        'error': {'code': 'NO_PERSON_FOUND', 'message': 'No person found', }, }))
    execute = Mock()
    monkeypatch.setattr(manipulation, '_run_execute_action', execute)
    result = manipulation.give_to_person()
    assert result['status'] == ToolStatus.FAILURE.value
    execute.assert_not_called()


def test_give_to_person_runs_robot_action_with_preparation_diagnostics(monkeypatch):
    preparation = Mock(return_value={
        'status': ToolStatus.SUCCESS.value,
        'result': {
            'approached': True,
            'preparation': 'drive_to_person',
            'target': 'person_0',
            'recipient': 'TestUserB',
            'person_distance': 3.0, }, })
    monkeypatch.setattr(manipulation, '_prepare_for_person_handoff', preparation)
    execute = Mock(return_value={'status': ToolStatus.SUCCESS.value, 'result': {'message': 'Item handed to person'}, })
    monkeypatch.setattr(manipulation, '_run_execute_action', execute)
    result = manipulation.give_to_person(person='TestUserB', timeout_seconds=12.0)
    assert result['status'] == ToolStatus.SUCCESS.value
    args = execute.call_args.args
    assert args[0] == 'give_to_person'
    assert args[1] == {'timeout_seconds': 12.0, 'person': 'TestUserB', 'recipient_policy': 'gesture_only'}
    assert args[2] == 132.0
    assert execute.call_args.kwargs['success_result']['approached'] is True
    assert execute.call_args.kwargs['success_result']['approach_person'] == 'TestUserB'
    assert 'recipient' not in execute.call_args.kwargs['success_result']
    preparation.assert_called_once_with('TestUserB')

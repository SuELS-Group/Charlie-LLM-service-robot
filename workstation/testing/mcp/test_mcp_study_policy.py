import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mcp_server import ros_utils
from mcp_server.study_policy import registry_view
from mcp_server.tools import manipulation, navigation


def policy_server(monkeypatch, *, mode='study'):
    from robot_common.study_policy import StudyPolicy
    policy = StudyPolicy({'waypoints': {'a': {'x': 0., 'y': 0., 'orientation': 0.}},
        'approved_objects': ['cup'], 'active_object': 'cup'}, mode)
    def serve(service, kind, request, **kwargs):
        assert service == '/k2/study_policy'
        params = json.loads(request.parameters)
        error = policy.tool_error(params['tool'], params['arguments'], params['root_tool']) if request.action_type != 'summary' else None
        return SimpleNamespace(status=SimpleNamespace(status=0 if error else 1,
            message=error[1] if error else 'allowed'), result_data=json.dumps({**policy.summary(),
                **({'error_code': error[0]} if error else {})}))
    monkeypatch.setenv('ROBOT_OPERATION_MODE', 'study')
    monkeypatch.setattr(ros_utils, 'call_service', serve)
    return policy


def test_unapproved_tool_and_destination_never_reach_motion_rpc(monkeypatch):
    policy_server(monkeypatch)
    physical = Mock(side_effect=AssertionError('No physical RPC permitted'))
    monkeypatch.setattr(navigation, 'call_action', physical)
    monkeypatch.setattr(manipulation, 'call_service', physical)
    assert navigation.move_to(0., 0., 0.)['error']['code'] == 'POLICY_DENIED'
    assert navigation.move_to_waypoint('unknown')['error']['code'] == 'WAYPOINT_NOT_APPROVED'
    assert manipulation.release_item()['error']['code'] == 'POLICY_DENIED'
    assert manipulation.put_item_in_trash()['error']['code'] == 'POLICY_DENIED'
    physical.assert_not_called()


def test_study_registry_only_advertises_allowed_tools(monkeypatch):
    policy_server(monkeypatch)
    inventory = {name: {'name': name, 'meta': {}} for name in ['take_from_hand', 'move_to', 'delete_waypoint', 'get_transport_state']}
    view = registry_view(inventory)
    assert {tool['name'] for tool in view} == {'take_from_hand', 'get_transport_state'}
    assert all(tool['meta']['allowed_in_mode'] == ['study'] for tool in view)


def test_mismatched_modes_fail_closed(monkeypatch):
    policy_server(monkeypatch, mode='development')
    physical = Mock()
    monkeypatch.setattr(navigation, 'call_action', physical)
    assert navigation.move_to_waypoint('a')['error']['code'] == 'STUDY_PROFILE_UNAVAILABLE'
    physical.assert_not_called()


def test_unavailable_policy_service_is_not_misreported_as_a_mode_mismatch(monkeypatch):
    monkeypatch.setenv('ROBOT_OPERATION_MODE', 'study')
    monkeypatch.setattr(ros_utils, 'call_service', lambda *_a, **_k: None)
    physical = Mock()
    monkeypatch.setattr(navigation, 'call_action', physical)
    result = navigation.move_to_waypoint('a')
    assert result['error']['code'] == 'STUDY_PROFILE_UNAVAILABLE'
    assert 'service is unavailable' in result['error']['message']
    physical.assert_not_called()


def test_object_name_is_checked_before_person_approach(monkeypatch):
    policy_server(monkeypatch)
    approach = Mock()
    monkeypatch.setattr(manipulation, '_prepare_for_person_handoff', approach)
    assert manipulation.take_from_hand(item='heavy vase')['error']['code'] == 'OBJECT_NOT_APPROVED'
    approach.assert_not_called()


def test_multiple_people_do_not_block_the_nearest_person_in_study_mode(monkeypatch):
    policy_server(monkeypatch)
    monkeypatch.setattr(navigation, '_visible_objects_snapshot', lambda: (1, [
        {'item_name': 'person_1', 'track_id':4, 'x': 2., 'y': 0.},
        {'item_name': 'person_2', 'track_id':8, 'x': 2., 'y': 1.}], None))
    monkeypatch.setattr(navigation,'_get_robot_position',lambda:(SimpleNamespace(x=0.,y=0.,yaw_deg=0.),None))
    physical = Mock(return_value={'status':'success','result':{}})
    monkeypatch.setattr(navigation, '_drive_to_person_item', physical)
    # The real private nested-call context is supplied by the parent wrapper.
    from mcp_server.call_state import tool_context
    with tool_context('take_from_hand'):
        result = navigation.drive_to_person()
    assert result['status'] == 'success' and result['result']['recipient_track'] == 4
    assert physical.call_args.args[0]['item_name'] == 'person_1'


def test_named_person_in_study_mode_is_selected_over_a_closer_bystander(monkeypatch):
    policy_server(monkeypatch)
    monkeypatch.setattr(navigation,'_visible_objects_snapshot',lambda:(1,[
        {'item_name':'person_1','track_id':4,'face_name':'TestUserB','x':1.4,'y':0.},
        {'item_name':'person_2','track_id':8,'face_name':'TestUserA','x':2.,'y':.2}],None))
    monkeypatch.setattr(navigation,'_get_robot_position',lambda:(SimpleNamespace(x=0.,y=0.,yaw_deg=0.),None))
    physical = Mock(return_value={'status':'success','result':{}})
    monkeypatch.setattr(navigation,'_drive_to_person_item',physical)
    from mcp_server.call_state import tool_context
    with tool_context('take_from_hand'):
        result = navigation.drive_to_person(person='TestUserA')
    assert result['status'] == 'success' and result['result']['recipient_track'] == 8
    assert result['result']['person_name'] == 'TestUserA'
    assert physical.call_args.args[0]['face_name'] == 'TestUserA'


def test_only_duplicate_named_identity_remains_ambiguous(monkeypatch):
    policy_server(monkeypatch)
    monkeypatch.setattr(navigation,'_visible_objects_snapshot',lambda:(1,[
        {'item_name':'person_1','track_id':4,'face_name':'TestUserA','x':1.4,'y':0.},
        {'item_name':'person_2','track_id':8,'face_name':'TestUserA','x':2.,'y':.2}],None))
    physical = Mock()
    monkeypatch.setattr(navigation,'_drive_to_person_item',physical)
    from mcp_server.call_state import tool_context
    with tool_context('take_from_hand'):
        result = navigation.drive_to_person(person='TestUserA')
    assert result['error']['code'] == 'RECIPIENT_AMBIGUOUS'
    physical.assert_not_called()


def test_missing_camera_stream_is_not_followed_by_another_scan(monkeypatch):
    policy_server(monkeypatch)
    monkeypatch.setattr(navigation, '_visible_objects_snapshot', lambda: (0, [], None))
    scan = Mock()
    monkeypatch.setattr(navigation, 'call_service', scan)
    from mcp_server.call_state import tool_context
    with tool_context('take_from_hand'):
        result = navigation.drive_to_person()
    assert result['error']['code'] == 'PERSON_VIEW_UNAVAILABLE'
    scan.assert_not_called()


def test_scan_location_is_approached_once_without_live_reacquisition(monkeypatch):
    policy_server(monkeypatch)
    snapshot = Mock(return_value=(1, [], None))
    monkeypatch.setattr(navigation, '_visible_objects_snapshot', snapshot)
    position = SimpleNamespace(x=0., y=0., yaw_deg=0.)
    monkeypatch.setattr(navigation, '_get_robot_position', lambda: (position, None))
    monkeypatch.setattr(navigation, 'call_service', lambda *_a, **_k: SimpleNamespace(
        status=SimpleNamespace(status=1), result_data='[{"item_name":"person_4","x":2,"y":0}]'))
    turn = Mock(return_value={'status': 'success', 'result': {}})
    drive = Mock(return_value={'status': 'success', 'result': {}})
    monkeypatch.setattr(navigation, '_turn_toward_map_position', turn)
    monkeypatch.setattr(navigation, '_drive_to_person_item', drive)
    from mcp_server.call_state import tool_context
    with tool_context('take_from_hand'):
        result = navigation.drive_to_person()
    snapshot.assert_called_once()
    turn.assert_not_called()
    drive.assert_called_once()
    assert result['status'] == 'success'

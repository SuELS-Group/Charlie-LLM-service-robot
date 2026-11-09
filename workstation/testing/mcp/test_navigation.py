import json
import math
from types import SimpleNamespace
from unittest.mock import Mock

import mcp_server.tools.manipulation  # noqa: F401
import mcp_server.tools.navigation as navigation
import pytest


def _response(status, result_data=''):
    return SimpleNamespace(
        status=SimpleNamespace(status=status, message='success'),
        result_data=result_data)


def _position(x=0.0, y=0.0, yaw_deg=0.0):
    return SimpleNamespace(
        status=SimpleNamespace(
            status=navigation.STATUS_SUCCESS, message='success'),
        x=x, y=y, yaw_deg=yaw_deg)


def _tracked_person(
        name='TestUserB', x=3.0, y=0.5, *,
        navigation_eligible=False, age_seconds=45.0):
    return json.dumps([{
        'track_id': 7,
        'item_name': 'person_7',
        'face_name': name,
        'position': {'x': x, 'y': y, 'z': 0.0},
        'age_seconds': age_seconds,
        'navigation_eligible': navigation_eligible,
        'state': 'last_known', }])


def test_visible_named_person_starts_one_drive_without_arrival_verification(
        monkeypatch):
    visible = _response(
        navigation.STATUS_SUCCESS,
        '[{"item_name":"person_0","face_name":"TestUserB",'
        '"x":3.0,"y":0.5}]')
    position = _position(x=10.0, y=20.0, yaw_deg=90.0)
    call_service = Mock(side_effect=[visible, position])
    drive = Mock(return_value={'status': 'success', 'result': {}})
    monkeypatch.setattr(navigation, 'call_service', call_service)
    monkeypatch.setattr(navigation, '_drive_to_person_item', drive)

    result = navigation.drive_to_person(person='TestUserB')

    assert result['status'] == 'success'
    assert call_service.call_count == 2
    mapped_person = drive.call_args.args[0]
    assert mapped_person['x'] == pytest.approx(9.5)
    assert mapped_person['y'] == pytest.approx(23.0)
    drive.assert_called_once_with(mapped_person, position, 1.3)
    assert result['result']['location_source'] == 'current_view'
    assert result['result']['target_verified'] is True
    assert result['result']['intermediate_check_performed'] is False


def test_visible_person_in_safe_band_skips_navigation(monkeypatch):
    visible = _response(
        navigation.STATUS_SUCCESS,
        '[{"item_name":"person_0","x":1.4,"y":0.1}]')
    position = _position(x=5.0, y=6.0, yaw_deg=30.0)
    monkeypatch.setattr(
        navigation, 'call_service', Mock(side_effect=[visible, position]))
    drive = Mock()
    monkeypatch.setattr(navigation, '_drive_to_person_item', drive)

    result = navigation.drive_to_person()

    assert result['status'] == 'success'
    assert result['result']['approached'] is False
    assert result['result']['person_distance'] == pytest.approx(
        math.hypot(1.4, 0.1))
    drive.assert_not_called()


def test_anonymous_request_scans_and_approaches_nearest_person(monkeypatch):
    visible = _response(navigation.STATUS_SUCCESS, '[]')
    scan = _response(
        navigation.STATUS_SUCCESS,
        '[{"item_name":"person_1","x":3.0,"y":0.0},'
        '{"item_name":"person_2","x":5.0,"y":0.0}]')
    position = _position()
    call_service = Mock(side_effect=[visible, scan, position])
    drive = Mock(return_value={'status': 'success', 'result': {}})
    monkeypatch.setattr(navigation, 'call_service', call_service)
    monkeypatch.setattr(navigation, '_drive_to_person_item', drive)

    result = navigation.drive_to_person(distance=1.3)

    assert result['status'] == 'success'
    assert call_service.call_args_list[1].args[2].action_type == (
        'get_look_around_objects')
    assert drive.call_args.args[0]['item_name'] == 'person_1'
    assert drive.call_args.args[2] == 1.3


def test_named_request_never_falls_back_to_other_or_anonymous_person(
        monkeypatch):
    visible = _response(
        navigation.STATUS_SUCCESS,
        '[{"item_name":"person_0","face_name":"TestUserC","x":1.0,"y":0.0}]')
    no_memory = _response(navigation.STATUS_SUCCESS, '[]')
    scan = _response(
        navigation.STATUS_SUCCESS,
        '[{"item_name":"person_1","face_name":"TestUserC","x":3.0,"y":0.0},'
        '{"item_name":"person_2","x":2.0,"y":0.0}]')
    call_service = Mock(side_effect=[visible, no_memory, scan])
    drive = Mock()
    monkeypatch.setattr(navigation, 'call_service', call_service)
    monkeypatch.setattr(navigation, '_drive_to_person_item', drive)

    result = navigation.drive_to_person(person='TestUserB')

    assert result['status'] == 'failure'
    assert result['error']['code'] == 'TARGET_PERSON_NOT_FOUND'
    assert call_service.call_count == 3
    drive.assert_not_called()


@pytest.mark.parametrize('yaw', [0., 90., 270.])
def test_remembered_person_is_approached_once_without_reacquisition(monkeypatch, yaw):
    services = Mock(side_effect=[
        _response(navigation.STATUS_SUCCESS, '[]'),
        _response(navigation.STATUS_SUCCESS, _tracked_person(x=3., y=.5, navigation_eligible=True)),
        _position(x=.2, y=-.3, yaw_deg=yaw)])
    drive = Mock(return_value={'status':'success','result':{}})
    turn = Mock(side_effect=AssertionError('No look/re-identification turn expected'))
    monkeypatch.setattr(navigation, 'call_service', services)
    monkeypatch.setattr(navigation, '_drive_to_person_item', drive)
    monkeypatch.setattr(navigation, '_turn_toward_map_position', turn)
    result = navigation.drive_to_person(person='TestUserB')
    assert result['status'] == 'success'
    assert services.call_count == 3
    assert json.loads(services.call_args_list[1].args[2].parameters)['navigation_only'] is True
    drive.assert_called_once()
    selected = drive.call_args.args[0]
    assert (selected['x'], selected['y']) == pytest.approx((3., .5))
    assert result['result']['location_source'] == 'last_known_position'
    assert result['result']['face_confirmed'] is False
    assert result['result']['memory_age_seconds'] == 45.
    turn.assert_not_called()


def test_ineligible_memory_and_no_matching_search_result_never_drive(monkeypatch):
    services = Mock(side_effect=[
        _response(navigation.STATUS_SUCCESS, '[]'),
        _response(navigation.STATUS_SUCCESS, _tracked_person(navigation_eligible=False)),
        _response(navigation.STATUS_SUCCESS, '[]')])
    drive = Mock()
    monkeypatch.setattr(navigation, 'call_service', services)
    monkeypatch.setattr(navigation, '_drive_to_person_item', drive)
    result = navigation.drive_to_person(person='TestUserB')
    assert result['error']['code'] == 'TARGET_PERSON_NOT_FOUND'
    drive.assert_not_called()


def test_failed_drive_to_memory_is_not_retried_or_followed_by_search(monkeypatch):
    services = Mock(side_effect=[
        _response(navigation.STATUS_SUCCESS, '[]'),
        _response(navigation.STATUS_SUCCESS, _tracked_person(navigation_eligible=True)),
        _position()])
    drive = Mock(return_value={'status':'failure','error':{'code':'DRIVE_TO_PERSON_FAILED'}})
    monkeypatch.setattr(navigation, 'call_service', services)
    monkeypatch.setattr(navigation, '_drive_to_person_item', drive)
    result = navigation.drive_to_person(person='TestUserB')
    assert result['status'] == 'failure'
    assert services.call_count == 3
    drive.assert_called_once()

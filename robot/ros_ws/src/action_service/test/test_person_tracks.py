import json
from types import SimpleNamespace

from action_service.action_service_node import ActionServiceNode


def response():
    return SimpleNamespace(status=SimpleNamespace(status=None, message=''), result_data='')


def fake_node(tracks):
    node = SimpleNamespace(_latest_person_tracks=tracks)

    def set_success(result, message, result_data=''):
        result.status.status = 1
        result.status.message = message
        result.result_data = result_data
        return result

    def set_failure(result, message, result_data=''):
        result.status.status = 0
        result.status.message = message
        result.result_data = result_data
        return result

    node.set_success = set_success
    node.set_failure = set_failure
    return node


def test_get_tracked_people_filters_name_and_navigation_eligibility():
    node = fake_node([{'track_id': 1, 'face_name': 'TestUserB', 'navigation_eligible': True, },
        {'track_id': 2, 'face_name': 'TestUserC', 'navigation_eligible': True, },
        {'track_id': 3, 'face_name': 'TestUserB', 'navigation_eligible': False, }])
    request = SimpleNamespace(parameters=json.dumps({
        'person': '  testuserb ',
        'navigation_only': True,
        'include_anonymous': False, }))
    result = ActionServiceNode._handle_get_tracked_people(node, request, response())
    tracks = json.loads(result.result_data)
    assert [track['track_id'] for track in tracks] == [1]


def test_get_tracked_people_reports_not_ready():
    result = ActionServiceNode._handle_get_tracked_people(fake_node(None), SimpleNamespace(parameters=''), response())
    assert result.status.status == 0
    assert 'not published' in result.status.message

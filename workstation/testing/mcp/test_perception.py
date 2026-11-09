import json
from types import SimpleNamespace

import mcp_server.tools.perception as perception


def test_get_tracked_people_returns_map_coordinates(monkeypatch):
    response = SimpleNamespace(status=SimpleNamespace(status=perception.STATUS_SUCCESS, message='success'), result_data=(
        '[{"track_id":4,"face_name":"TestUserB",'
        '"position":{"x":1.2,"y":-0.4,"z":0.0},'
        '"age_seconds":3.5,"state":"last_known",'
        '"navigation_eligible":true}]'))
    calls = []

    def call_service(name, action_type, request, timeout_sec):
        calls.append((name, action_type, request, timeout_sec))
        return response

    monkeypatch.setattr(perception, 'call_service', call_service)
    result = perception.get_tracked_people(person='TestUserB', navigation_only=True)
    assert result['status'] == 'success'
    assert result['result']['count'] == 1
    person = result['result']['people'][0]
    assert person['x'] == 1.2
    assert person['y'] == -0.4
    assert person['state'] == 'last_known'
    request = calls[0][2]
    assert request.action_type == 'get_tracked_people'
    assert json.loads(request.parameters) == {
        'include_anonymous': False,
        'navigation_only': True,
        'person': 'TestUserB', }


def test_get_tracked_people_reports_tracker_unavailable(monkeypatch):
    response = SimpleNamespace(status=SimpleNamespace(status=0, message='not ready'), result_data='')
    monkeypatch.setattr(perception, 'call_service', lambda *args, **kwargs: response)
    result = perception.get_tracked_people()
    assert result['status'] == 'failure'
    assert result['error']['code'] == 'PERSON_TRACKER_UNAVAILABLE'

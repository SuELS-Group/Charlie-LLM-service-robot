import json
from types import SimpleNamespace
from unittest.mock import Mock

import action_service.get_visible_objects_action as visible_module
from action_service.get_visible_objects_action import GetVisibleObjectsAction


def _node():
    logger = SimpleNamespace(info=Mock(), warning=Mock())
    return SimpleNamespace(get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=100_100_000_000)), log=SimpleNamespace(sub=lambda _name: logger), set_success=lambda response, message, result='': SimpleNamespace(status='success', message=message, result_data=result), set_failure=lambda response, message, result='': SimpleNamespace(status='failure', message=message, result_data=result), set_stopped=lambda response, message, result='': SimpleNamespace(status='stopped', message=message, result_data=result), )


def _message(detections):
    return SimpleNamespace(data=json.dumps({'header': {'frame_id': 'base_link', 'stamp':100.}, 'detections': detections, }))


def test_no_messages_returns_failure(monkeypatch):
    action = GetVisibleObjectsAction(_node())
    monkeypatch.setattr(visible_module.config.timeouts, 'first_message_seconds', 0.0)
    response = action.execute(None, SimpleNamespace())
    assert response.status == 'failure'
    assert response.message == 'No head detection frames received'


def test_valid_empty_message_returns_success(monkeypatch):
    action = GetVisibleObjectsAction(_node())
    monkeypatch.setattr(visible_module.config.timeouts, 'visible_seconds', 0.0)
    monkeypatch.setattr(action, '_wait_for_first_message', lambda _seconds: action.handle_detection(_message([])))
    response = action.execute(None, SimpleNamespace())
    assert response.status == 'success'
    assert json.loads(response.result_data) == []


def test_named_person_and_malformed_position_are_handled(monkeypatch):
    action = GetVisibleObjectsAction(_node())
    monkeypatch.setattr(visible_module.config.timeouts, 'visible_seconds', 0.0)
    detections = [{'label': 'person (TestUserC)', 'confidence': 0.9, 'position_3d': {'x': 1.0, 'y': 0.2, 'z': 1.0}, },
        {'label': 'bottle', 'confidence': 0.8, 'position_3d': None, }]
    monkeypatch.setattr(action, '_wait_for_first_message', lambda _seconds: action.handle_detection(_message(detections)))
    response = action.execute(None, SimpleNamespace())
    items = json.loads(response.result_data)
    assert response.status == 'success'
    assert len(items) == 1
    assert items[0]['item_name'] == 'person_0'
    assert items[0]['face_name'] == 'TestUserC'


def test_departed_person_is_not_kept_in_snapshot_history(monkeypatch):
    action = GetVisibleObjectsAction(_node())
    monkeypatch.setattr(visible_module.config.timeouts, 'visible_seconds', 0.)
    person = {'label': 'person', 'confidence': .9, 'position_3d': {'x': 1.3, 'y': 0., 'z': 1.}}
    def frames(_):
        action.handle_detection(_message([person]))
        action.handle_detection(_message([]))
    monkeypatch.setattr(action, '_wait_for_first_message', frames)
    result = action.execute(None, SimpleNamespace())
    assert result.status == 'success' and json.loads(result.result_data) == []


def test_delayed_old_frame_cannot_restore_a_departed_person(monkeypatch):
    action = GetVisibleObjectsAction(_node())
    monkeypatch.setattr(visible_module.config.timeouts, 'visible_seconds', 0.)
    def frames(_):
        action.handle_detection(_message([]))
        payload = json.loads(_message([{'label': 'person', 'confidence': .9,
            'position_3d': {'x': 1.3, 'y': 0., 'z': 1.}}]).data)
        payload['header']['stamp'] = 99.5
        action.handle_detection(SimpleNamespace(data=json.dumps(payload)))
    monkeypatch.setattr(action, '_wait_for_first_message', frames)
    assert json.loads(action.execute(None, SimpleNamespace()).result_data) == []


def test_old_camera_payload_is_not_current_person_evidence():
    action = GetVisibleObjectsAction(_node())
    action.snapshot_active = True
    payload = json.loads(_message([]).data)
    payload['header']['stamp'] = 90.
    action.handle_detection(SimpleNamespace(data=json.dumps(payload)))
    assert action.snapshot_messages == 0


def test_nearby_people_remain_separate_and_keep_their_track_ids(monkeypatch):
    action = GetVisibleObjectsAction(_node())
    monkeypatch.setattr(visible_module.config.timeouts,'visible_seconds',0.)
    detections = [{'label':'person','confidence':.9,'track_id':track,'face_name':name,
        'position_3d':{'x':x,'y':0.,'z':1.}} for track,name,x in [(4,'TestUserA',1.3),(8,'TestUserB',1.4)]]
    monkeypatch.setattr(action,'_wait_for_first_message',lambda _:action.handle_detection(_message(detections)))
    result = json.loads(action.execute(None,SimpleNamespace()).result_data)
    assert len(result) == 2
    assert {(p['track_id'],p['face_name']) for p in result} == {(4,'TestUserA'),(8,'TestUserB')}

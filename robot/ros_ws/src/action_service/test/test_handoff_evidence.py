import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from std_msgs.msg import String
from robot_common.handoff import RecipientEvidence
from action_service.take_from_hand_action import TakeFromHandAction


def frame():
    return {'header': {'stamp': 100., 'frame_id': 'base_link'}, 'detections': [
        {'label': 'person', 'confidence': .9, 'track_id': 4, 'face_name': 'Alex',
         'position_3d': {'x': 1.3, 'y': .1}}]}


@pytest.mark.parametrize('change', ['old', 'future', 'two', 'wrong_name', 'changed_track', 'near', 'nan', 'no_track'])
def test_invalid_recipient_evidence_fails_closed(change):
    payload = frame()
    target = payload['detections'][0]
    if change == 'old': payload['header']['stamp'] = 90.
    if change == 'future': payload['header']['stamp'] = 102.
    if change == 'two': payload['detections'].append(dict(target))
    if change == 'wrong_name': target['face_name'] = 'Someone else'
    if change == 'changed_track': target['track_id'] = 8
    if change == 'near': target['position_3d']['x'] = .2
    if change == 'nan': target['position_3d']['x'] = float('nan')
    if change == 'no_track': target['track_id'] = None
    evidence = RecipientEvidence()
    evidence.update(payload)
    target, reason = evidence.check(now=100.1, person='Alex', track_id=4)
    assert target is None and reason


def handoff():
    node = SimpleNamespace(log=Mock(), get_clock=lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(nanoseconds=100_000_000_000)))
    node.log.sub.return_value = Mock()
    action = TakeFromHandAction(node)
    action._gesture_session = 'active'
    action._armed_at = 99.5
    action._recipient_track = 4
    return action


@pytest.mark.parametrize('change', ['previous_session', 'old_frame', 'future_frame', 'wrong_action',
                                    'wrong_policy', 'nan_confidence', 'unstable'])
def test_unsafe_gesture_never_reaches_waiter(change):
    payload = {'session_id': 'active', 'stamp': 99.9, 'action': 'take_from_hand',
               'track_id': 4, 'recipient_policy': 'gesture_only', 'gesture': 'thumbs_up', 'confidence': .9, 'stable_frames': 3}
    if change == 'previous_session': payload['session_id'] = 'old'
    if change == 'old_frame': payload['stamp'] = 98.
    if change == 'future_frame': payload['stamp'] = 101.
    if change == 'wrong_action': payload['action'] = 'give_to_person'
    if change == 'wrong_policy': payload['recipient_policy'] = 'tracked'
    if change == 'nan_confidence': payload['confidence'] = float('nan')
    if change == 'unstable': payload['stable_frames'] = 1
    action = handoff()
    action.handle_gesture(String(data=json.dumps(payload)))
    assert action.latest_gesture is None


def test_valid_session_gesture_is_retained():
    action = handoff()
    action.handle_gesture(String(data=json.dumps({'session_id': 'active', 'stamp': 99.9,
        'action': 'take_from_hand', 'track_id': 4, 'recipient_policy': 'gesture_only', 'gesture': 'thumbs_up', 'confidence': .9, 'stable_frames': 3})))
    assert action.latest_gesture['gesture'] == 'thumbs_up'


def other_person(track=8, name='TestUserA', x=.9, y=0.):
    return {'label':'person','confidence':.95,'track_id':track,'face_name':name,
        'position_3d':{'x':x,'y':y}}


def test_named_recipient_is_selected_even_when_a_different_person_is_closer():
    payload = frame()
    payload['detections'].append(other_person())
    evidence = RecipientEvidence()
    evidence.update(payload)
    target,reason = evidence.check(now=100.1, person=' Alex ')
    assert reason is None and target['track_id'] == 4 and target['visible_person_count'] == 2


def test_unnamed_recipient_is_nearest_and_does_not_switch_when_someone_else_approaches():
    payload = frame()
    payload['detections'].append(other_person())
    evidence = RecipientEvidence()
    evidence.update(payload)
    target,reason = evidence.check(now=100.1)
    assert reason is None and target['track_id'] == 8
    payload['header']['stamp'] = 100.1
    payload['detections'][0]['position_3d']['x'] = .8
    evidence.update(payload)
    target,reason = evidence.check(now=100.2, track_id=8)
    assert reason is None and target['track_id'] == 8


def test_missing_named_person_never_falls_back_to_a_visible_bystander():
    payload = frame()
    payload['detections'].append(other_person())
    evidence = RecipientEvidence()
    evidence.update(payload)
    target,reason = evidence.check(now=100.1, person='TestUserB')
    assert target is None and 'requested recipient' in reason


@pytest.mark.parametrize('fault', ['capture_expired', 'session_changed', 'policy_changed'])
def test_gesture_is_revalidated_when_consumed(monkeypatch, fault):
    import action_service.common.handoff as module
    action = handoff()
    action.latest_gesture = {'session_id': 'active', 'track_id': 4, 'stamp': 99.9,
        'recipient_policy': 'gesture_only', 'gesture': 'thumbs_up', 'confidence': .9, 'received_monotonic': 0.}
    if fault == 'capture_expired':
        action.node.get_clock = lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=101_200_000_000))
    if fault == 'session_changed': action._gesture_session = 'new'
    if fault == 'policy_changed': action.latest_gesture['recipient_policy'] = 'tracked'
    action._disarm_gesture_detection = Mock()
    ticks = iter([0., 0., 0., 0., 0., 2.])
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
    assert action._wait_for_gesture(1.) is None
    action._disarm_gesture_detection.assert_not_called()


def test_valid_consumed_gesture_disarms_before_close_or_release():
    import time
    action = handoff()
    action.latest_gesture = {'session_id': 'active', 'track_id': 4, 'stamp': 99.9,
        'recipient_policy': 'gesture_only', 'gesture': 'thumbs_up', 'confidence': .9, 'received_monotonic': time.monotonic()}
    action._disarm_gesture_detection = Mock()
    assert action._wait_for_gesture(1.)['gesture'] == 'thumbs_up'
    action._disarm_gesture_detection.assert_called_once()

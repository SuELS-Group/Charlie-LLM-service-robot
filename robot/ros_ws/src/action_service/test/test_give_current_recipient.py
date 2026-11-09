"""Handoff keeps the approach selection and does not re-identify people."""
import json
from types import MethodType
from unittest.mock import Mock

import pytest
from std_msgs.msg import String
from action_service.common.handoff import HandoffActionBase
from test_give_to_person_action import _ready_action as give_ready
from test_take_from_hand_action import _ready_action as take_ready


@pytest.mark.parametrize('ready', [take_ready, give_ready])
@pytest.mark.parametrize('approach_track', [None, 4, 99])
def test_handoff_completes_without_face_body_or_track_recheck(ready, approach_track):
    node, action = ready()
    node.check_handoff_recipient = Mock(side_effect=AssertionError('No person recheck after approach'))
    action._wait_for_gesture = MethodType(HandoffActionBase._wait_for_gesture, action)
    def ready_for_gesture(_action):
        action.handle_gesture(String(data=json.dumps({
            'session_id': action._gesture_session, 'action': action.action_key,
            'recipient_policy': 'gesture_only', 'track_id': None, 'stamp': 100.,
            'gesture': 'thumbs_up', 'confidence': .9, 'stable_frames': 3})))
    node.announce_handoff_ready.side_effect = ready_for_gesture
    result = action.execute(None, object(), {'person': 'TestUserA', 'recipient_track': approach_track})
    assert result[0] == 'success'
    node.check_handoff_recipient.assert_not_called()
    transfer = action._close_gripper if action.action_key == 'take_from_hand' else action._open_gripper
    transfer.assert_called_once()
    controls = [json.loads(call.args[0].data) for call in node.handoff_gesture_control_pub.publish.call_args_list]
    armed = next(payload for payload in controls if payload['command'] == 'arm')
    assert armed['recipient_policy'] == 'gesture_only' and armed['track_id'] is None
    assert not action._gesture_session


@pytest.mark.parametrize('ready', [take_ready, give_ready])
@pytest.mark.parametrize('track', [None, 8, 99])
def test_session_gesture_does_not_depend_on_yolo_track(ready, track):
    node, action = ready()
    action._gesture_session, action._armed_at, action._recipient_track = 'new', 99.5, 4
    action.handle_gesture(String(data=json.dumps({'session_id':'new','action':action.action_key,
        'recipient_policy':'gesture_only','track_id':track,'stamp':99.9,
        'gesture':'thumbs_up','confidence':.9,'stable_frames':3})))
    action._wait_for_gesture = MethodType(HandoffActionBase._wait_for_gesture, action)
    assert action._wait_for_gesture(.2)['track_id'] == track
    assert not action._gesture_session


@pytest.mark.parametrize('ready', [take_ready, give_ready])
@pytest.mark.parametrize('fault', ['old_session','old_frame','future_frame','unstable','wrong_policy','wrong_action'])
def test_handoff_still_rejects_invalid_gesture(ready, fault):
    _, action = ready()
    action._gesture_session, action._armed_at = 'new', 99.5
    event = {'session_id':'new','action':action.action_key,'recipient_policy':'gesture_only',
        'stamp':99.9,'gesture':'thumbs_up','confidence':.9,'stable_frames':3}
    if fault == 'old_session': event['session_id'] = 'old'
    if fault == 'old_frame': event['stamp'] = 98.
    if fault == 'future_frame': event['stamp'] = 101.
    if fault == 'unstable': event['stable_frames'] = 1
    if fault == 'wrong_policy': event['recipient_policy'] = 'tracked'
    if fault == 'wrong_action': event['action'] = 'something_else'
    action.handle_gesture(String(data=json.dumps(event)))
    assert action.latest_gesture is None

import json
import threading
import time
from itertools import count
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from gesture_detection_node.gesture_detection_node import GestureDetectionNode
from gesture_detection_node.gesture_event_gate import GestureEventGate


def detector():
    node = object.__new__(GestureDetectionNode)
    node.state_lock = threading.RLock()
    node.gesture_armed = False
    node.handoff_session = ''
    node.hand_event_gate = GestureEventGate(3, 2)
    node._refresh_sensor_subscriptions = Mock()
    node.log = Mock()
    node.person_bbox = {'center_x': 50, 'center_y': 50, 'width': 50, 'height': 80}
    node.person_track = 4
    node._people_by_track = {4:dict(node.person_bbox)}
    node.last_person_detection_monotonic = time.monotonic()
    node.person_capture_stamp = 100.
    node._pointing_gate = lambda: True
    node.get_clock = lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=100_000_000_000))
    node.overlay_publisher = None
    node.gesture_publisher = Mock()
    node.processor = Mock()
    node.processor.process_hands.return_value = (SimpleNamespace(name='thumbs_up', confidence=.9), None)
    node.control_callback(String(data=json.dumps({'command': 'arm', 'session_id': 'new',
        'armed_at': 99.9, 'track_id': 4, 'action': 'give_to_person'})))
    return node


_frames = count(900_000_000, 1_000_000)


def image(stamp=99):
    message = CompressedImage()
    message.header.stamp.sec = stamp
    message.header.stamp.nanosec = next(_frames) if stamp == 99 else 0
    return np.zeros((100, 100, 3), dtype=np.uint8), message


def test_preexisting_thumb_needs_release_then_new_stable_gesture():
    node = detector()
    for _ in range(5): node._process_handoff_gesture(*image())
    node.gesture_publisher.publish.assert_not_called()
    node.processor.process_hands.return_value = (SimpleNamespace(name='', confidence=0), None)
    for _ in range(2): node._process_handoff_gesture(*image())
    node.processor.process_hands.return_value = (SimpleNamespace(name='thumbs_up', confidence=.9), None)
    for _ in range(3): node._process_handoff_gesture(*image())
    payload = json.loads(node.gesture_publisher.publish.call_args.args[0].data)
    assert payload['session_id'] == 'new' and payload['track_id'] == 4
    assert payload['action'] == 'give_to_person'
    assert node.processor.process_hands.call_args.args[0].shape[:2] != (100, 100)


def test_old_frame_and_wrong_person_never_reach_hand_model():
    node = detector()
    node._process_handoff_gesture(*image(90))
    node._people_by_track = {8:dict(node.person_bbox)}
    node._process_handoff_gesture(*image())
    node.processor.process_hands.assert_not_called()


def test_inference_finishing_after_rearm_cannot_emit_old_event():
    node = detector()
    node.hand_event_gate.reset()
    def infer(*_, **__):
        node.handoff_session = 'newer'
        return SimpleNamespace(name='thumbs_up', confidence=.9), None
    node.processor.process_hands.side_effect = infer
    node._process_handoff_gesture(*image())
    node.gesture_publisher.publish.assert_not_called()


def test_replayed_image_does_not_count_as_additional_stable_frames():
    node = detector()
    node.hand_event_gate.reset()
    frame = image()
    for _ in range(5): node._process_handoff_gesture(*frame)
    assert node.processor.process_hands.call_count == 1
    node.gesture_publisher.publish.assert_not_called()


def person_frame(stamp, tracks):
    return String(data=json.dumps({'source':'obj','header':{'stamp':stamp},'detections':[
        {'label':'person', 'track_id':track, 'bbox_2d':bbox} for track,bbox in tracks]}))


def test_selected_persons_crop_is_used_with_multiple_people_in_the_image():
    node = detector()
    node._clear_markers = Mock()
    chosen = {'center_x':70,'center_y':50,'width':30,'height':60}
    other = {'center_x':15,'center_y':50,'width':20,'height':70}
    node.person_detection_callback(person_frame(100.01,[(8,other),(4,chosen)]))
    node.get_clock = lambda: SimpleNamespace(now=lambda:SimpleNamespace(nanoseconds=100_100_000_000))
    assert node.person_count == 2 and node.person_bbox is None
    node.hand_event_gate.reset()
    pixels = np.tile(np.arange(100,dtype=np.uint8)[None,:,None],(100,1,3))
    for nanosecond in (20_000_000,30_000_000,40_000_000):
        frame = CompressedImage()
        frame.header.stamp.sec,frame.header.stamp.nanosec = 100,nanosecond
        node._process_handoff_gesture(pixels,frame)
    crop = node.processor.process_hands.call_args.args[0]
    assert crop[0,0,0] == 53 and crop.shape[1] == 33
    event = json.loads(node.gesture_publisher.publish.call_args.args[0].data)
    assert event['track_id'] == 4


def test_other_persons_gesture_cannot_replace_a_departed_recipient():
    node = detector()
    node._clear_markers = Mock()
    node.person_detection_callback(person_frame(100.01,[(8,node.person_bbox)]))
    node.get_clock = lambda: SimpleNamespace(now=lambda:SimpleNamespace(nanoseconds=100_100_000_000))
    frame = CompressedImage()
    frame.header.stamp.sec,frame.header.stamp.nanosec = 100,20_000_000
    node._process_handoff_gesture(np.zeros((100,100,3),dtype=np.uint8),frame)
    node.processor.process_hands.assert_not_called()
    node.gesture_publisher.publish.assert_not_called()


def current_detector():
    node = detector()
    node.current_recipient_bbox = None
    node.current_recipient_stamp = node.current_recipient_received = 0.
    node.control_callback(String(data=json.dumps({'command':'arm','session_id':'new',
        'armed_at':99.9,'track_id':None,'action':'give_to_person','recipient_policy':'current_visible'})))
    node.get_clock = lambda: SimpleNamespace(now=lambda:SimpleNamespace(nanoseconds=100_100_000_000))
    return node


def depth_people(stamp=100.01,track=None,near_distance=1.1):
    return {'source':'obj','header':{'stamp':stamp,'frame_id':'base_link'},'detections':[
        {'label':'person','confidence':.9,'track_id':123,'face_name':'Old name',
         'position_3d':{'x':1.8,'y':0.},
         'bbox_2d':{'center_x':15,'center_y':50,'width':20,'height':70}},
        {'label':'person','confidence':.9,'track_id':track,'face_name':None,
         'position_3d':{'x':near_distance,'y':0.},
         'bbox_2d':{'center_x':70,'center_y':50,'width':30,'height':60}}]}


@pytest.mark.parametrize('fault',['empty','stale','near','wrong_frame','no_depth','bad_bbox'])
def test_current_delivery_requires_current_reachable_body_evidence(fault):
    node = current_detector()
    payload = depth_people()
    if fault == 'empty': payload['detections'] = []
    if fault == 'stale': payload['header']['stamp'] = 95.
    if fault == 'near': payload['detections'][1]['position_3d']['x'] = .1
    if fault == 'wrong_frame': payload['header']['frame_id'] = 'map'
    if fault == 'no_depth':
        for person in payload['detections']: person.pop('position_3d')
    if fault == 'bad_bbox': payload['detections'][1].pop('bbox_2d')
    node.handoff_person_detection_callback(String(data=json.dumps(payload)))
    message = CompressedImage()
    message.header.stamp.sec,message.header.stamp.nanosec = 100,20_000_000
    node._process_handoff_gesture(np.zeros((100,100,3),dtype=np.uint8),message)
    node.processor.process_hands.assert_not_called()


def test_handoff_does_not_compete_with_pointing_model():
    node=detector()
    node._pointing_gate=lambda *_:True
    node._decode_color=Mock(return_value=np.zeros((100,100,3),dtype=np.uint8))
    node._process_handoff_gesture=Mock()
    node._process_pointing=Mock()
    node.image_callback(image()[1])
    node._process_handoff_gesture.assert_called_once()
    node._process_pointing.assert_not_called()


def gesture_only_detector(action='take_from_hand'):
    node = detector()
    node.control_callback(String(data=json.dumps({'command':'arm','session_id':'transfer',
        'armed_at':99.9,'track_id':None,'action':action,'recipient_policy':'gesture_only'})))
    node._handoff_person_evidence = Mock(side_effect=AssertionError('No body or track lookup during handoff'))
    node._handoff_person_bbox = Mock(side_effect=AssertionError('No person crop required'))
    node.get_clock = lambda: SimpleNamespace(now=lambda:SimpleNamespace(nanoseconds=100_100_000_000))
    return node


@pytest.mark.parametrize('action', ['take_from_hand', 'give_to_person'])
def test_gesture_only_handoff_uses_full_image_without_any_person_evidence(action):
    node = gesture_only_detector(action)
    pixels = np.zeros((100, 100, 3), dtype=np.uint8)
    for index, name in enumerate(['thumbs_up'] * 3 + [''] * 2 + ['thumbs_up'] * 3):
        node.processor.process_hands.return_value = (SimpleNamespace(name=name,confidence=.9,reason='neutral'),None)
        message = CompressedImage()
        message.header.stamp.sec,message.header.stamp.nanosec = 100,10_000_000 + index * 10_000_000
        node._process_handoff_gesture(pixels,message)
        if index < 7:
            node.gesture_publisher.publish.assert_not_called()
    node.gesture_publisher.publish.assert_called_once()
    payload = json.loads(node.gesture_publisher.publish.call_args.args[0].data)
    assert payload['recipient_policy'] == 'gesture_only' and payload['track_id'] is None
    assert payload['action'] == action and payload['session_id'] == 'transfer'
    assert node.processor.process_hands.call_args.args[0].shape == pixels.shape


@pytest.mark.parametrize('fault', ['old_image', 'future_image', 'disarmed', 'rearm_during_inference', 'slow_inference'])
def test_gesture_only_policy_preserves_frame_and_session_checks(fault):
    node = gesture_only_detector()
    node.hand_event_gate.reset()
    node.hand_event_gate.active_frames = 2
    pixels, message = image(100)
    if fault == 'old_image': message.header.stamp.sec = 90
    if fault == 'future_image': message.header.stamp.sec = 101
    if fault == 'disarmed': node.gesture_armed = False
    def infer(*_a, **_k):
        if fault == 'rearm_during_inference': node.handoff_session = 'other'
        if fault == 'slow_inference':
            node.get_clock = lambda: SimpleNamespace(now=lambda:SimpleNamespace(nanoseconds=102_000_000_000))
        return SimpleNamespace(name='thumbs_up',confidence=.9),None
    node.processor.process_hands.side_effect = infer
    node._process_handoff_gesture(pixels,message)
    node.gesture_publisher.publish.assert_not_called()

import pytest
from gesture_detection_node.gesture_event_gate import TimedGestureEventGate


def ready():
    gate=TimedGestureEventGate()
    gate.reset(require_release=True)
    assert not gate.update(False,stamp=0.)
    assert not gate.update(False,stamp=.2)
    assert not gate.latched
    return gate


def test_slow_fresh_frames_complete_confirmation():
    gate=ready()
    assert not gate.update(True,stamp=.3)
    assert not gate.update(True,stamp=.9)
    assert gate.update(True,stamp=1.5)
    assert gate.active_frames==3


def test_one_detection_miss_does_not_discard_valid_window():
    gate=ready()
    for stamp,active in ((.3,True),(.4,False),(.5,True)):
        assert not gate.update(active,stamp=stamp)
    assert gate.update(True,stamp=.7)


def test_brief_invalid_view_does_not_require_another_neutral_phase():
    gate=ready()
    gate.update(True,stamp=.3)
    gate.invalidate()
    assert not gate.latched
    for stamp in (.5,.8):assert not gate.update(True,stamp=stamp)
    assert gate.update(True,stamp=1.1)


def test_preexisting_thumb_still_requires_release():
    gate=TimedGestureEventGate()
    gate.reset(require_release=True)
    for index in range(20):assert not gate.update(True,stamp=index*.2)
    assert gate.latched


def test_duplicate_burst_and_expired_samples_do_not_confirm():
    gate=ready()
    for stamp in (.3,.3,.3,.31,.32):assert not gate.update(True,stamp=stamp)
    assert not gate.update(True,stamp=3.)
    assert gate.active_frames==1


def test_same_held_thumb_only_emits_once():
    gate=ready()
    assert [gate.update(True,stamp=stamp) for stamp in (.3,.6,.9,1.2,1.5)]==[False,False,True,False,False]
    gate.reset(require_release=True)
    assert not gate.update(True,stamp=1.6)


@pytest.mark.parametrize('window',[0.,float('nan'),30.])
def test_confirmation_window_cannot_be_unbounded(window):
    with pytest.raises(ValueError):TimedGestureEventGate(window_seconds=window)


def test_slow_window_event_is_accepted_by_real_robot_validator():
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock
    import numpy as np
    from sensor_msgs.msg import CompressedImage
    from std_msgs.msg import String
    from test_handoff_session import gesture_only_detector
    from action_service.give_to_person_action import GiveToPersonAction
    detector=gesture_only_detector('give_to_person')
    detector.hand_event_gate=TimedGestureEventGate()
    detector.hand_event_gate.reset(require_release=True)
    for capture,active in ((100.,False),(100.2,False),(100.3,True),(100.9,True),(101.5,True)):
        clock_ns=int((capture+.01)*1e9)
        detector.get_clock=lambda value=clock_ns:SimpleNamespace(now=lambda:SimpleNamespace(nanoseconds=value))
        detector.processor.process_hands.return_value=(SimpleNamespace(
            name='thumbs_up' if active else '',confidence=.95,reason='neutral'),None)
        image=CompressedImage()
        image.header.stamp.sec=int(capture)
        image.header.stamp.nanosec=int(round((capture%1)*1e9))
        detector._process_handoff_gesture(np.zeros((100,100,3),dtype=np.uint8),image)
    assert detector.gesture_publisher.publish.call_count==1
    robot=GiveToPersonAction(SimpleNamespace(log=Mock(),get_clock=detector.get_clock))
    robot._gesture_session='transfer';robot._armed_at=99.9
    robot.handle_gesture(detector.gesture_publisher.publish.call_args.args[0])
    assert robot.latest_gesture['stable_frames']==3

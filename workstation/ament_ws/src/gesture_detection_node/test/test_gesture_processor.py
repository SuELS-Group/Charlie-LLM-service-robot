import numpy as np
import pytest
from gesture_detection_node.gesture_event_gate import GestureEventGate
from gesture_detection_node.gesture_processor import (GestureProcessor, world_forearm_direction, )


def test_held_handoff_gesture_emits_only_one_event():
    gate = GestureEventGate(stable_frames=3, release_frames=2)
    events = [gate.update(True) for _ in range(10)]
    assert events == [False, False, True] + [False] * 7


def test_handoff_gesture_requires_release_before_next_event():
    gate = GestureEventGate(stable_frames=2, release_frames=3)
    assert not gate.update(True)
    assert gate.update(True)
    assert not gate.update(False)
    assert not gate.update(False)
    assert not gate.update(True)
    assert not gate.update(False)
    assert not gate.update(False)
    assert not gate.update(False)
    assert not gate.update(True)
    assert gate.update(True)


def test_pointing_processor_declares_only_right_arm_landmarks():
    assert GestureProcessor.RIGHT_LANDMARKS == {'shoulder': 12, 'elbow': 14, 'wrist': 16, 'index': 20, }


def test_right_angle_helper_recognizes_extended_arm():
    angle = GestureProcessor._joint_angle_degrees(shoulder=np.asarray([0.0, 0.0, 0.0]), elbow=np.asarray([1.0,
                                                                                                          0.0,
                                                                                                          0.0]), wrist=np.asarray([2.0,
                                                                                                                                   0.0,
                                                                                                                                   0.0]), )
    assert angle == pytest.approx(180.0)


def test_existing_world_forearm_direction_is_normalized():
    landmarks = [type('Landmark', (), {'x': 0.0, 'y': 0.0, 'z': 0.0})() for _ in range(21)]
    landmarks[14] = type('Landmark', (), {'x': 1.0, 'y': 2.0, 'z': 3.0})()
    landmarks[16] = type('Landmark', (), {'x': 4.0, 'y': 6.0, 'z': 3.0})()
    direction = world_forearm_direction(landmarks)
    assert direction == pytest.approx([0.6, 0.8, 0.0])


@pytest.mark.parametrize('rotation_name', ['', 'ROTATE_90_CLOCKWISE', 'ROTATE_90_COUNTERCLOCKWISE', 'ROTATE_180'], )
def test_rotation_pixel_mapping_round_trips(rotation_name):
    processor = object.__new__(GestureProcessor)
    processor.rotation_name = rotation_name
    width, height = 640, 480
    raw = (123.25, 234.5)
    rotated = processor._raw_to_rotated(*raw, width, height)
    recovered = processor._rotated_to_raw(*rotated, width, height)
    assert recovered == pytest.approx(raw)

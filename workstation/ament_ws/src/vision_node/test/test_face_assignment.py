import threading
from unittest.mock import Mock

import numpy as np
import pytest

from vision_node.face_processor import FaceProcessor, apply_face_mapping_result


def person(track_id, cx, cy, width, height, points=None):
    return {
        'track_id': track_id,
        'bbox_2d': {'center_x': cx, 'center_y': cy, 'width': width, 'height': height, },
        'segmentation_2d': {'points': points or []}, }


def test_face_is_assigned_to_only_one_overlapping_person():
    faces = [{'location': (20, 60, 60, 20), 'face_name': 'TestUserB', 'face_confidence': 0.4, 'face_distance': 0.4, }]
    people = [person(10, 40, 60, 70, 110, [[10, 5], [75, 5], [75, 115], [10, 115]]),
        person(11, 65, 60, 100, 115, [[15, 5], [115, 5], [115, 115], [15, 115]]), ]
    assignments = FaceProcessor._assign_faces_to_people(faces, people, scale=1.0)
    assert list(assignments) == [10]
    assert assignments[10]['face_name'] == 'TestUserB'


def test_two_faces_are_assigned_one_to_one():
    faces = [{'location': (10, 35, 40, 5), 'face_name': 'TestUserC', 'face_distance': 0.4},
        {'location': (10, 135, 40, 105), 'face_name': 'TestUserA', 'face_distance': 0.4}, ]
    people = [person(1, 20, 50, 40, 100), person(2, 120, 50, 40, 100)]
    assignments = FaceProcessor._assign_faces_to_people(faces, people, scale=1.0)
    assert assignments[1]['face_name'] == 'TestUserC'
    assert assignments[2]['face_name'] == 'TestUserA'


def test_mapping_accepts_face_at_strict_threshold():
    detection = {'label': 'person', 'track_id': 7}
    face_info = {
        'face_name': 'TestUserD',
        'face_confidence': 0.60,
        'face_distance': 0.60,
    }

    name = apply_face_mapping_result(detection, face_info, 0.60)

    assert name == 'TestUserD'
    assert detection['label'] == 'person (TestUserD)'
    assert detection['face_name'] == 'TestUserD'
    assert detection['face_distance'] == 0.60
    assert detection['face_candidate_name'] == 'TestUserD'
    assert detection['face_candidate_distance'] == 0.60


def test_mapping_hides_permissive_candidate_from_rviz_identity():
    detection = {
        'label': 'person (TestUserD)',
        'face_name': 'TestUserD',
        'face_distance': 0.55,
    }
    face_info = {
        'face_name': 'TestUserF',
        'face_confidence': 0.65,
        'face_distance': 0.65,
    }

    name = apply_face_mapping_result(detection, face_info, 0.60)

    assert name == 'unknown'
    assert detection['label'] == 'person'
    assert 'face_name' not in detection
    assert 'face_distance' not in detection
    assert detection['face_candidate_name'] == 'TestUserF'
    assert detection['face_candidate_distance'] == 0.65


class _CropFallbackFaceRecognition:

    def __init__(self):
        self.location_calls = 0

    def face_locations(self, image, model=None):
        self.location_calls += 1
        if self.location_calls == 1:
            return []
        return [(10, 40, 50, 10)]

    @staticmethod
    def face_encodings(image, locations):
        return [np.asarray([0.1], dtype=np.float64)]

    @staticmethod
    def face_distance(known, encoding):
        return np.asarray([0.42], dtype=np.float64)


def processor_for_crop_fallback():
    processor = object.__new__(FaceProcessor)
    processor.logger = Mock()
    processor.detect_model = 'hog'
    processor.max_input_size = 480
    processor.crop_max_input_size = 0
    processor.max_faces_per_request = 3
    processor.min_face_height = 30
    processor.match_threshold = 0.60
    processor.lock = threading.Lock()
    processor.known_faces = {'TestUserC': [np.asarray([0.1], dtype=np.float64)], }
    processor._fr = _CropFallbackFaceRecognition()
    processor._fr_available = True
    return processor


def test_native_person_crop_recognizes_face_missed_in_resized_frame():
    processor = processor_for_crop_fallback()
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    results = processor._recognize_frame(image, [person(7, 150, 250, 100, 300)])
    assert results[7]['face_name'] == 'TestUserC'
    assert results[7]['face_distance'] == 0.42
    assert processor._fr.location_calls == 2


@pytest.mark.parametrize('distance', [float('nan'), float('inf'), -.1, None, 'invalid'])
def test_invalid_face_distance_never_confirms_an_identity(distance):
    detection = {'label': 'person'}
    assert apply_face_mapping_result(detection, {'face_name': 'TestUserA',
        'face_distance': distance}, .60) == 'unknown'
    assert 'face_name' not in detection

import json
from types import SimpleNamespace

import action_service.common.detection as detection


def _message(label='person (TestUserC)', position=None):
    return SimpleNamespace(data=json.dumps({
        'header': {'frame_id': 'base_link'},
        'detections': [{
            'label': label,
            'confidence': 0.9,
            'position_3d': position or {'x': 3.0, 'y': 4.0, 'z': 0.0}, }], }))


def test_named_person_is_canonicalized_and_identity_is_preserved():
    _, detections = detection.parse_detection_message(_message())
    det, label, score = next(detection.iter_object_detections(detections))
    assert label == 'person'
    assert score == 0.9
    assert detection.person_name_from_label(det['label']) == 'TestUserC'


def test_invalid_position_does_not_create_detection_record():
    assert detection.build_detection_record(None, 'person', 0.9) is None
    assert detection.build_detection_record({'x': 1.0}, 'person', 0.9) is None


def test_transformed_record_keeps_robot_relative_distance(monkeypatch):
    transform = SimpleNamespace()
    node = SimpleNamespace(tf_buffer=SimpleNamespace(lookup_transform=lambda *args, **kwargs: transform))

    def transform_point(point, _transform):
        return SimpleNamespace(point=SimpleNamespace(x=point.point.x + 10.0, y=point.point.y + 20.0, z=point.point.z))

    monkeypatch.setattr(detection, 'do_transform_point', transform_point)
    records = detection.collect_transformed_detection_records(node, _message(), 'map')
    assert len(records) == 1
    assert records[0]['class_id'] == 'person'
    assert records[0]['face_name'] == 'TestUserC'
    assert records[0]['x'] == 13.0
    assert records[0]['y'] == 24.0
    assert records[0]['distance'] == 5.0

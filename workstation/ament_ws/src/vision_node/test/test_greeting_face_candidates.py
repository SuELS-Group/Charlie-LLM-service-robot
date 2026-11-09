from vision_node.greeting_mode_node import GreetingModeNode


def test_greeting_uses_permissive_face_candidate():
    node = object.__new__(GreetingModeNode)

    candidates = node._known_face_candidates([{
        'label': 'person',
        'face_candidate_name': 'TestUserF',
        'face_candidate_distance': 0.65,
    }])

    assert len(candidates) == 1
    assert candidates[0]['face_name'] == 'TestUserF'
    assert candidates[0]['face_confidence'] == 0.65


def test_greeting_rejects_candidate_above_its_threshold():
    node = object.__new__(GreetingModeNode)

    candidates = node._known_face_candidates([{
        'label': 'person',
        'face_candidate_name': 'TestUserF',
        'face_candidate_distance': 0.76,
    }])

    assert candidates == []


def test_greeting_rejects_candidate_without_distance():
    node = object.__new__(GreetingModeNode)

    candidates = node._known_face_candidates([{
        'label': 'person',
        'face_candidate_name': 'TestUserF',
    }])

    assert candidates == []


def test_greeting_supports_existing_face_fields():
    node = object.__new__(GreetingModeNode)

    candidates = node._known_face_candidates([{
        'label': 'person (TestUserD)',
        'face_name': 'TestUserD',
        'face_distance': 0.55,
    }])

    assert len(candidates) == 1
    assert candidates[0]['face_name'] == 'TestUserD'
    assert candidates[0]['face_confidence'] == 0.55

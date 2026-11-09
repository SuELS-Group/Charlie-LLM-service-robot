from types import SimpleNamespace

import pytest
from robot_common.gripper_feedback import GripperFeedback


def sample(stamp, position=.1, load=-8.):
    return SimpleNamespace(name=['joint_gripper_finger_left'], position=[position], effort=[load],
        header=SimpleNamespace(stamp=SimpleNamespace(sec=int(stamp), nanosec=round((stamp-int(stamp))*1e9))))


def contact(feedback, now=100.6):
    return feedback.contact_indicated(now=now, started_at=100., target=-.35, start_position=.56)


def test_fresh_stable_closing_load_is_an_indication_only():
    feedback = GripperFeedback()
    for stamp in [100., 100.1, 100.2, 100.3, 100.4, 100.5]: feedback.update(sample(stamp))
    assert contact(feedback)
    assert feedback.latest_position(100.6) == .1


@pytest.mark.parametrize('fault', ['moving', 'low_load', 'wrong_load_direction', 'stale_capture',
    'stale_receipt', 'future_capture', 'before_close', 'at_target', 'missing_effort', 'invalid_effort',
    'gapped_frames', 'insufficient_frames', 'insufficient_duration', 'no_closing_progress'])
def test_motion_or_missing_evidence_never_counts_as_contact(fault, monkeypatch):
    feedback = GripperFeedback()
    stamps = [100., 100.1, 100.2, 100.3, 100.4, 100.5]
    if fault == 'gapped_frames': stamps = [100., 100.49, 100.5]
    if fault == 'insufficient_frames': stamps = [100., 100.5]
    if fault == 'insufficient_duration': stamps = [100.3, 100.4, 100.5]
    for i, stamp in enumerate(stamps):
        position, load = (.5-.07*i if fault == 'moving' else .1), -8.
        if fault == 'low_load': load = -.6
        if fault == 'wrong_load_direction': load = 8.
        if fault == 'at_target': position = -.35
        if fault == 'invalid_effort': load = float('nan')
        if fault == 'before_close': stamp -= 1.
        if fault == 'no_closing_progress': position = .55
        message = sample(stamp, position, load)
        if fault == 'missing_effort': message.effort = []
        feedback.update(message)
    now = 102. if fault == 'stale_capture' else 100.4 if fault == 'future_capture' else 100.6
    if fault == 'stale_receipt':
        import robot_common.gripper_feedback as module
        monkeypatch.setattr(module.time, 'monotonic', lambda: 10**12)
    assert not contact(feedback, now)


def test_replayed_frame_cannot_restore_expired_contact():
    feedback = GripperFeedback()
    for stamp in [100., 100.1, 100.2, 100.3, 100.4, 100.5]: feedback.update(sample(stamp))
    feedback.update(sample(100.6, .08, -.5))
    feedback.update(sample(100.5))
    assert not contact(feedback, 100.7)


@pytest.mark.parametrize('field,value', [('min_closing_load_pct', 0.), ('stable_seconds', 0.),
    ('position_span_rad', .2), ('max_age_seconds', float('nan'))])
def test_invalid_thresholds_do_not_disable_evidence_checks(field, value):
    with pytest.raises(ValueError): GripperFeedback(SimpleNamespace(**{field:value}))

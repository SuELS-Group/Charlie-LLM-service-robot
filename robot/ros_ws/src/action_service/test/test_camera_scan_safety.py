from types import SimpleNamespace
from unittest.mock import Mock

from action_service.get_look_around_objects_action import GetLookAroundObjectsAction


def test_failed_scan_head_motion_never_returns_a_successful_scan():
    n = SimpleNamespace(log=Mock(),
        set_failure=lambda _r, text: ('failure', text),
        set_stopped=lambda _r, text: ('stopped', text))
    n.log.sub.return_value = Mock()
    action = GetLookAroundObjectsAction(n)
    action.motion = Mock()
    action.motion.move_head.return_value = False
    result = action.execute(None, SimpleNamespace())
    assert result[0] == 'failure' and 'not confirmed' in result[1]
    assert action.motion.move_head.call_count == 1


def test_scan_from_downward_navigation_pose_observes_at_normal_height_and_finishes_forward():
    n = SimpleNamespace(log=Mock(), set_success=lambda _r, text, data: ('success', data))
    n.log.sub.return_value = Mock()
    action = GetLookAroundObjectsAction(n)
    pose = {'pan': -1.57, 'tilt': -0.6}
    observations = []
    def move(**kwargs):
        pose.update({key:kwargs[key] for key in ('pan','tilt') if key in kwargs})
        return True
    def observe(_seconds):
        observations.append(dict(pose))
        action.scan_messages += 1
    action.motion = SimpleNamespace(move_head=move, sleep=observe)
    result = action.execute(None, SimpleNamespace())
    assert result[0] == 'success'
    assert observations and all(abs(p['tilt'] - (-0.2)) < 0.001 for p in observations)
    assert min(p['pan'] for p in observations) < 0 < max(p['pan'] for p in observations)
    assert pose == {'pan': 0.0, 'tilt': -0.2}


def test_stop_during_scan_does_not_move_head_again_to_finish_or_restore():
    n = SimpleNamespace(log=Mock(), set_stopped=lambda _r, text: ('stopped', text))
    n.log.sub.return_value = Mock()
    action = GetLookAroundObjectsAction(n)
    action.motion = Mock()
    def stop(**_kwargs):
        action.stop()
        return True
    action.motion.move_head.side_effect = stop
    assert action.execute(None, SimpleNamespace())[0] == 'stopped'
    assert action.motion.move_head.call_count == 1

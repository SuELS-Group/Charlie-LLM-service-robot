import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from geometry_msgs.msg import Pose
from command_node.command_node import CommandNode


def node():
    pose = Pose()
    pose.orientation.w = 1.
    n = SimpleNamespace(current_pose=pose, _pose_received=time.monotonic(), log=Mock(),
        operation_mode='study', _nav_wait_lock=threading.Lock(), _active_nav_action_goal=None,
        _base_lease_id='', _base_terminal_confirmed=True, _driver_stop_confirmed=True,
        _head_motion=SimpleNamespace(has_pending_motion=lambda: False), _lease_cleanup_pending=False,
        _navigation_deadline=None, _navigation_budget_expired=False,
        _lease_request_pending=False, _lease_request_uncertain=False, _lease_cleanup_retry_at=0.)
    n.get_position = lambda: CommandNode.get_position(n)
    return n


def test_stale_localization_prevents_coordination_and_base_start():
    n = node()
    n._pose_received -= 2.
    n._lease_service_call = Mock()
    lease, error = CommandNode._acquire_base_lease(n, SimpleNamespace(), 'move_to_waypoint', target='a', goal=[1., 2., 0.])
    assert lease is None and 'localization' in error
    n._lease_service_call.assert_not_called()


@pytest.mark.parametrize('stale', [False, True])
def test_watchdog_distinguishes_budget_expiry_and_stale_localization(stale):
    n = node()
    n._navigation_deadline = time.monotonic() + (30. if stale else -1.)
    if stale: n._pose_received -= 2.
    n.get_clock = lambda: SimpleNamespace(now=lambda: SimpleNamespace(to_msg=lambda: None))
    # Generated ROS Time is used even though no ROS node is constructed.
    from builtin_interfaces.msg import Time
    n.get_clock = lambda: SimpleNamespace(now=lambda: SimpleNamespace(to_msg=lambda: Time(sec=100)))
    n.global_stop_callback = Mock()
    CommandNode._navigation_watchdog(n)
    assert n._navigation_abort_reason == ('localization' if stale else 'budget')
    n.global_stop_callback.assert_called_once()


@pytest.mark.parametrize('terminal,ack,head_pending,should_release', [
    (True, True, False, True), (False, True, False, False), (True, False, False, False), (True, True, True, False)])
def test_late_cleanup_releases_only_confirmed_terminated_work(terminal, ack, head_pending, should_release):
    n = node()
    n._base_lease_id = 'owned'
    n._base_terminal_confirmed, n._driver_stop_confirmed = terminal, ack
    n._head_motion.has_pending_motion = lambda: head_pending
    n._release_base_lease = Mock(return_value=True)
    CommandNode._navigation_watchdog(n)
    assert bool(n._release_base_lease.call_count) is should_release
    assert bool(n._base_lease_id) is not should_release


def test_legacy_following_is_disabled_in_study_mode():
    n = node()
    assert CommandNode._set_follow_mode(n, 'person', 1.3)[0] is False


def test_late_base_reservation_is_released_without_starting_motion(monkeypatch):
    from rclpy.task import Future
    import command_node.command_node as module
    n = node()
    pending = Future()
    n.base_lease_client = SimpleNamespace(wait_for_service=lambda **_: True, call_async=lambda _: pending)
    n._late_lease_acquisition = lambda future: CommandNode._late_lease_acquisition(n, future)
    ticks = iter([0., 0., 3.])
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
    details, error = CommandNode._lease_service_call(n, 'acquire', {})
    assert details is None and 'not started' in error and n._lease_request_pending
    pending.set_result(SimpleNamespace(status=SimpleNamespace(status=1), result_data='{"lease_id":"late"}'))
    assert n._base_lease_id == 'late' and not n._lease_request_pending
    assert n._base_terminal_confirmed and not n._lease_request_uncertain
    n._release_base_lease = Mock(return_value=True)
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: 4.))
    CommandNode._navigation_watchdog(n)
    n._release_base_lease.assert_called_once_with('late')
    assert n._base_lease_id == ''


@pytest.mark.parametrize('status,data,uncertain', [(0, '{}', False), (1, '{}', True),
    (1, '[]', True)])
def test_late_coordination_clears_only_definitive_rejection_or_known_reservation(status, data, uncertain):
    from rclpy.task import Future
    n = node()
    n._lease_request_pending = True
    future = Future()
    future.set_result(SimpleNamespace(status=SimpleNamespace(status=status), result_data=data))
    CommandNode._late_lease_acquisition(n, future)
    assert not n._lease_request_pending and n._lease_request_uncertain is uncertain


@pytest.mark.parametrize('pending,uncertain', [(True, False), (False, True)])
def test_unresolved_coordination_blocks_new_navigation(pending, uncertain):
    from rclpy.action import GoalResponse
    n = node()
    n._last_stop_time = 0.
    n.use_nav2_action = True
    n.current_nav_goal_handle = None
    n._lease_request_pending, n._lease_request_uncertain = pending, uncertain
    assert CommandNode.navigation_goal_callback(n, SimpleNamespace(issued_at=100.)) == GoalResponse.REJECT


def test_failed_coordination_cleanup_has_a_retry_delay():
    n = node()
    n._base_lease_id = 'owned'
    n._release_base_lease = Mock(return_value=False)
    CommandNode._navigation_watchdog(n)
    CommandNode._navigation_watchdog(n)
    n._release_base_lease.assert_called_once()
    assert n._base_lease_id == 'owned'

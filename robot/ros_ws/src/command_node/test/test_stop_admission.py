import threading
import json
import asyncio
import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from rclpy.action import GoalResponse
from command_node.command_node import CommandNode


def test_navigation_rejects_pre_stop_goals_but_accepts_fresh_ones():
    node = SimpleNamespace(_last_stop_time=100, use_nav2_action=True, log=Mock(),
        _driver_stop_confirmed=True, _head_motion=SimpleNamespace(has_pending_motion=lambda: False),
        _nav_wait_lock=threading.Lock(), _active_nav_action_goal=None, current_nav_goal_handle=None, _base_lease_id='', _lease_request_pending=False, _lease_request_uncertain=False)
    assert CommandNode.navigation_goal_callback(node, SimpleNamespace(issued_at=90)) == GoalResponse.REJECT
    assert CommandNode.navigation_goal_callback(node, SimpleNamespace(issued_at=110)) == GoalResponse.ACCEPT


def node():
    n = SimpleNamespace(_last_stop_time=100, use_nav2_action=True, log=Mock(),
        _nav_wait_lock=threading.Lock(), _active_nav_action_goal=None, current_nav_goal_handle=None,
        _active_nav2_action_goal=None, _driver_stop_confirmed=True, _driver_stop_generation=0,
        _nav_goal_generation=0,
        _head_stop_evt=threading.Event(), _head_motion=Mock(), latest_global_stop=None,
        follow_mode_active=False, current_nav_action_name=None, llm_info_publisher_=Mock(),
        describe_global_stop=lambda _: '', format_global_stop_message=lambda text: text)
    n._lease_request_pending = False
    n._lease_request_uncertain = False
    n._base_lease_id = ''
    n._acquire_base_lease = Mock(return_value=('lease', None))
    n._release_base_lease = Mock(return_value=True)
    n._head_motion.has_pending_motion.return_value = False
    n._driver_stop_result = lambda future, generation: CommandNode._driver_stop_result(n, future, generation)
    return n


@pytest.mark.parametrize('ready', [False, True])
def test_driver_stop_failure_does_not_skip_navigation_or_head_cancellation(ready):
    n = node()
    n.driver_stop_client = SimpleNamespace(service_is_ready=lambda: ready,
        call_async=Mock(side_effect=RuntimeError('offline driver fault')))
    n.follow_mode_active = True
    n._active_nav_action_goal = Mock()
    n._active_nav2_action_goal = Mock()
    CommandNode.global_stop_callback(n, SimpleNamespace(stamp=SimpleNamespace(sec=101, nanosec=0), reason='test'))
    assert n._nav_action_stop_requested and not n.follow_mode_active and n._head_stop_evt.is_set()
    n._active_nav2_action_goal.cancel_goal_async.assert_called_once()
    n._head_motion.cancel_active_trajectories.assert_called_once()
    assert not n._driver_stop_confirmed


def test_navigation_waits_for_latest_driver_ack_and_head_termination():
    from rclpy.task import Future
    n = node()
    n._driver_stop_confirmed = False
    n._driver_stop_generation = 2
    request = SimpleNamespace(issued_at=110)
    assert CommandNode.navigation_goal_callback(n, request) == GoalResponse.REJECT
    future = Future()
    future.set_result(SimpleNamespace(success=True))
    CommandNode._driver_stop_result(n, future, 1)
    assert not n._driver_stop_confirmed
    CommandNode._driver_stop_result(n, future, 2)
    assert CommandNode.navigation_goal_callback(n, request) == GoalResponse.ACCEPT
    n._head_motion.has_pending_motion.return_value = True
    assert CommandNode.navigation_goal_callback(n, request) == GoalResponse.REJECT


def test_failed_head_motion_cannot_claim_navigation_head_pose():
    n = node()
    n.nav_head_pan, n.nav_head_tilt = 0, -.6
    n._head_motion.move_head.return_value = False
    assert not CommandNode._move_head_for_navigation(n)
    assert not n.head_in_nav_position


def test_failed_head_restoration_keeps_navigation_pose_flag():
    n = node()
    n.head_in_nav_position = True
    n.default_head_pan, n.default_head_tilt = 0, -.2
    n._head_motion.move_head.return_value = False
    assert not CommandNode._reset_head_after_navigation(n)
    assert n.head_in_nav_position


def test_stop_during_head_server_check_prevents_dispatch():
    from robot_common.motion import MotionHelper
    n = node()
    n.nav_head_pan, n.nav_head_tilt = 0, -.6
    def ready(**_kwargs):
        n._head_stop_evt.set()
        return True
    n._traj_client = SimpleNamespace(wait_for_server=ready, send_goal_async=Mock())
    n._head_motion = MotionHelper(n, stop_evt=n._head_stop_evt)
    assert not CommandNode._move_head_for_navigation(n)
    n._traj_client.send_goal_async.assert_not_called()


def test_action_result_exposes_head_uncertainty_with_real_interface():
    from command_interfaces.action import TurnTo
    n = node()
    n._head_motion.has_pending_motion.return_value = True
    result = CommandNode._set_action_result_status(n, TurnTo.Result(), 3, 'stopped')
    assert json.loads(result.result_data)['execution_uncertain']


def test_command_node_initializes_without_starting_ros(monkeypatch):
    import command_node.command_node as module
    monkeypatch.setattr(module.Node, '__init__', lambda *_a, **_k: None)
    for name in ['create_publisher', 'create_subscription', 'create_service', 'create_client', 'create_timer']:
        monkeypatch.setattr(CommandNode, name, Mock())
    for name in ['ActionClient', 'ActionServer', 'get_logger']:
        monkeypatch.setattr(module, name, Mock())
    monkeypatch.setattr(module.tf2_ros, 'Buffer', Mock())
    monkeypatch.setattr(module.tf2_ros, 'TransformListener', Mock())
    n = CommandNode()
    assert not n._head_stop_evt.is_set() and not n._head_motion.has_pending_motion()


def test_late_legacy_goal_acceptance_after_stop_is_cancelled():
    from rclpy.task import Future
    n = node()
    n._nav_goal_generation = 2
    handle = SimpleNamespace(accepted=True, cancel_goal_async=Mock())
    future = Future()
    future.set_result(handle)
    CommandNode._nav_goal_response_callback(n, future, 1)
    handle.cancel_goal_async.assert_called_once()


def test_unsafe_transit_posture_reports_the_cause_without_starting_head_or_base():
    from command_interfaces.action import MoveTo
    from tool_status.msg import ToolStatus
    n = node()
    n.navigation_map_ready = True
    reason = 'Transit posture not confirmed: joint_lift=0.97 outside [0.2,0.9]'
    n._acquire_base_lease.return_value = (None, reason)
    n._move_head_for_navigation = Mock()
    n.nav_to_pose_client = Mock()
    goal = SimpleNamespace(request=SimpleNamespace(issued_at=110), abort=Mock())
    result = asyncio.run(CommandNode._execute_navigation_action(n, goal, action_name='move_to',
        target_name='', goal_x=1, goal_y=2, goal_orientation=0, setup_success=True, setup_message='',
        feedback_type=None, result_builder=lambda status, message: CommandNode._set_action_result_status(
            n, MoveTo.Result(), status, message)))
    data = json.loads(result.result_data)
    assert result.status.status == ToolStatus.STATUS_FAILURE and result.status.message == reason
    assert data['phase'] == 'transit_posture' and data['base_started'] is False
    assert data['posture_error'] == reason and data['completion_confirmed']
    assert not data['execution_uncertain']
    n._move_head_for_navigation.assert_not_called()
    n.nav_to_pose_client.send_goal_async.assert_not_called()


@pytest.mark.parametrize('prepare_ok,restore_ok,base_called', [(False, True, False), (True, False, True)])
@pytest.mark.parametrize('controller_error', [None, {'code': 'RUNSTOP_ACTIVE',
    'message': 'Cannot execute goals while in mode=runstopped', 'controller_error_code': -1, 'action_status': 6}])
def test_native_navigation_checks_head_before_base_and_preserves_partial_arrival(prepare_ok, restore_ok, base_called, controller_error):
    from rclpy.task import Future
    from geometry_msgs.msg import PoseStamped
    from command_interfaces.action import MoveTo
    from tool_status.msg import ToolStatus
    def completed(value):
        future = Future()
        future.set_result(value)
        return future
    n = node()
    n._head_motion.last_trajectory_error = controller_error
    n.navigation_map_ready = True
    n._move_head_for_navigation = Mock(return_value=prepare_ok)
    n._reset_head_after_navigation = Mock(return_value=restore_ok)
    n._await_controller_cleanup = Mock()
    n._publish_action_started = Mock()
    n._publish_action_completion = Mock()
    n._publish_navigation_feedback = Mock()
    n._build_goal_pose = lambda *_args: PoseStamped()
    n._map_nav2_result = lambda *_a, **_k: (ToolStatus.STATUS_SUCCESS, 'arrived')
    nav_handle = SimpleNamespace(accepted=True, get_result_async=lambda: completed(SimpleNamespace(status=4)))
    n.nav_to_pose_client = SimpleNamespace(wait_for_server=lambda **_k: True,
        send_goal_async=Mock(return_value=completed(nav_handle)))
    goal = SimpleNamespace(request=SimpleNamespace(issued_at=110), is_cancel_requested=False,
                           abort=Mock(), succeed=Mock())
    result = asyncio.run(CommandNode._execute_navigation_action(n, goal, action_name='move_to',
        target_name='', goal_x=1, goal_y=2, goal_orientation=0, setup_success=True, setup_message='',
        feedback_type=None, result_builder=lambda status, message: CommandNode._set_action_result_status(
            n, MoveTo.Result(), status, message)))
    assert result.status.status == ToolStatus.STATUS_FAILURE
    assert bool(n.nav_to_pose_client.send_goal_async.call_count) is base_called
    goal.succeed.assert_not_called()
    assert json.loads(result.result_data).get('base_reached', False) is base_called
    if not prepare_ok:
        data = json.loads(result.result_data)
        assert data['phase'] == 'head_preparation' and data['base_started'] is False
        assert data['controller_error'] == (controller_error or {})
        if controller_error:
            assert controller_error['message'] in result.status.message
    assert n._active_nav_action_goal is None


@pytest.mark.parametrize('outcome', ['aborted', 'rejected', 'unavailable', 'cancelled',
    'unknown', 'global_stop', 'cancel_requested', 'head_pending', 'driver_unconfirmed'])
def test_finished_failed_navigation_restores_head_but_stop_and_unknown_motion_do_not(outcome):
    from rclpy.task import Future
    from geometry_msgs.msg import PoseStamped
    from command_interfaces.action import MoveTo
    from action_msgs.msg import GoalStatus
    from tool_status.msg import ToolStatus
    from types import MethodType
    def completed(value):
        future = Future(); future.set_result(value); return future
    n = node()
    n.navigation_map_ready = True
    n.head_in_nav_position = False
    n.nav_head_pan, n.nav_head_tilt = 0., -.6
    n.default_head_pan, n.default_head_tilt = 0., -.2
    n._head_motion.move_head.return_value = True
    n._move_head_for_navigation = MethodType(CommandNode._move_head_for_navigation, n)
    n._reset_head_after_navigation = MethodType(CommandNode._reset_head_after_navigation, n)
    n._restore_head_after_navigation_failure = MethodType(CommandNode._restore_head_after_navigation_failure, n)
    n._map_nav2_result = MethodType(CommandNode._map_nav2_result, n)
    n._await_controller_cleanup = Mock()
    n._publish_action_started = Mock()
    n._publish_action_completion = Mock()
    n._publish_navigation_feedback = Mock()
    n._build_goal_pose = lambda *_args: PoseStamped()
    goal = SimpleNamespace(request=SimpleNamespace(issued_at=110), is_cancel_requested=False,
        abort=Mock(), succeed=Mock(), canceled=Mock())
    def finished():
        if outcome == 'global_stop':
            n.latest_global_stop = object(); n._head_stop_evt.set(); n._nav_action_stop_requested = True
        if outcome == 'cancel_requested': goal.is_cancel_requested = True
        if outcome == 'head_pending': n._head_motion.has_pending_motion.return_value = True
        if outcome == 'driver_unconfirmed': n._driver_stop_confirmed = False
        status = {'cancelled': GoalStatus.STATUS_CANCELED, 'unknown': GoalStatus.STATUS_UNKNOWN}.get(
            outcome, GoalStatus.STATUS_ABORTED)
        return completed(SimpleNamespace(status=status))
    handle = SimpleNamespace(accepted=outcome != 'rejected', get_result_async=finished)
    n.nav_to_pose_client = SimpleNamespace(wait_for_server=lambda **_: outcome != 'unavailable',
        send_goal_async=Mock(return_value=completed(handle)))
    result = asyncio.run(CommandNode._execute_navigation_action(n, goal, action_name='position_for_action',
        target_name='trash_can', goal_x=1, goal_y=2, goal_orientation=0, setup_success=True, setup_message='',
        feedback_type=None, result_builder=lambda status, message: CommandNode._set_action_result_status(
            n, MoveTo.Result(), status, message)))
    should_restore = outcome in {'aborted', 'rejected', 'unavailable'}
    tilts = [call.args[1] for call in n._head_motion.move_head.call_args_list]
    assert tilts == ([-.6, -.2] if should_restore else [-.6])
    assert n.head_in_nav_position is not should_restore
    assert result.status.status != ToolStatus.STATUS_SUCCESS
    assert not json.loads(result.result_data).get('base_reached', False)
    goal.succeed.assert_not_called()

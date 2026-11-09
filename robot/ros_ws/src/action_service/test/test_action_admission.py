import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from action_interfaces.srv import ExecuteAction
from action_service.action_service_node import ActionServiceNode
from tool_status.msg import ToolStatus


def node():
    motion = SimpleNamespace(has_pending_motion=lambda: False)
    n = SimpleNamespace(log=Mock(), _action_lock=threading.Lock(), _last_stop_time=100,
        _action_stop_evt=threading.Event(), _motion_helper=motion, _stoppable_actions=[],
        _driver_stop_confirmed=True, _driver_stop_pending=False, _driver_stop_generation=0,
        _stop_ack_lock=threading.Lock())
    from robot_common.k2_runtime import K2Runtime
    from robot_common.driver_state import DriverState
    n.driver_state = DriverState()
    from robot_common.object_state import ObjectState
    from robot_common.study_policy import StudyPolicy
    n.study_policy = StudyPolicy({}, mode='development')
    n.object_state = ObjectState()
    n.object_state.set('empty', 'test_setup')
    from robot_common.config import load
    n.k2_runtime = K2Runtime(load(Path(__file__).parents[1] / 'config/k2_runtime.yaml'))
    n.get_clock = lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=110_000_000_000))
    n._deadline_lock = threading.Lock()
    n._request_generation = ''
    n._budget_expired = False
    n._expire_action_budget = Mock()
    n._finish_action_budget = lambda generation, deadline: ActionServiceNode._finish_action_budget(n, generation, deadline)
    n._has_unfinished_work = lambda **kwargs: ActionServiceNode._has_unfinished_work(n, **kwargs)
    n._await_worker_cleanup = lambda: ActionServiceNode._await_worker_cleanup(n)
    n._await_driver_stop_ack = lambda: ActionServiceNode._await_driver_stop_ack(n)
    n.set_failure = lambda response, message, data='': ActionServiceNode.set_failure(n, response, message, data)
    n.set_success = lambda response, message, data='': ActionServiceNode.set_success(n, response, message, data)
    n.set_timeout = lambda response, message, data='': ActionServiceNode.set_timeout(n, response, message, data)
    n.set_stopped = lambda response, message, data='': ActionServiceNode.set_stopped(n, response, message, data)
    n.set_status = lambda response, status, message, data='': ActionServiceNode.set_status(response, status, message, data)
    n.format_global_stop_message = lambda fallback: fallback
    n._action_handlers = {'move_head': Mock(return_value=ExecuteAction.Response())}
    return n


def request(issued_at):
    return SimpleNamespace(action_type='move_head', parameters=json.dumps({'_issued_at': issued_at}))


def test_stale_action_does_not_clear_stop_or_execute():
    n = node()
    result = ActionServiceNode.handle_action(n, request(90), ExecuteAction.Response())
    assert result.status.status == ToolStatus.STATUS_STOPPED
    n._action_handlers['move_head'].assert_not_called()


def test_overlapping_workflow_is_rejected():
    n = node()
    n._action_lock.acquire()
    try:
        result = ActionServiceNode.handle_action(n, request(110), ExecuteAction.Response())
    finally:
        n._action_lock.release()
    assert result.status.status == ToolStatus.STATUS_FAILURE
    n._action_handlers['move_head'].assert_not_called()


def test_unterminated_controller_work_remains_blocked():
    n = node()
    n._motion_helper.has_pending_motion = lambda: True
    result = ActionServiceNode.handle_action(n, request(110), ExecuteAction.Response())
    assert json.loads(result.result_data)['execution_uncertain']
    n._action_handlers['move_head'].assert_not_called()


def test_fresh_action_can_begin_after_termination():
    n = node()
    ActionServiceNode.handle_action(n, request(110), ExecuteAction.Response())
    n._action_handlers['move_head'].assert_called_once()
    assert not n._action_lock.locked()


def test_stop_during_object_admission_is_not_erased_before_dispatch():
    n = node()
    def stop_during_check(_action):
        n._last_stop_time = 120
        n._action_stop_evt.set()
        return None
    n.object_state.admission_error = stop_during_check
    result = ActionServiceNode.handle_action(n, request(110), ExecuteAction.Response())
    assert result.status.status == ToolStatus.STATUS_STOPPED
    n._action_handlers['move_head'].assert_not_called()
    assert n._action_stop_evt.is_set()


def test_global_stop_is_honored_during_startup():
    n = node()
    n._startup_time = 1e20
    n.describe_global_stop = lambda _: ''
    n.stop_current_action = Mock()
    message = SimpleNamespace(stamp=SimpleNamespace(sec=101, nanosec=0))
    ActionServiceNode._global_stop_callback(n, message)
    assert n._action_stop_evt.is_set()
    n.stop_current_action.assert_called_once()


def test_cleanup_failure_cannot_leave_handoff_success():
    n = node()
    n.take_from_hand_action = SimpleNamespace(_arm_motion_started=True, _arm_retracted=False, _object_secured=True, _orientation_restored=True, _navigation_mode_restored=True)
    response = ExecuteAction.Response()
    response.status.status = ToolStatus.STATUS_SUCCESS
    n._action_handlers['take_from_hand'] = Mock(return_value=response)
    request = SimpleNamespace(action_type='take_from_hand', parameters='{"_issued_at": 110}')
    result = ActionServiceNode.handle_action(n, request, response)
    assert result.status.status == ToolStatus.STATUS_FAILURE
    assert json.loads(result.result_data)['travel_ready'] is False


def test_worker_alive_after_join_blocks_new_work_even_if_active_flag_was_cleared():
    n = node()
    worker = SimpleNamespace(is_alive=lambda: True, join=Mock())
    n._stoppable_actions = [('dock', SimpleNamespace(dock_active=False, _op_thread=worker), 'dock_active')]
    result = ActionServiceNode.handle_action(n, request(110), ExecuteAction.Response())
    assert result.status.status == ToolStatus.STATUS_FAILURE
    n._action_handlers['move_head'].assert_not_called()


def test_older_stop_ack_cannot_release_a_newer_stop():
    from rclpy.task import Future
    n = node()
    n._driver_stop_generation = 2
    n._driver_stop_confirmed = False
    n._driver_stop_pending = True
    future = Future()
    future.set_result(SimpleNamespace(success=True))
    ActionServiceNode._driver_stop_result(n, future, 1)
    assert not n._driver_stop_confirmed and n._driver_stop_pending
    ActionServiceNode._driver_stop_result(n, future, 2)
    assert n._driver_stop_confirmed and not n._driver_stop_pending


def test_delayed_budget_callback_is_enforced_before_completion_is_reported(monkeypatch):
    import action_service.action_service_node as module
    clock = {'now': 0.}
    monkeypatch.setattr(module, 'time', SimpleNamespace(
        time=lambda: 110., monotonic=lambda: clock['now'], sleep=Mock()))
    monkeypatch.setattr(module.threading, 'Timer', Mock())
    n = node()
    def halt():
        n._driver_stop_confirmed = False  # Halt has not been acknowledged.
    n.stop_current_action = Mock(side_effect=halt)
    def handler(_request, response):
        clock['now'] = 3.
        return n.set_success(response, 'finished')
    n._action_handlers['move_head'] = Mock(side_effect=handler)
    req = SimpleNamespace(action_type='move_head', parameters='{"_issued_at":110,"_budget_s":2}')
    result = ActionServiceNode.handle_action(n, req, ExecuteAction.Response())
    assert result.status.status == ToolStatus.STATUS_TIMEOUT
    details = json.loads(result.result_data)
    assert details['execution_uncertain'] and not details['completion_confirmed']
    assert 'stop requested' in result.status.message
    n.stop_current_action.assert_called_once()
    # A fresh request cannot erase the missing physical halt acknowledgement.
    blocked = ActionServiceNode.handle_action(n, req, ExecuteAction.Response())
    assert blocked.status.status == ToolStatus.STATUS_FAILURE
    n._action_handlers['move_head'].assert_called_once()

from mcp_server import ros_utils
from types import SimpleNamespace


class CompletedFuture:
    def __init__(self, value=None):
        self.value = value

    def done(self):
        return True

    def result(self):
        return self.value


class PendingFuture:

    def done(self):
        return False


class GoalHandle:

    def __init__(self, cancel_future=None):
        self.cancel_future = cancel_future or CompletedFuture(SimpleNamespace(return_code=0, goals_canceling=[object()]))
        self.cancel_requests = 0

    def cancel_goal_async(self):
        self.cancel_requests += 1
        return self.cancel_future


def test_cancel_action_goal_confirms_acknowledgement_and_terminal_result():
    goal_handle = GoalHandle()
    confirmed = ros_utils._cancel_action_goal(goal_handle, '/command_node/turn_to', CompletedFuture(SimpleNamespace(status=5)))
    assert confirmed is True
    assert goal_handle.cancel_requests == 1


def test_denied_cancellation_is_not_confirmed_even_if_action_succeeded():
    goal = GoalHandle(CompletedFuture(SimpleNamespace(return_code=1, goals_canceling=[])))
    terminal = CompletedFuture(SimpleNamespace(status=4, result='done'))
    assert not ros_utils._cancel_action_goal(goal, '/test', terminal)
    completion = ros_utils._action_completion(terminal)
    assert completion['completion_confirmed']
    assert completion['result'] == 'done'


def test_cancel_ack_without_a_cancelled_result_is_not_confirmation():
    assert not ros_utils._cancel_action_goal(GoalHandle(), '/test', CompletedFuture(SimpleNamespace(status=4)))


def test_late_accepted_goal_is_cancelled():
    goal = GoalHandle()
    goal.accepted = True
    ros_utils._cancel_late_goal(CompletedFuture(goal))
    assert goal.cancel_requests == 1


def test_acceptance_has_a_short_deadline_and_late_goal_callback(monkeypatch):
    from rclpy.task import Future
    from unittest.mock import Mock
    future = Future()
    client = SimpleNamespace(wait_for_server=lambda **_: True, send_goal_async=lambda _: future)
    monkeypatch.setattr(ros_utils, 'get_node', lambda: SimpleNamespace())
    monkeypatch.setattr('rclpy.action.ActionClient', lambda *_: client)
    monkeypatch.setattr(ros_utils, 'time', SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 0, 6]), sleep=lambda _: None))
    result = ros_utils.call_action('/test', object, object(), timeout_sec=300)
    assert result['timed_out'] and result['execution_uncertain']
    goal = GoalHandle()
    goal.accepted = True
    future.set_result(goal)
    assert goal.cancel_requests == 1


def test_cancel_action_goal_reports_when_result_does_not_terminate(monkeypatch):
    monkeypatch.setattr(ros_utils, 'ACTION_CANCEL_WAIT_SECONDS', 0.0)
    goal_handle = GoalHandle()
    confirmed = ros_utils._cancel_action_goal(goal_handle, '/command_node/turn_to', PendingFuture())
    assert confirmed is False
    assert goal_handle.cancel_requests == 1

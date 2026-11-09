from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from workstation_common import get_logger
from mcp_server.call_state import record_call_fault, unresolved_call, request_issue_time, request_language, remaining_budget, request_tool

from tool_status.msg import ToolStatus as RosToolStatus

log = get_logger('mcp.ros')
_node: Any | None = None
_executor: Any | None = None
_executor_thread: threading.Thread | None = None
STATUS_SUCCESS = RosToolStatus.STATUS_SUCCESS
STATUS_FAILURE = RosToolStatus.STATUS_FAILURE
STATUS_STOPPED = RosToolStatus.STATUS_STOPPED
STATUS_TIMEOUT = RosToolStatus.STATUS_TIMEOUT
ACTION_CANCEL_WAIT_SECONDS = 2.0
ACTION_ACCEPT_WAIT_SECONDS = 5.0


@dataclass
class RosCallFailure:
    status: Any
    result_data: str = ''


def _service_failure(service_name, *, timed_out=False, sent=False, message=''):
    # A lost state-ledger response cannot leave a motor moving. Keep the RPC
    # failure visible without creating a physical-recovery latch for metadata.
    motion_uncertain = sent and service_name != '/k2/object_state'
    details = {'code': 'SERVICE_TIMEOUT' if timed_out else 'SERVICE_ERROR',
               'message': message or f'Could not complete {service_name}',
               'retryable': False, 'execution_uncertain': motion_uncertain}
    record_call_fault(details)
    return RosCallFailure(SimpleNamespace(
        status=STATUS_TIMEOUT if timed_out else STATUS_FAILURE,
        message=details['message']), result_data=json.dumps({
            'execution_started': sent, 'execution_uncertain': motion_uncertain,
            'completion_confirmed': not sent}))


def get_node():
    global _node
    if _node is None:
        import rclpy
        from rclpy.node import Node

        if not rclpy.ok():
            rclpy.init()
        _node = Node('mcp_ros_utils')
        log.info('Created shared MCP ROS helper node')
        _ensure_executor()
    return _node


def _ensure_executor() -> None:
    global _executor, _executor_thread
    node = _node
    if node is None:
        return
    if _executor_thread is not None and _executor_thread.is_alive():
        return
    from rclpy.executors import MultiThreadedExecutor

    _executor = MultiThreadedExecutor(num_threads=4)
    _executor.add_node(node)
    _executor_thread = threading.Thread(target=_spin_executor, name='mcp-ros-executor', daemon=True)
    _executor_thread.start()
    log.info('Started shared MCP ROS executor thread')


def _spin_executor() -> None:
    try:
        if _executor is not None:
            _executor.spin()
    except Exception as exc:
        log.exception(f'MCP ROS executor stopped unexpectedly: {exc}')


def response_status(response: Any) -> int | None:
    if response is None:
        return None
    status = getattr(response, 'status', None)
    value = getattr(status, 'status', None)
    return int(value) if isinstance(value, int) else None


def response_message(response: Any, default: str = '') -> str:
    if response is None:
        return default
    status = getattr(response, 'status', None)
    message = getattr(status, 'message', '')
    text = str(message or '').strip()
    return text or default


def action_status(result: Any) -> int | None:
    if result is None:
        return None
    status = getattr(result, 'status', None)
    value = getattr(status, 'status', None)
    return int(value) if isinstance(value, int) else None


def action_message(result: Any, default: str = '') -> str:
    if result is None:
        return default
    status = getattr(result, 'status', None)
    message = getattr(status, 'message', '')
    text = str(message or '').strip()
    return text or default


def publish_string(topic: str, payload: dict[str, Any]) -> None:
    from std_msgs.msg import String

    node = get_node()
    key = f'_pub_{topic.replace("/", "_")}'
    if not hasattr(node, key):
        setattr(node, key, node.create_publisher(String, topic, 10))
    publisher = getattr(node, key)
    publisher.publish(String(data=json.dumps(payload)))
    log.info(f'Published message to {topic}')
    time.sleep(0.05)


def call_service(service_name: str, srv_type, request, timeout_sec: float = 60.0):
    if unresolved_call():
        return _service_failure(service_name, sent=True, message='Previous operation has not been confirmed')
    timeout_sec = remaining_budget(timeout_sec)
    if timeout_sec <= 0:
        return _service_failure(service_name, timed_out=True, sent=False, message='Operation budget exhausted before dispatch')
    deadline = time.monotonic() + timeout_sec
    if service_name == '/execute_action':
        try:
            params = json.loads(request.parameters) if request.parameters else {}
            params['_issued_at'] = request_issue_time()
            params['_language'] = request_language()
            params['_request_tool'] = request_tool()
            params['_budget_s'] = timeout_sec
            request.parameters = json.dumps(params)
        except (ValueError, TypeError, AttributeError):
            return _service_failure(service_name, message='Invalid action parameters')
    node = get_node()
    key = f'_svc_{service_name.replace("/", "_")}'
    if not hasattr(node, key):
        setattr(node, key, node.create_client(srv_type, service_name))
    client = getattr(node, key)
    if not client.wait_for_service(timeout_sec=min(2.0, timeout_sec)):
        log.warning(f'Service {service_name} was not available')
        return None
    dispatch_time = time.monotonic()
    if dispatch_time >= deadline:
        return _service_failure(service_name, timed_out=True, sent=False, message='Operation budget exhausted before dispatch')
    if service_name == '/execute_action':
        params['_budget_s'] = deadline - dispatch_time
        request.parameters = json.dumps(params)
    log.info(f'Calling service {service_name}')
    try:
        future = client.call_async(request)
    except Exception as exc:
        return _service_failure(service_name, sent=True, message=str(exc))
    while not future.done():
        if time.monotonic() >= deadline:
            future.cancel()
            log.warning(f'Service call to {service_name} timed out after {timeout_sec:.1f}s')
            return _service_failure(service_name, timed_out=True, sent=True,
                message=f'Timeout waiting for {service_name}; server completion is unknown')
        time.sleep(0.05)
    try:
        result = future.result()
    except Exception as exc:
        return _service_failure(service_name, sent=True, message=str(exc))
    try:
        data = json.loads(getattr(result, 'result_data', '') or '{}')
        if isinstance(data, dict) and data.get('execution_uncertain') is True:
            record_call_fault({'code': 'ROBOT_STATE_UNCERTAIN',
                'message': response_message(result, 'Robot state could not be confirmed'),
                'retryable': False, 'execution_uncertain': True})
    except (TypeError, ValueError):
        pass
    return result


def call_action(action_name: str, action_type, goal, timeout_sec: float = 60.0) -> dict[str, Any]:
    if unresolved_call():
        return {'accepted': False, 'timed_out': False, 'result': None,
                'message': 'Previous operation has not been confirmed', 'execution_uncertain': True}
    timeout_sec = remaining_budget(timeout_sec)
    if timeout_sec <= 0:
        return {'accepted': False, 'timed_out': True, 'result': None,
            'message': 'Operation budget exhausted before dispatch', 'completion_confirmed': True,
            'execution_uncertain': False, 'execution_started': False}
    from rclpy.action import ActionClient
    deadline = time.monotonic() + timeout_sec
    if hasattr(goal, 'issued_at'):
        goal.issued_at = float(request_issue_time())
    if hasattr(goal, 'request_tool'):
        goal.request_tool = request_tool()

    node = get_node()
    key = f'_action_{action_name.replace("/", "_")}'
    if not hasattr(node, key):
        setattr(node, key, ActionClient(node, action_type, action_name))
    client = getattr(node, key)
    if not client.wait_for_server(timeout_sec=min(2.0, timeout_sec)):
        log.warning(f'Action {action_name} was not available')
        return {'accepted': False, 'timed_out': False, 'result': None, 'message': f'Action {action_name} not available'}
    if time.monotonic() >= deadline:
        return {'accepted': False, 'timed_out': True, 'result': None,
            'message': 'Operation budget exhausted before dispatch', 'completion_confirmed': True,
            'execution_uncertain': False, 'execution_started': False}
    log.info(f'Calling action {action_name}')
    try:
        send_goal_future = client.send_goal_async(goal)
    except Exception as exc:
        return _action_fault(str(exc))
    acceptance_deadline = min(deadline, time.monotonic() + ACTION_ACCEPT_WAIT_SECONDS)
    while not send_goal_future.done():
        if time.monotonic() >= acceptance_deadline:
            # Retain the future so a goal accepted late can still be cancelled.
            send_goal_future.add_done_callback(_cancel_late_goal)
            log.warning(f'Action goal to {action_name} timed out after {timeout_sec:.1f}s before acceptance')
            record_call_fault({'code': 'ACTION_TIMEOUT', 'message': 'Timeout waiting for goal acceptance',
                               'retryable': False, 'execution_uncertain': True})
            return {
                'accepted': False,
                'timed_out': True,
                'result': None,
                'message': 'Timeout waiting for goal acceptance', 'execution_uncertain': True,
                'completion_confirmed': False}
        time.sleep(0.05)
    try:
        goal_handle = send_goal_future.result()
    except Exception as exc:
        return _action_fault(str(exc))
    if not goal_handle.accepted:
        log.warning(f'Action goal was rejected by {action_name}')
        return {'accepted': False, 'timed_out': False, 'result': None, 'message': 'Action goal was rejected'}
    try:
        result_future = goal_handle.get_result_async()
    except Exception as exc:
        return _action_fault(str(exc), accepted=True)
    while not result_future.done():
        if time.monotonic() >= deadline:
            cancellation_confirmed = _cancel_action_goal(goal_handle, action_name, result_future)
            log.warning(f'Action result wait for {action_name} timed out after {timeout_sec:.1f}s')
            message = 'Timeout waiting for action result'
            if not cancellation_confirmed:
                message += '; cancellation was requested but not confirmed'
            completion = _action_completion(result_future)
            if not completion['completion_confirmed']:
                record_call_fault({'code': 'ACTION_TIMEOUT', 'message': message,
                    'retryable': False, 'execution_uncertain': True})
            return {'accepted': True, 'timed_out': True, 'message': message,
                    'cancellation_confirmed': cancellation_confirmed, **completion}
        time.sleep(0.05)
    try:
        completion = _action_completion(result_future)
        result = completion['result']
        if completion['execution_uncertain']:
            record_call_fault({'code': 'ACTION_ERROR', 'message': 'Action terminal state could not be confirmed',
                               'retryable': False, 'execution_uncertain': True})
    except Exception as exc:
        return {'accepted': True, 'timed_out': False, 'result': None, 'message': str(exc), 'execution_uncertain': True}
    return {'accepted': True, 'timed_out': False, 'message': action_message(result), **completion}


def _action_fault(message, accepted=False):
    record_call_fault({'code': 'ACTION_ERROR', 'message': message,
                       'retryable': False, 'execution_uncertain': True})
    return {'accepted': accepted, 'timed_out': False, 'result': None, 'message': message,
            'execution_uncertain': True, 'completion_confirmed': False}


def _action_completion(future):
    from action_msgs.msg import GoalStatus
    try:
        wrapper = future.result() if future.done() else None
        status = getattr(wrapper, 'status', None)
        confirmed = status in {GoalStatus.STATUS_SUCCEEDED, GoalStatus.STATUS_CANCELED, GoalStatus.STATUS_ABORTED}
        result = getattr(wrapper, 'result', None)
        details = json.loads(getattr(result, 'result_data', '') or '{}')
        if (not isinstance(details, dict) or
                (hasattr(result, 'result_data') and details.get('completion_confirmed') is not True) or
                details.get('execution_uncertain') is True):
            confirmed = False
        return {'completion_confirmed': confirmed, 'execution_uncertain': not confirmed,
                'terminal_status': status, 'result': result}
    except Exception:
        return {'completion_confirmed': False, 'execution_uncertain': True, 'result': None}


def _cancel_late_goal(future):
    try:
        handle = future.result()
        if handle is not None and handle.accepted:
            handle.cancel_goal_async()
    except Exception as exc:
        log.warning(f'Could not cancel late-accepted goal: {exc}')


def _cancel_action_goal(goal_handle, action_name: str, result_future=None) -> bool:
    """Confirm both acceptance of cancellation and a cancelled terminal result."""
    from action_msgs.msg import GoalStatus
    try:
        cancel_future = goal_handle.cancel_goal_async()
    except Exception as exc:
        log.warning(f'Could not request cancellation for {action_name}: {exc}')
        return False
    deadline = time.monotonic() + ACTION_CANCEL_WAIT_SECONDS
    while not cancel_future.done() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not cancel_future.done():
        log.warning(f'Cancellation acknowledgement for {action_name} did not arrive '
                    f'within {ACTION_CANCEL_WAIT_SECONDS:.1f}s')
        return False
    try:
        acknowledgement = cancel_future.result()
        accepted = (acknowledgement is not None and acknowledgement.return_code == 0 and
                    bool(acknowledgement.goals_canceling))
        if not accepted:
            return False
    except Exception as exc:
        log.warning(f'Cancellation request for {action_name} failed: {exc}')
        return False
    if result_future is not None:
        result_deadline = time.monotonic() + ACTION_CANCEL_WAIT_SECONDS
        while (not result_future.done() and time.monotonic() < result_deadline):
            time.sleep(0.05)
        if not result_future.done():
            log.warning(f'Timed-out action {action_name} acknowledged cancellation '
                        'but did not reach a terminal state')
            return False
        completion = _action_completion(result_future)
        if not completion['completion_confirmed'] or completion.get('terminal_status') != GoalStatus.STATUS_CANCELED:
            return False
    else:
        return False
    log.info(f'Cancellation and termination confirmed for timed-out action '
             f'{action_name}')
    return True


def call_trigger_service(service_name: str, timeout_sec: float = 30.0) -> dict[str, Any]:
    from std_srvs.srv import Trigger

    if unresolved_call() and service_name != '/stop_the_robot':
        return {'success': False, 'message': 'Previous operation has not been confirmed',
                'execution_uncertain': True}
    node = get_node()
    key = f'_trigger_svc_{service_name.replace("/", "_")}'
    if not hasattr(node, key):
        setattr(node, key, node.create_client(Trigger, service_name))
    client = getattr(node, key)
    if not client.wait_for_service(timeout_sec=2.0):
        log.warning(f'Trigger service {service_name} was not available')
        return {'success': False, 'message': f'Service {service_name} not available'}
    log.info(f'Calling trigger service {service_name}')
    try:
        future = client.call_async(Trigger.Request())
    except Exception as exc:
        failure = _service_failure(service_name, sent=True, message=str(exc))
        return {'success': False, 'message': response_message(failure), 'execution_uncertain': True}
    start = time.monotonic()
    while not future.done():
        if time.monotonic() - start > timeout_sec:
            future.cancel()
            log.warning(f'Trigger service {service_name} timed out after {timeout_sec:.1f}s')
            failure = _service_failure(service_name, timed_out=True, sent=True,
                message=f'Timeout waiting for {service_name}; server completion is unknown')
            return {'success': False, 'message': response_message(failure), 'execution_uncertain': True}
        time.sleep(0.05)
    try:
        result = future.result()
        return {'success': result.success, 'message': result.message}
    except Exception as exc:
        failure = _service_failure(service_name, sent=True, message=str(exc))
        return {'success': False, 'message': response_message(failure), 'execution_uncertain': True}

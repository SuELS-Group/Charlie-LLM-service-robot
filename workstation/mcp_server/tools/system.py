"""Internal input handling. These tools are excluded from the LLM catalog."""
import json

from action_interfaces.srv import ExecuteAction
from mcp_server.call_state import request_id, request_issue_time
from mcp_server.decorators import ToolStatus, tool_result, tool_with_meta
from mcp_server.ros_utils import call_service, response_message, response_status, STATUS_SUCCESS


def _report_possession(state, operation, source):
    request = ExecuteAction.Request()
    request.action_type = operation
    request.parameters = json.dumps({'_issued_at': request_issue_time(), '_request_id': request_id()})
    response = call_service('/k2/object_state', ExecuteAction, request, timeout_sec=2.)
    try:
        data = json.loads(getattr(response, 'result_data', '') or '{}')
        if not isinstance(data, dict): data = {}
    except (TypeError, ValueError):
        data = {}
    if (response_status(response) == STATUS_SUCCESS and data.get('possession') == state and
            data.get('possession_source') == source and data.get('human_report_recorded') is True):
        return tool_result(ToolStatus.SUCCESS, result=data)
    return tool_result(ToolStatus.FAILURE, result=data, error={
        'code': data.get('error_code', 'OBJECT_STATE_UNAVAILABLE'),
        'message': response_message(response, 'Human gripper-state report was not confirmed'),
        'retryable': False, 'execution_started': False, 'execution_uncertain': False})


@tool_with_meta(returns_data=True, wait_duration=3.)
def report_empty_gripper() -> dict:
    """Record a clear human empty-gripper report. Internal; never moves the robot."""
    return _report_possession('empty', 'report_empty', 'explicit_user_empty_report')


@tool_with_meta(returns_data=True, wait_duration=3.)
def report_holding_gripper() -> dict:
    """Record a clear human held-item report. Internal; never moves the robot."""
    return _report_possession('holding_assumed', 'report_holding', 'explicit_user_holding_report')

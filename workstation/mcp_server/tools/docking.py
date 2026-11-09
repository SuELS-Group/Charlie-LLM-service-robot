import json

from mcp_server.decorators import ToolStatus, tool_result, tool_with_meta
from mcp_server.ros_utils import STATUS_STOPPED, STATUS_SUCCESS, STATUS_TIMEOUT, call_service, response_message, response_status
from workstation_common import get_logger

from action_interfaces.srv import ExecuteAction

log = get_logger('mcp.docking')


@tool_with_meta(wait_duration=180.0)
def dock_robot() -> dict:
    """
    Dock with the charging station.
    Use when the user asks to dock, recharge, or return to the charger.
    """
    try:
        log.info('Docking requested')
        request = ExecuteAction.Request()
        request.action_type = 'dock'
        request.parameters = ''
        response = call_service('/execute_action', ExecuteAction, request, timeout_sec=150.0)
        status = response_status(response)
        try:
            evidence = json.loads(getattr(response, 'result_data', '') or '{}')
            evidence = evidence if isinstance(evidence, dict) else {}
        except (TypeError, ValueError):
            evidence = {}
        error_state = {'retryable': False,
            'execution_uncertain': evidence.get('execution_uncertain', status == STATUS_TIMEOUT)}
        if status == STATUS_STOPPED:
            return tool_result(ToolStatus.STOPPED, result=evidence, error={
                **error_state,
                'code': 'GLOBAL_STOP',
                'message': response_message(response, 'Docking stopped')})
        if status == STATUS_TIMEOUT:
            return tool_result(ToolStatus.TIMEOUT, result=evidence, error={
                **error_state,
                'code': 'DOCKING_TIMEOUT',
                'message': response_message(response, 'Docking timed out')})
        if status == STATUS_SUCCESS:
            if evidence.get('completion_confirmed') is not True or evidence.get('execution_uncertain'):
                return tool_result(ToolStatus.FAILURE, result=evidence, error={
                    'code': 'COMPLETION_UNCONFIRMED', 'message': 'Docking controller completion was not confirmed',
                    'retryable': False, 'execution_uncertain': True})
            if (not isinstance(evidence, dict) or evidence.get('charger_connected') is not True or
                    not (evidence.get('charging') is True or evidence.get('battery_full') is True)):
                return tool_result(ToolStatus.FAILURE, error={'code': 'CHARGING_UNCONFIRMED',
                    'message': 'Docking returned no verified charging/full-battery evidence', 'retryable': False})
            announcement = ("I've docked and I'm now charging." if evidence.get('charging') is True
                            else "I've docked. My battery is already full.")
            return tool_result(ToolStatus.SUCCESS, result={
                'message': response_message(response, 'Docked'),
                **evidence}, meta={'announcement': announcement})
        msg = response_message(response, 'Docking failed')
        return tool_result(ToolStatus.FAILURE, result=evidence, error={**error_state, 'code': 'DOCKING_FAILED', 'message': msg})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e),
            'retryable': False, 'execution_uncertain': True})

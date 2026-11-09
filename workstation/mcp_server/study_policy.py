"""Study-policy client. The robot owns the authoritative profile."""
import json
import os


def mode():
    value = os.environ.get('ROBOT_OPERATION_MODE', 'development')
    if value not in {'development', 'study'}:
        raise ValueError('ROBOT_OPERATION_MODE must be development or study')
    return value


def query_policy(tool=None, arguments=None, root=None):
    from action_interfaces.srv import ExecuteAction
    from mcp_server.ros_utils import call_service, response_status, response_message, STATUS_SUCCESS
    request = ExecuteAction.Request()
    request.action_type = 'validate_tool' if tool else 'summary'
    request.parameters = json.dumps({'tool': tool, 'arguments': arguments or {}, 'root_tool': root})
    response = call_service('/k2/study_policy', ExecuteAction, request, timeout_sec=2.)
    if response is None:
        return {}, ('STUDY_PROFILE_UNAVAILABLE', 'Robot study policy service is unavailable')
    try:
        summary = json.loads(getattr(response, 'result_data', '') or '{}')
        if not isinstance(summary, dict) or summary.get('mode') not in {'development', 'study'}:
            return {}, ('STUDY_PROFILE_UNAVAILABLE', 'Robot study policy response is unavailable or invalid')
        if summary['mode'] != mode():
            return {}, ('STUDY_PROFILE_UNAVAILABLE', 'Robot and workstation study modes do not match')
    except (ValueError, TypeError):
        return {}, ('STUDY_PROFILE_UNAVAILABLE', 'Study policy is unavailable')
    if response_status(response) != STATUS_SUCCESS:
        return summary, (summary.get('error_code', 'POLICY_DENIED'),
                         response_message(response, 'Study policy denied this action'))
    return summary, None


def check_tool(tool, arguments, root):
    try:
        if mode() == 'development':
            return None
    except ValueError as exc:
        return 'STUDY_PROFILE_UNAVAILABLE', str(exc)
    _, error = query_policy(tool, dict(arguments), root)
    return error


def registry_view(registry):
    if mode() == 'development':
        return list(registry.values())
    summary, error = query_policy()
    if error:
        raise RuntimeError(error[1])
    allowed = set(summary.get('allowed_tools') or [])
    return [{**tool, 'meta': {**tool.get('meta', {}), 'allowed_in_mode': ['study'],
                'risk_level': 'physical' if name in {'take_from_hand', 'give_to_person', 'move_to_waypoint'} else 'read_or_communication'}}
            for name, tool in registry.items() if name in allowed]

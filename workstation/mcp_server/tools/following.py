from mcp_server.decorators import ToolStatus, tool_result, tool_with_meta
from mcp_server.ros_utils import STATUS_SUCCESS, call_service, response_message, response_status
from workstation_common import get_logger

from command_interfaces.srv import StartFollowing, StopFollowing

log = get_logger('mcp.following')


@tool_with_meta(announcement='I\'m now following you.', wait_duration=2.0, )
def start_following(target: str = 'person', distance: float = 0.8) -> dict:
    """
    Start following `target` at `distance` meters.
    Use for person-following or object-following tasks.
    `target` is usually `person`; `distance` is the desired follow distance in meters.
    """
    try:
        log.info(f'Start-following requested for {target} at distance={distance}')
        request = StartFollowing.Request()
        request.target = target
        request.distance = float(distance)
        response = call_service('/command_node/start_following', StartFollowing, request, timeout_sec=5.0)
        if response_status(response) == STATUS_SUCCESS:
            return tool_result(ToolStatus.SUCCESS, result={
                'message': response_message(response),
                'target': response.target,
                'distance': response.distance})
        message = response_message(response, 'Could not start following')
        return tool_result(ToolStatus.FAILURE, error={'code': 'FOLLOW_START_FAILED', 'message': message})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(announcement='I\'ve stopped following.', wait_duration=1.0, )
def stop_following() -> dict:
    """
    Stop the current follow behavior and stay in place.
    """
    try:
        log.info('Stop-following requested')
        response = call_service('/command_node/stop_following', StopFollowing, StopFollowing.Request(), timeout_sec=5.0)
        if response_status(response) == STATUS_SUCCESS:
            return tool_result(ToolStatus.SUCCESS, result={
                'message': response_message(response),
                'was_active': response.was_active})
        message = response_message(response, 'Could not stop following')
        return tool_result(ToolStatus.FAILURE, error={'code': 'FOLLOW_STOP_FAILED', 'message': message})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})

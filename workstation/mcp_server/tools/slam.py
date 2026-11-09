from mcp_server.decorators import ToolStatus, tool_result, tool_with_meta
from mcp_server.ros_utils import call_trigger_service
from workstation_common import get_logger

log = get_logger('mcp.slam')


def _get_mode_description(mode: str) -> str:
    descriptions = {
        'localization': 'Robot is localizing within existing map.',
        'mapping': 'Robot is in mapping mode. New areas will be added to the map.', }
    return descriptions.get(mode, f'Unknown mode: {mode}')


@tool_with_meta(wait_duration=2.0, )
def get_slam_mode() -> dict:
    """
    Get the current SLAM mode: `localization` or `mapping`.
    Use when the user asks whether the robot is mapping or using an existing map.
    """
    try:
        log.info('SLAM mode requested')
        result = call_trigger_service('/slam_mode_manager/get_current_mode', timeout_sec=5.0)
        if result and result['success']:
            mode = result['message']
            return tool_result(ToolStatus.SUCCESS, result={'mode': mode, 'description': _get_mode_description(mode)})
        msg = result.get('message', 'Service unavailable') if result else 'Service unavailable'
        return tool_result(ToolStatus.FAILURE, error={'code': 'SERVICE_FAILED', 'message': msg})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(wait_duration=15.0, )
def switch_to_mapping_mode() -> dict:
    """
    Switch SLAM to mapping mode.
    Use before exploring new unmapped areas.
    """
    try:
        log.info('Switching SLAM to mapping mode')
        result = call_trigger_service('/slam_mode_manager/switch_to_mapping', timeout_sec=30.0)
        if result and result['success']:
            return tool_result(ToolStatus.SUCCESS, result={'message': result['message'], 'mode': 'mapping'})
        msg = result.get('message', 'Service unavailable') if result else 'Service unavailable'
        return tool_result(ToolStatus.FAILURE, error={'code': 'SWITCH_FAILED', 'message': msg})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(wait_duration=15.0, )
def switch_to_localization_mode() -> dict:
    """
    Switch SLAM to localization mode.
    Use for stable navigation on an existing map.
    """
    try:
        log.info('Switching SLAM to localization mode')
        result = call_trigger_service('/slam_mode_manager/switch_to_localization', timeout_sec=30.0)
        if result and result['success']:
            return tool_result(ToolStatus.SUCCESS, result={'message': result['message'], 'mode': 'localization'})
        msg = result.get('message', 'Service unavailable') if result else 'Service unavailable'
        return tool_result(ToolStatus.FAILURE, error={'code': 'SWITCH_FAILED', 'message': msg})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})

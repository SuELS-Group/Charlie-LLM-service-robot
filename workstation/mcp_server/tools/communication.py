from mcp_server.config import config
from mcp_server.decorators import ToolStatus, tool_result, tool_with_meta
from mcp_server.ros_utils import publish_string
from workstation_common import get_logger
from mcp_server.call_state import request_id, request_issue_time, request_language

log = get_logger('mcp.communication')


@tool_with_meta(wait_duration=5.0, is_async=True, )
def speak(instruction: str) -> dict:
    """
    Speak `instruction` aloud with text-to-speech.
    Use this only when the user explicitly wants standalone speech that is not already covered by a tool announcement.
    Do not use it just to relay results from tools that can already be announced with `should_announce=true`.
    """
    try:
        if not instruction or not instruction.strip():
            return tool_result(ToolStatus.FAILURE, error={
                'code': 'EMPTY_INSTRUCTION',
                'message': 'No instruction provided'})
        log.info(f'Speak requested: {instruction[:80]}')
        publish_string(config.ros_response_subscriber, {'type': 'tts_request', 'text': instruction,
            'request_id': request_id(), 'lang': request_language(), 'issued_at': request_issue_time()})
        return tool_result(ToolStatus.SUCCESS, result={'delivered': True})
    except Exception as exc:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(exc)})

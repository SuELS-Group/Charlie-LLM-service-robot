import json

from mcp_server.decorators import ToolStatus, tool_result, tool_with_meta
from mcp_server.ros_utils import STATUS_STOPPED, STATUS_SUCCESS, STATUS_TIMEOUT, call_service, response_message, response_status
from workstation_common import get_logger

from action_interfaces.srv import ExecuteAction
from vision_interfaces.srv import VlmQuery

log = get_logger('mcp.perception')


def _format_objects(items):
    if not items:
        return 'nothing'
    parts = []
    for obj in items:
        raw = obj.get('item_name', 'unknown')
        label = raw.rsplit('_', 1)[0] if '_' in raw and raw.rsplit('_', 1)[1].isdigit() else raw
        face_name = obj.get('face_name')
        desc = obj.get('description', '')
        dist = obj.get('distance')
        if face_name:
            part = f'{face_name} ({label})'
        elif desc:
            part = desc
        else:
            part = f'a {label}'
        if dist is not None:
            part += f' about {dist:.1f}m away'
        parts.append(part)
    return ', '.join(parts)


def _format_tracked_people(people):
    if not people:
        return 'no people'
    parts = []
    for person in people:
        name = person.get('face_name') or f'person {person.get("track_id", "?")}'
        state = str(person.get('state') or 'last_known').replace('_', ' ')
        age = float(person.get('age_seconds', 0.0))
        parts.append(f'{name} ({state}, {age:.1f}s ago)')
    return ', '.join(parts)


def _flatten_track_positions(tracks):
    flattened = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        item = dict(track)
        position = item.get('position') or {}
        for axis in ('x', 'y', 'z'):
            if axis in position:
                item[axis] = position[axis]
        flattened.append(item)
    return flattened


@tool_with_meta(announcement='I see {result.summary}.', wait_duration=30.0, provides=[{
    'state': 'scene_objects',
    'path': 'result.objects[]'}], )
def get_look_around_objects() -> dict:
    """
    Scan around and return objects with labels, coordinates, and distances.
    Use for manipulation planning or explicit look-around requests.
    """
    try:
        log.info('Starting look-around object scan')
        request = ExecuteAction.Request()
        request.action_type = 'get_look_around_objects'
        request.parameters = ''
        response = call_service('/execute_action', ExecuteAction, request, timeout_sec=60.0)
        status = response_status(response)
        if status == STATUS_STOPPED:
            return tool_result(ToolStatus.STOPPED, error={
                'code': 'GLOBAL_STOP',
                'message': response_message(response, 'Scan stopped')})
        if status == STATUS_TIMEOUT:
            return tool_result(ToolStatus.TIMEOUT, error={
                'code': 'SCAN_TIMEOUT',
                'message': response_message(response, 'Scan timed out')})
        if status != STATUS_SUCCESS:
            return tool_result(ToolStatus.FAILURE, error={
                'code': 'SCAN_FAILED',
                'message': response_message(response, 'Look-around scan failed'),
                'retryable': True})
        json_items = json.loads(response.result_data) if response.result_data else []
        return tool_result(ToolStatus.SUCCESS, result={
            'count': len(json_items),
            'objects': json_items,
            'summary': _format_objects(json_items)}, meta={
            'message': 'No objects found.' if not json_items else None}, )
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e), 'retryable': True})


@tool_with_meta(announcement='I see {result.summary}.', wait_duration=8.0, provides=[{
    'state': 'visible_objects',
    'path': 'result.objects[]'}], )
def get_visible_objects() -> dict:
    """
    Return objects visible in the current view.
    No head movement.
    Use for quick perception queries like “what do you see right now?”.
    """
    try:
        log.info('Requesting visible objects from current camera view')
        request = ExecuteAction.Request()
        request.action_type = 'get_visible_objects'
        request.parameters = ''
        response = call_service('/execute_action', ExecuteAction, request, timeout_sec=15.0)
        status = response_status(response)
        if status == STATUS_STOPPED:
            return tool_result(ToolStatus.STOPPED, error={
                'code': 'GLOBAL_STOP',
                'message': response_message(response, 'Visible-object scan stopped')})
        if status == STATUS_TIMEOUT:
            return tool_result(ToolStatus.TIMEOUT, error={
                'code': 'SCAN_TIMEOUT',
                'message': response_message(response, 'Visible-object scan timed out')})
        if status != STATUS_SUCCESS:
            return tool_result(ToolStatus.FAILURE, error={
                'code': 'SCAN_FAILED',
                'message': response_message(response, 'Visible-object scan failed'),
                'retryable': True})
        json_items = json.loads(response.result_data) if response.result_data else []
        return tool_result(ToolStatus.SUCCESS, result={
            'count': len(json_items),
            'objects': json_items,
            'summary': _format_objects(json_items)}, meta={
            'message': 'No objects found.' if not json_items else None}, )
    except Exception as exc:
        log.error(f'Visible-object request failed: {exc}')
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(exc), 'retryable': True})


@tool_with_meta(announcement='I remember {result.summary}.', wait_duration=5.0, provides=[{
                                                                                              'state': 'tracked_people',
                                                                                              'path': 'result.people[]'}], )
def get_tracked_people(person: str = None, include_anonymous: bool = False, navigation_only: bool = False) -> dict:
    """
    Return people remembered on the navigation map, including their last known
    map coordinates, age, uncertainty, and whether the location is safe enough
    for navigation. Set `person` to an exact recognized face name to filter.
    A location can remain visible here after it is too old for navigation.
    """
    try:
        params = {'include_anonymous': bool(include_anonymous), 'navigation_only': bool(navigation_only), }
        if person:
            params['person'] = str(person)
        request = ExecuteAction.Request()
        request.action_type = 'get_tracked_people'
        request.parameters = json.dumps(params)
        response = call_service('/execute_action', ExecuteAction, request, timeout_sec=5.0)
        if response_status(response) != STATUS_SUCCESS:
            return tool_result(ToolStatus.FAILURE, error={
                'code': 'PERSON_TRACKER_UNAVAILABLE',
                'message': response_message(response, 'Person tracker is not available'),
                'retryable': True, })
        tracks = json.loads(response.result_data) if response.result_data else []
        people = _flatten_track_positions(tracks)
        return tool_result(ToolStatus.SUCCESS, result={
            'count': len(people),
            'people': people,
            'summary': _format_tracked_people(people), }, meta={
            'message': 'No remembered people found.' if not people else None, })
    except Exception as exc:
        log.error(f'Tracked-person request failed: {exc}')
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(exc), 'retryable': True, })


@tool_with_meta(announcement='{result.description}', wait_duration=8.0, provides=[{
    'state': 'scene_description',
    'path': 'result.description'}], )
def get_scene_description() -> dict:
    """
    Describe the current scene in short natural language.
    Use when the user wants a spoken scene summary instead of object coordinates.
    """
    try:
        log.info('Requesting VLM scene description')
        request = VlmQuery.Request()
        request.prompt = 'Describe what you see in 2-3 short spoken sentences. Name the key objects and people you notice. No markdown, no bullet points, no numbering, no obstacle analysis. Write as if speaking aloud.'
        request.camera = 'head'
        feedback = call_service('/vision_node/vlm_query', VlmQuery, request, timeout_sec=15.0)
        status = response_status(feedback)
        if status == STATUS_TIMEOUT:
            return tool_result(ToolStatus.TIMEOUT, error={
                'code': 'VLM_TIMEOUT',
                'message': response_message(feedback, 'VLM query timed out'),
                'retryable': True})
        if status == STATUS_SUCCESS:
            description = feedback.response_text
            if description:
                return tool_result(ToolStatus.SUCCESS, result={'description': description})
            return tool_result(ToolStatus.FAILURE, error={
                'code': 'NO_RESPONSE',
                'message': 'No description received',
                'retryable': True})
        error_msg = response_message(feedback, 'No response from vision node')
        return tool_result(ToolStatus.FAILURE, error={'code': 'VLM_FAILURE', 'message': error_msg, 'retryable': True})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(announcement='I\'ve moved my head.', wait_duration=5.0)
def move_head(pan: float = None, tilt: float = None, duration: float = 1.5) -> dict:
    """
    Move the robot's head camera to the specified pan and/or tilt angle in radians.
    Use to look left/right (pan) or up/down (tilt).
    Angles are in radians. Typical ranges:
      pan:  -1.57 (left) to 1.57 (right), 0.0 = center
      tilt: -0.8 (up) to 0.5 (down), 0.0 = straight
    `duration` controls movement speed in seconds.
    At least one of pan or tilt must be provided.
    """
    if pan is None and tilt is None:
        return tool_result(ToolStatus.FAILURE, error={
            'code': 'MISSING_PARAM',
            'message': 'At least one of pan or tilt must be specified'})
    try:
        params: dict = {'duration': float(duration)}
        if pan is not None:
            params['pan'] = float(pan)
        if tilt is not None:
            params['tilt'] = float(tilt)
        log.info(f'move_head requested: {params}')
        request = ExecuteAction.Request()
        request.action_type = 'move_head'
        request.parameters = json.dumps(params)
        response = call_service('/execute_action', ExecuteAction, request, timeout_sec=float(duration) + 5.0)
        status = response_status(response)
        if status == STATUS_STOPPED:
            return tool_result(ToolStatus.STOPPED, error={
                'code': 'GLOBAL_STOP',
                'message': response_message(response, 'Head movement stopped')})
        if status == STATUS_SUCCESS:
            return tool_result(ToolStatus.SUCCESS, result={
                'message': response_message(response, 'Head moved'),
                'pan': pan,
                'tilt': tilt})
        return tool_result(ToolStatus.FAILURE, error={
            'code': 'MOVE_HEAD_FAILED',
            'message': response_message(response, 'Head movement failed')})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})

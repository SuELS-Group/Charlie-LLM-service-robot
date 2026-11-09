import json
import math

from mcp_server.decorators import ToolStatus, tool_result, tool_with_meta
from mcp_server.ros_utils import STATUS_STOPPED, STATUS_SUCCESS, STATUS_TIMEOUT, action_message, action_status, call_action, call_service, response_message, response_status
from workstation_common import get_logger
from mcp_server.study_policy import mode as operation_mode, query_policy

from action_interfaces.srv import ExecuteAction
from command_interfaces.action import MoveTo as MoveToAction, MoveToWaypoint as MoveToWaypointAction, PositionForAction as PositionForActionAction, TurnTo as TurnToAction, TurnToRelative as TurnToRelativeAction
from command_interfaces.srv import GetPosition
from waypoint_interfaces.srv import (DeleteWaypoint, ListWaypoints, SavePointedWaypoint, SaveWaypoint, )

log = get_logger('mcp.navigation')
_PERSON_POSITION_TOLERANCE_M = 0.3
_PERSON_FACING_TOLERANCE_DEG = 20.0
_POINTING_HYSTERESIS_SECONDS = 2.0


def _strip_instance_suffix(name: str) -> str:
    raw = str(name or '').strip()
    if '_' in raw:
        prefix, suffix = raw.rsplit('_', 1)
        if suffix.isdigit():
            return prefix
    return raw


def _base_label(name: str) -> str:
    return _strip_instance_suffix(name).lower()


def _is_person(item: dict) -> bool:
    label = _base_label(item.get('item_name', ''))
    return label == 'person' or label.startswith('person (') or bool(item.get('face_name'))


def _normalize_person_name(name: str) -> str:
    return ' '.join(str(name or '').split()).casefold()


def _person_name(item: dict) -> str:
    explicit_name = str(item.get('face_name') or '').strip()
    if explicit_name and _normalize_person_name(explicit_name) != 'unknown':
        return explicit_name
    label = _strip_instance_suffix(item.get('item_name', ''))
    if label.casefold().startswith('person (') and label.endswith(')'):
        return label[len('person ('):-1].strip()
    return ''


def _horizontal_distance(item: dict, robot_x: float = 0.0, robot_y: float = 0.0) -> float:
    return math.hypot(float(item.get('x', 0.0)) - float(robot_x), float(item.get('y', 0.0)) - float(robot_y))


def _select_person(items, robot_x: float = 0.0, robot_y: float = 0.0, person_name: str = None):
    people = [item for item in items if isinstance(item, dict) and _is_person(item)]
    wanted_name = _normalize_person_name(person_name)
    if wanted_name:
        people = [item for item in people if _normalize_person_name(_person_name(item)) == wanted_name]
    valid = []
    for item in people:
        try:
            distance = _horizontal_distance(item,robot_x,robot_y)
            if math.isfinite(distance): valid.append((distance,item))
        except (TypeError,ValueError,OverflowError):
            continue
    if not valid:
        return None
    return min(valid,key=lambda candidate:candidate[0])[1]


def _named_recipient_ambiguity(items, name):
    if not name:
        return None
    wanted = _normalize_person_name(name)
    matches = [item for item in items if isinstance(item,dict) and _is_person(item) and
        _normalize_person_name(_person_name(item)) == wanted]
    if len(matches) > 1:
        return tool_result(ToolStatus.FAILURE,error={'code':'RECIPIENT_AMBIGUOUS',
            'message':f'Multiple visible tracks match {name}', 'retryable':False,'execution_uncertain':False})
    return None


def _person_not_found(person_name: str = None) -> dict:
    requested_name = str(person_name or '').strip()
    if requested_name:
        return tool_result(ToolStatus.FAILURE, error={
            'code': 'TARGET_PERSON_NOT_FOUND',
            'message': f'Could not find {requested_name} during the person scan',
            'retryable': True, })
    return tool_result(ToolStatus.FAILURE, error={
        'code': 'NO_PERSON_FOUND',
        'message': 'No person was found during the scan',
        'retryable': True, })


def _person_standoff_goal(robot_x: float, robot_y: float, person_x: float, person_y: float, distance: float) -> tuple[
    float, float, float]:
    angle_to_person = math.atan2(person_y - robot_y, person_x - robot_x)
    goal_x = person_x - distance * math.cos(angle_to_person)
    goal_y = person_y - distance * math.sin(angle_to_person)
    goal_orientation = math.degrees(angle_to_person) % 360.0
    return goal_x, goal_y, goal_orientation


def _base_item_to_map(item: dict, position) -> dict:
    """Convert a base_link detection into the map frame used by navigation."""
    yaw_rad = math.radians(float(position.yaw_deg))
    base_x = float(item.get('x', 0.0))
    base_y = float(item.get('y', 0.0))
    mapped = dict(item)
    mapped['x'] = (float(position.x) + base_x * math.cos(yaw_rad) - base_y * math.sin(yaw_rad))
    mapped['y'] = (float(position.y) + base_x * math.sin(yaw_rad) + base_y * math.cos(yaw_rad))
    return mapped


def _map_item_to_base(item: dict, position) -> dict:
    """Convert a map-frame scan result into the current base_link frame."""
    yaw_rad = math.radians(float(position.yaw_deg))
    delta_x = float(item.get('x', 0.0)) - float(position.x)
    delta_y = float(item.get('y', 0.0)) - float(position.y)
    based = dict(item)
    based['x'] = delta_x * math.cos(yaw_rad) + delta_y * math.sin(yaw_rad)
    based['y'] = -delta_x * math.sin(yaw_rad) + delta_y * math.cos(yaw_rad)
    return based


def _person_is_near_and_facing(person_in_base: dict, distance: float) -> bool:
    person_x = float(person_in_base.get('x', 0.0))
    person_y = float(person_in_base.get('y', 0.0))
    person_distance = math.hypot(person_x, person_y)
    bearing_degrees = math.degrees(math.atan2(person_y, person_x))
    return (
            float(distance) <= person_distance <= float(distance) + _PERSON_POSITION_TOLERANCE_M and abs(bearing_degrees) <= _PERSON_FACING_TOLERANCE_DEG)


def _already_positioned_result(person_in_base: dict, position, distance: float) -> dict:
    result = {
        'message': 'Already within range and facing the person',
        'goal_x': float(position.x),
        'goal_y': float(position.y),
        'goal_orientation': float(position.yaw_deg) % 360.0,
        'target': person_in_base.get('item_name', 'person'),
        'distance': float(distance),
        'person_distance': _horizontal_distance(person_in_base),
        'approached': False, }
    detected_name = _person_name(person_in_base)
    if detected_name:
        result['person_name'] = detected_name
    return tool_result(ToolStatus.SUCCESS, result=result)


def _get_robot_position():
    response = call_service('/command_node/get_position', GetPosition, GetPosition.Request(), timeout_sec=5.0)
    if response is None or response_status(response) != STATUS_SUCCESS:
        return None, tool_result(ToolStatus.FAILURE, error={
            'code': 'NO_POSITION',
            'message': response_message(response, 'Could not retrieve current robot position')})
    return response, None


def _get_navigation_people(person_name: str, navigation_only: bool = True) -> list[dict]:
    request = ExecuteAction.Request()
    request.action_type = 'get_tracked_people'
    request.parameters = json.dumps({
        'person': person_name,
        'include_anonymous': False,
        'navigation_only': bool(navigation_only), })
    try:
        response = call_service('/execute_action', ExecuteAction, request, timeout_sec=5.0)
    except Exception as exc:
        log.warning(f'Could not query remembered people: {exc}')
        return []
    if response_status(response) != STATUS_SUCCESS:
        return []
    try:
        tracks = json.loads(response.result_data) if response.result_data else []
    except (AttributeError, TypeError, json.JSONDecodeError):
        return []
    people = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        if navigation_only and track.get('navigation_eligible') is not True:
            continue
        person = dict(track)
        position = person.get('position') or {}
        try:
            person['x'] = float(position['x'])
            person['y'] = float(position['y'])
            person['z'] = float(position.get('z', 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(person[key]) for key in ('x', 'y', 'z')):
            continue
        people.append(person)
    return people


def _mark_person_verification(result: dict, verification_source: str, face_confirmed: bool, intermediate_check_performed: bool, **details) -> dict:
    if isinstance(result.get('result'), dict):
        result['result'].update({
            'verification_source': verification_source,
            'target_selected': True,
            'target_verified': bool(face_confirmed),
            'face_confirmed': bool(face_confirmed),
            'intermediate_check_performed': bool(intermediate_check_performed), **details, })
    return result


def _turn_toward_map_position(item: dict, position) -> dict:
    """Rotate the base so the head camera faces a remembered map point."""
    delta_x = float(item.get('x', 0.0)) - float(position.x)
    delta_y = float(item.get('y', 0.0)) - float(position.y)
    target_distance = math.hypot(delta_x, delta_y)
    if target_distance < 0.05:
        return tool_result(ToolStatus.SUCCESS, result={
            'message': 'Remembered position is already at the robot',
            'goal_orientation': float(position.yaw_deg) % 360.0,
            'target': item.get('item_name', 'person'),
            'looked_toward_last_known': True,
            'turn_skipped': True, })
    target_orientation = math.degrees(math.atan2(delta_y, delta_x)) % 360.0
    log.info(f'Turning base toward remembered {item.get("item_name", "person")} '
             f'at map position ({float(item.get("x", 0.0)):.2f}, '
             f'{float(item.get("y", 0.0)):.2f}); '
             f'orientation={target_orientation:.1f}deg')
    goal = TurnToAction.Goal()
    goal.orientation = target_orientation
    result = _run_navigation_action('/command_node/turn_to', TurnToAction, goal, 60.0, failure_code='TURN_TO_PERSON_FAILED', timeout_code='TURN_TO_PERSON_TIMEOUT')
    if result.get('status') == ToolStatus.SUCCESS.value:
        result['result'].update({
            'goal_orientation': target_orientation,
            'target': item.get('item_name', 'person'),
            'looked_toward_last_known': True,
            'turn_skipped': False, })
    return result


def _drive_to_person_item(person: dict, position, distance: float) -> dict:
    safe_distance = max(float(distance), 1.3)
    person_x = float(person.get('x', 0.0))
    person_y = float(person.get('y', 0.0))
    goal_x, goal_y, goal_orientation = _person_standoff_goal(float(position.x), float(position.y), person_x, person_y, safe_distance)
    goal = MoveToAction.Goal()
    goal.x = goal_x
    goal.y = goal_y
    goal.orientation = goal_orientation
    result = _run_navigation_action('/command_node/move_to', MoveToAction, goal, 180.0, failure_code='DRIVE_TO_PERSON_FAILED', timeout_code='DRIVE_TO_PERSON_TIMEOUT')
    if result.get('status') == 'success':
        details = {
            'goal_x': goal_x,
            'goal_y': goal_y,
            'goal_orientation': goal_orientation,
            'target': person.get('item_name', 'person'),
            'distance': safe_distance,
            'person_distance': _horizontal_distance(person, float(position.x), float(position.y)), }
        detected_name = _person_name(person)
        if detected_name:
            details['person_name'] = detected_name
        result['result'].update(details)
    return result


def _visible_objects_snapshot():
    request = ExecuteAction.Request()
    request.action_type = 'get_visible_objects'
    request.parameters = ''
    response = call_service('/execute_action', ExecuteAction, request, timeout_sec=10.0)
    status = response_status(response)
    items = []
    if status == STATUS_SUCCESS and response.result_data:
        try:
            items = json.loads(response.result_data)
        except (TypeError, json.JSONDecodeError):
            items = []
    return status, items, response


def _approach_person(
        person_in_base: dict, position, distance: float,
        requested_name: str | None, *, location_source: str,
        verification_source: str = 'current_face', face_confirmed: bool = True, **details) -> dict:
    """Use the selected person position once; do not re-identify during the approach."""
    if _person_is_near_and_facing(person_in_base, distance):
        result = _already_positioned_result(
            person_in_base, position, distance)
    else:
        person_in_map = _base_item_to_map(person_in_base, position)
        result = _drive_to_person_item(person_in_map, position, distance)
        if isinstance(result.get('result'), dict):
            result['result']['approached'] = True
    if isinstance(result.get('result'), dict):
        result['result'].update({
            'location_source': location_source,
            **details, })
        name = _person_name(person_in_base)
        if name:
            result['result']['person_name'] = name
        track = person_in_base.get('track_id')
        if isinstance(track,int) and not isinstance(track,bool) and track >= 0:
            result['result']['recipient_track'] = track
    if requested_name:
        result = _mark_person_verification(
            result, verification_source, face_confirmed, False)
    return result


def _run_navigation_action(action_name: str, action_type, goal, timeout_sec: float, *, failure_code: str, timeout_code: str) -> dict:
    action_response = call_action(action_name, action_type, goal, timeout_sec=timeout_sec)
    if action_response.get('timed_out'):
        return tool_result(ToolStatus.TIMEOUT, result={
            'completion_confirmed': action_response.get('completion_confirmed', False),
            'terminal_status': action_response.get('terminal_status'),
            'completed_late': (action_response.get('completion_confirmed') is True and
                action_status(action_response.get('result')) == STATUS_SUCCESS)}, error={
            'code': timeout_code,
            'message': action_response.get('message') or 'Timeout', 'retryable': False,
            'execution_uncertain': not action_response.get('completion_confirmed', False),
            'cancellation_confirmed': action_response.get('cancellation_confirmed', False)})
    if not action_response.get('accepted'):
        message = action_response.get('message') or 'Navigation action was rejected'
        return tool_result(ToolStatus.FAILURE, error={'code': failure_code, 'message': message,
            'retryable': False, 'execution_uncertain': action_response.get('execution_uncertain', False)})
    result = action_response.get('result')
    status = action_status(result)
    message = action_message(result, 'Done')
    try:
        details = json.loads(getattr(result, 'result_data', '') or '{}')
        details = details if isinstance(details, dict) else {}
    except (ValueError, TypeError):
        details = {}
    error_state = {'retryable': False, 'execution_uncertain':
        action_response.get('execution_uncertain', False) or details.get('execution_uncertain', False)}
    if status == STATUS_STOPPED:
        return tool_result(ToolStatus.STOPPED, result=details, error={**error_state, 'code': 'GLOBAL_STOP', 'message': message})
    if status == STATUS_TIMEOUT:
        return tool_result(ToolStatus.TIMEOUT, result=details, error={**error_state, 'code': timeout_code, 'message': message})
    if status != STATUS_SUCCESS:
        controller_error = details.get('controller_error')
        controller_error = controller_error if isinstance(controller_error, dict) else {}
        code = ('RUNSTOP_ACTIVE' if controller_error.get('code') == 'RUNSTOP_ACTIVE' else
            'ARM_NOT_STOWED' if details.get('phase') == 'transit_posture' else
            'HEAD_PREPARATION_FAILED' if details.get('phase') == 'head_preparation' else failure_code)
        return tool_result(ToolStatus.FAILURE, result=details, error={**error_state, 'code': code, 'message': message})
    if error_state['execution_uncertain']:
        return tool_result(ToolStatus.FAILURE, result=details, error={**error_state,
            'code': 'COMPLETION_UNCONFIRMED', 'message': 'Navigation/head completion was not confirmed'})
    return tool_result(ToolStatus.SUCCESS, result={
        **details,
        'message': message,
        'goal_x': getattr(result, 'goal_x', getattr(result, 'x', 0.0)),
        'goal_y': getattr(result, 'goal_y', getattr(result, 'y', 0.0)),
        'goal_orientation': getattr(result, 'goal_orientation', getattr(result, 'orientation', 0.0)), })


@tool_with_meta(announcement='I\'ve moved to position ({x}, {y}).', wait_duration=120.0, invalidates=['scene_objects',
                                                                                                      'visible_objects'], )
def move_to(x: float, y: float, orientation: float) -> dict:
    """
    Move robot to exact map coordinates (x, y) with target orientation in degrees.
    Use only when the user provides specific numeric coordinates.
    For named locations, use move_to_waypoint() instead.
    """
    try:
        log.info(f'move_to requested: x={x}, y={y}, orientation={orientation}')
        goal = MoveToAction.Goal()
        goal.x = float(x)
        goal.y = float(y)
        goal.orientation = float(orientation)
        return _run_navigation_action('/command_node/move_to', MoveToAction, goal, 300.0, failure_code='NAV_FAILED', timeout_code='NAV_TIMEOUT', )
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(announcement='I\'ve moved {distance} meters {direction}.', wait_duration=60.0, invalidates=[
    'scene_objects',
    'visible_objects'], )
def move_relative(distance: float, direction: str = 'forward') -> dict:
    """
    Move the robot a relative distance in a direction from its current position.
    Use for relative movement commands: "move 1 meter forward", "go back 50 cm", "drive 2 meters right",
    or German commands: "bewege dich 1 meter nach vorne", "fahr zurück", "geh 2 Meter links", "fahre rechts".
    `distance` is in meters (e.g. 0.5 for 50 cm, 1.0 for 1 meter, 0.1 for 10 cm).
    `direction` must be one of: forward, backward, left, right.
      forward = vorwärts / nach vorne, backward = zurück / rückwärts, left = links, right = rechts
    The robot uses the navigation map so obstacle avoidance is active.
    """
    _DIRECTION_ALIASES = {
        'forward': 'forward',
        'vorwärts': 'forward',
        'vorne': 'forward',
        'vor': 'forward',
        'vorwaerts': 'forward',
        'backward': 'backward',
        'back': 'backward',
        'zurück': 'backward',
        'zurueck': 'backward',
        'rückwärts': 'backward',
        'rueckwaerts': 'backward',
        'hinten': 'backward',
        'left': 'left',
        'links': 'left',
        'right': 'right',
        'rechts': 'right', }
    try:
        direction_key = (direction or 'forward').strip().lower()
        direction_lower = _DIRECTION_ALIASES.get(direction_key)
        if direction_lower is None:
            return tool_result(ToolStatus.FAILURE, error={
                'code': 'INVALID_DIRECTION',
                'message': f'direction must be one of forward/backward/left/right (or German: vorne/zurück/links/rechts), got {direction!r}'})
        log.info(f'move_relative requested: distance={distance}, direction={direction_lower}')
        pos_response = call_service('/command_node/get_position', GetPosition, GetPosition.Request(), timeout_sec=5.0)
        if pos_response is None:
            return tool_result(ToolStatus.FAILURE, error={
                'code': 'NO_POSITION',
                'message': 'Could not retrieve current robot position'})
        current_x = float(pos_response.x)
        current_y = float(pos_response.y)
        yaw_deg = float(pos_response.yaw_deg)
        yaw_rad = math.radians(yaw_deg)
        d = float(distance)
        if direction_lower == 'forward':
            offset_rad = yaw_rad
        elif direction_lower == 'backward':
            offset_rad = yaw_rad + math.pi
        elif direction_lower == 'left':
            offset_rad = yaw_rad + math.pi / 2.0
        else:  # right
            offset_rad = yaw_rad - math.pi / 2.0
        goal_x = current_x + d * math.cos(offset_rad)
        goal_y = current_y + d * math.sin(offset_rad)
        log.info(f'move_relative: ({current_x:.2f}, {current_y:.2f}, {yaw_deg:.1f}°) -> ({goal_x:.2f}, {goal_y:.2f})')
        goal = MoveToAction.Goal()
        goal.x = goal_x
        goal.y = goal_y
        goal.orientation = yaw_deg
        result = _run_navigation_action('/command_node/move_to', MoveToAction, goal, 60.0, failure_code='MOVE_RELATIVE_FAILED', timeout_code='MOVE_RELATIVE_TIMEOUT')
        if result.get('status') == 'success':
            result['result']['distance'] = distance
            result['result']['direction'] = direction_lower
        return result
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(announcement='I have driven to the person.', wait_duration=90.0, invalidates=['scene_objects',
    'visible_objects'], )
def drive_to_person(distance: float = 1.3, person: str = None) -> dict:
    """
    Drive once to a selected person position, stopping at least 1.3 meters
    away and facing them.
    Set `person` to the requested person's recognized face name, for example `TestUserB`.
    For a request such as `[User TestUserA]: come to me`, set `person="TestUserA"`.
    When `person` is omitted, the nearest visible person is selected.
    A named request never falls back to a different or unidentified person.
    If a named person is not visible, use their last known map position when
    the person tracker marks it eligible for navigation. Otherwise search once.
    Once a person position is selected, drive there without requiring another
    face recognition or a stable YOLO track ID, including after a scan.
    Use when the user asks the robot to come or drive to them. This does not start a handoff or turn the robot side-on.
    A later take_from_hand call performs and restores its own base and head-camera handoff alignment.
    """
    try:
        requested_name = str(person or '').strip() or None
        safe_distance = max(float(distance), 1.3)
        log.info(f'drive_to_person requested for '
                 f'{requested_name or "nearest person"} with distance={distance}, '
                 f'safe_distance={safe_distance}')
        visible_status, visible_items, visible_response = (
            _visible_objects_snapshot())
        if visible_status == STATUS_STOPPED:
            return tool_result(ToolStatus.STOPPED, error={
                'code': 'GLOBAL_STOP',
                'message': response_message(visible_response, 'Person lookup stopped')})
        visible_person = _select_person(visible_items, person_name=requested_name)
        ambiguity = _named_recipient_ambiguity(visible_items,requested_name)
        if ambiguity: return ambiguity
        if visible_person is not None:
            position, error = _get_robot_position()
            if error is not None:
                return error
            return _approach_person(
                visible_person, position, safe_distance, requested_name,
                location_source='current_view')

        if requested_name:
            remembered_people = _get_navigation_people(requested_name, navigation_only=True)
            ambiguity = _named_recipient_ambiguity(remembered_people, requested_name)
            if ambiguity:
                return ambiguity
            remembered_person = _select_person(remembered_people, person_name=requested_name)
            if remembered_person is not None:
                log.info(f'Using {requested_name} last known map position for the approach')
                position, error = _get_robot_position()
                if error is not None:
                    return error
                return _approach_person(
                    _map_item_to_base(remembered_person, position), position, safe_distance,
                    requested_name, location_source='last_known_position',
                    verification_source='last_known_position', face_confirmed=False,
                    memory_age_seconds=remembered_person.get('age_seconds'))

        if operation_mode() == 'study' and visible_status != STATUS_SUCCESS:
            return tool_result(ToolStatus.FAILURE, error={'code': 'PERSON_VIEW_UNAVAILABLE',
                'message': 'No current view or navigation-eligible remembered person is available', 'retryable': False})

        log.info('No matching person in the current view; starting one '
                 'look-around scan')
        scan_request = ExecuteAction.Request()
        scan_request.action_type = 'get_look_around_objects'
        scan_request.parameters = ''
        scan_response = call_service('/execute_action', ExecuteAction, scan_request, timeout_sec=60.0)
        scan_status = response_status(scan_response)
        if scan_status == STATUS_STOPPED:
            return tool_result(ToolStatus.STOPPED, error={
                'code': 'GLOBAL_STOP',
                'message': response_message(scan_response, 'Person scan stopped')})
        if scan_status == STATUS_TIMEOUT:
            return tool_result(ToolStatus.TIMEOUT, error={
                'code': 'PERSON_SCAN_TIMEOUT',
                'message': response_message(scan_response, 'Person scan timed out')})
        if scan_status != STATUS_SUCCESS:
            return tool_result(ToolStatus.FAILURE, error={
                'code': 'PERSON_SCAN_FAILED',
                'message': response_message(scan_response, 'Could not scan for a person')})
        items = json.loads(scan_response.result_data) if scan_response.result_data else []
        scanned_person = _select_person(items, person_name=requested_name)
        if scanned_person is None:
            return _person_not_found(requested_name)
        position, error = _get_robot_position()
        if error is not None:
            return error
        target_person = _select_person(
            items, float(position.x), float(position.y), requested_name)
        ambiguity = _named_recipient_ambiguity(items, requested_name)
        if ambiguity:
            return ambiguity
        person_in_base = _map_item_to_base(target_person, position)
        return _approach_person(
            person_in_base, position, safe_distance, requested_name,
            location_source='look_around', verification_source='scan_face')
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(announcement='I\'ve rotated {degrees} degrees.', wait_duration=30.0, invalidates=['visible_objects'], )
def turn_to(degrees: int) -> dict:
    """
    Turn robot to an ABSOLUTE compass heading in degrees.
    Only use when the user specifies an exact heading (e.g. "face north", "turn to 180 degrees").
    For relative turns like "turn left" or "turn right", use turn_to_relative() instead.
    """
    try:
        log.info(f'turn_to requested: degrees={degrees}')
        goal = TurnToAction.Goal()
        goal.orientation = float(degrees)
        result = _run_navigation_action('/command_node/turn_to', TurnToAction, goal, 60.0, failure_code='TURN_FAILED', timeout_code='TURN_TIMEOUT', )
        if result.get('status') == 'success':
            result['result']['degrees'] = degrees
        return result
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(announcement='I\'ve rotated {angle} degrees.', wait_duration=30.0, invalidates=['visible_objects'], )
def turn_to_relative(angle: int = None, degrees: int = None) -> dict:
    """
    Turn robot by a RELATIVE angle from its current facing direction.
    Use for "turn left", "turn right", "rotate 90 degrees" — any relative rotation.
    Positive = left, negative = right. Example: turn_to_relative(angle=90) = turn left 90 degrees.
    """
    rotation_angle = angle if angle is not None else degrees
    if rotation_angle is None:
        return tool_result(ToolStatus.FAILURE, error={
            'code': 'MISSING_PARAM',
            'message': 'angle or degrees parameter required'})
    try:
        log.info(f'turn_to_relative requested: angle={rotation_angle}')
        goal = TurnToRelativeAction.Goal()
        goal.angle = float(rotation_angle)
        result = _run_navigation_action('/command_node/turn_to_relative', TurnToRelativeAction, goal, 60.0, failure_code='TURN_FAILED', timeout_code='TURN_TIMEOUT', )
        if result.get('status') == 'success':
            result['result']['angle'] = rotation_angle
        return result
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(announcement='I\'ve moved to action position for {target}.', wait_duration=30.0, requires=[{
    'state': 'scene_objects',
    'provider': 'get_look_around_objects'}], invalidates=['visible_objects'], )
def position_for_action(target: str, x: float, y: float, distance: float = 0.65, z: float = None, ) -> dict:
    """
    Move next to a scanned object so the arm can reach it.
    `target` is the exact `item_name` from the scan result, while `x` and `y` come from that same result.
    Some fixed-marker objects have stable names without a `_N` suffix.
    """
    del z
    try:
        log.info(f'position_for_action requested: target={target}, x={x}, y={y}, distance={distance}')
        goal = PositionForActionAction.Goal()
        goal.target = target or ''
        goal.x = float(x)
        goal.y = float(y)
        goal.distance = float(distance)
        return _run_navigation_action('/command_node/position_for_action', PositionForActionAction, goal, 300.0, failure_code='POSITION_FAILED', timeout_code='POSITION_TIMEOUT', )
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(announcement='I\'ve arrived at {name}.', wait_duration=120.0, requires=[{
    'state': 'waypoints',
    'provider': 'get_waypoints',
    'validate': {'arg': 'name', 'in': 'result.waypoints[]'}}], invalidates=['scene_objects', 'visible_objects'], )
def move_to_waypoint(name: str) -> dict:
    """
    Navigate to a saved waypoint by name.
    Use for named locations, not raw map coordinates.
    Use the exact saved waypoint name. Do not guess or invent names.
    """
    try:
        log.info(f'move_to_waypoint requested: {name}')
        goal = MoveToWaypointAction.Goal()
        goal.name = name.lower()
        result = _run_navigation_action('/command_node/move_to_waypoint', MoveToWaypointAction, goal, 300.0, failure_code='NAV_FAILED', timeout_code='NAV_TIMEOUT', )
        if result.get('status') == 'success':
            result['result']['name'] = name
        return result
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(announcement='I know these locations: {result.waypoints}.', wait_duration=5.0, provides=[{
    'state': 'waypoints',
    'path': 'result.waypoints[]'}], )
def get_waypoints() -> dict:
    """
    Return all saved waypoint names.
    Use when navigation depends on a named location.
    """
    try:
        log.info('get_waypoints requested')
        response = call_service('/waypoint/list', ListWaypoints, ListWaypoints.Request(), timeout_sec=5.0)
        if response_status(response) == STATUS_SUCCESS:
            names = [waypoint.name for waypoint in response.waypoints]
            if operation_mode() == 'study':
                profile, error = query_policy()
                if error:
                    return tool_result(ToolStatus.FAILURE, error={'code': error[0], 'message': error[1], 'retryable': False})
                names = [name for name in names if name in profile.get('approved_waypoints', {})]
            log.info(f'Waypoints returned: {names}')
            return tool_result(ToolStatus.SUCCESS, result={'count': len(names), 'waypoints': names})
        message = response_message(response, 'Could not retrieve waypoints')
        log.warning(message)
        return tool_result(ToolStatus.FAILURE, error={'code': 'NO_RESPONSE', 'message': message})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(announcement='I\'ve saved this location as {name}.', wait_duration=1.0, invalidates=['waypoints'], )
def save_waypoint(name: str) -> dict:
    """
    Save the robot's current position as a named waypoint for future navigation.
    Do not use this when a person is pointing at a location; call
    save_pointed_waypoint() so the pointed location is saved instead of the
    robot's current pose.
    """
    try:
        log.info(f'save_waypoint requested: {name}')
        request = SaveWaypoint.Request()
        request.name = name.lower()
        response = call_service('/waypoint/save', SaveWaypoint, request, timeout_sec=5.0)
        if response_status(response) == STATUS_SUCCESS:
            saved_name = response.waypoint.name or name.lower()
            return tool_result(ToolStatus.SUCCESS, result={'message': response_message(response), 'name': saved_name})
        message = response_message(response, 'Could not save waypoint')
        return tool_result(ToolStatus.FAILURE, error={'code': 'SAVE_FAILED', 'message': message})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(announcement='{result.message}', wait_duration=1.0, invalidates=['waypoints'], returns_data=True, )
def save_pointed_waypoint(name: str = 'pointed_location') -> dict:
    """
    Save the location indicated by one person's right-arm pointing gesture.
    Call this while, or immediately after, the user points. No coordinates are
    needed. The detector accepts its last valid pointing location for at most
    two seconds, clips the ray at mapped walls, and chooses connected free map
    space near the indicated ground point. The returned ``pointing_status`` is
    explicit: ``success``, ``wall_hit``, ``no_person``,
    ``multiple_people``, ``no_pointing``, or ``expired_hysteresis``.
    """
    normalized_name = ' '.join(str(name or '').split())
    if not normalized_name:
        return tool_result(ToolStatus.FAILURE, error={
            'code': 'INVALID_WAYPOINT_NAME',
            'message': 'A waypoint name is required.',
            'pointing_status': 'invalid_name', })
    try:
        log.info(f'save_pointed_waypoint requested: {normalized_name}')
        request = SavePointedWaypoint.Request()
        request.name = normalized_name
        request.max_pointing_age_seconds = _POINTING_HYSTERESIS_SECONDS
        response = call_service('/gesture_detection/save_pointed_waypoint', SavePointedWaypoint, request, timeout_sec=6.0, )
        if response is None:
            return tool_result(ToolStatus.FAILURE, error={
                'code': 'GESTURE_DETECTOR_UNAVAILABLE',
                'message': ('The gesture detector did not respond; no waypoint was '
                            'saved.'),
                'pointing_status': 'unavailable', })
        outcome = str(response.outcome or 'unknown')
        message = str(response.message or response_message(response, 'The pointed waypoint could not be saved.'))
        if response_status(response) == STATUS_SUCCESS:
            waypoint = response.waypoint
            return tool_result(ToolStatus.SUCCESS, result={
                'pointing_status': outcome,
                'message': message,
                'name': waypoint.name or normalized_name.lower(),
                'x': float(waypoint.x),
                'y': float(waypoint.y),
                'frame_id': waypoint.frame_id or 'map',
                'wall_hit': bool(response.wall_hit),
                'used_hysteresis': bool(response.used_hysteresis),
                'pointing_age_seconds': round(float(response.pointing_age_seconds), 3), })
        error_codes = {
            'no_person': 'NO_PERSON',
            'multiple_people': 'MULTIPLE_PEOPLE',
            'no_pointing': 'NO_POINTING',
            'expired_hysteresis': 'EXPIRED_HYSTERESIS',
            'map_unavailable': 'MAP_UNAVAILABLE',
            'navigation_unavailable': 'NAVIGATION_UNAVAILABLE',
            'unreachable': 'POINTED_LOCATION_UNREACHABLE',
            'save_failed': 'SAVE_WAYPOINT_FAILED',
            'invalid_name': 'INVALID_WAYPOINT_NAME', }
        return tool_result(ToolStatus.FAILURE, error={
            'code': error_codes.get(outcome, 'POINTED_WAYPOINT_FAILED'),
            'message': message,
            'pointing_status': outcome,
            'pointing_age_seconds': round(float(response.pointing_age_seconds), 3), })
    except Exception as exc:
        return tool_result(ToolStatus.FAILURE, error={
            'code': 'POINTED_WAYPOINT_ERROR',
            'message': str(exc),
            'pointing_status': 'error', })


@tool_with_meta(announcement='I\'ve deleted the location {name}.', wait_duration=1.0, requires=[{
    'state': 'waypoints',
    'provider': 'get_waypoints',
    'validate': {'arg': 'name', 'in': 'result.waypoints[]'}}], invalidates=['waypoints'], )
def delete_waypoint(name: str) -> dict:
    """
    Delete a saved waypoint by its exact name. Permanent.
    Use the exact saved waypoint name. Do not guess or invent names.
    """
    try:
        log.info(f'delete_waypoint requested: {name}')
        request = DeleteWaypoint.Request()
        request.name = name.lower()
        response = call_service('/waypoint/delete', DeleteWaypoint, request, timeout_sec=5.0)
        if response_status(response) == STATUS_SUCCESS:
            deleted_name = response.name or name.lower()
            return tool_result(ToolStatus.SUCCESS, result={'message': response_message(response), 'name': deleted_name})
        message = response_message(response, 'Could not delete waypoint')
        return tool_result(ToolStatus.FAILURE, error={'code': 'DELETE_FAILED', 'message': message})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})

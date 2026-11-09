import json
import math
import re
import time
import uuid
from typing import Optional

from mcp_server.config import config as mcp_config
from mcp_server.decorators import ToolStatus, tool_result, tool_with_meta
from mcp_server.ros_utils import (STATUS_STOPPED, STATUS_SUCCESS, STATUS_TIMEOUT, call_service, call_trigger_service, response_message, response_status)
from mcp_server.tools import navigation as navigation_tools
from workstation_common import get_logger

from action_interfaces.srv import ExecuteAction

log = get_logger('mcp.manipulation')


def _read_object_state():
    request = ExecuteAction.Request()
    request.action_type = 'read'
    response = call_service('/k2/object_state', ExecuteAction, request, timeout_sec=2.)
    if response_status(response) != STATUS_SUCCESS:
        return None
    try:
        state = json.loads(response.result_data)
        return state if isinstance(state, dict) else None
    except (TypeError, ValueError, AttributeError):
        return None


def _require_object_state(expected):
    state = _read_object_state()
    if state is None or state.get('possession') != expected:
        return tool_result(ToolStatus.FAILURE, result=state or {}, error={
            'code': 'OBJECT_STATE_INVALID', 'retryable': False, 'execution_uncertain': False,
            'message': ('The gripper must be confirmed empty before pickup' if expected == 'empty' else
                        'No held item is recorded; provide a current gripper-state correction')})
    return None


@tool_with_meta(returns_data=True, wait_duration=2.)
def get_transport_state() -> dict:
    """Read the robot's object state: empty, holding_assumed or unknown.

    holding_item is true for held possession, false for empty, null for unknown.
    holding_assumed records a completed pickup or an explicit human correction.
    It is not sensor proof. This tool always reads live robot state and cannot
    change or reset it; do not answer possession questions from old LLM history.
    """
    state = _read_object_state()
    if state is None:
        return tool_result(ToolStatus.FAILURE, error={'code': 'OBJECT_STATE_UNAVAILABLE',
            'message': 'Robot object state is unavailable', 'retryable': False})
    return tool_result(ToolStatus.SUCCESS, result=state)


def _base_label(name: str) -> str:
    raw = (name or '').strip().lower()
    if '_' in raw:
        prefix, suffix = raw.rsplit('_', 1)
        if suffix.isdigit():
            return prefix
    return raw


def _pick_surface_candidate(items, surface: str):
    wanted = (surface or '').strip().lower()
    matches = [obj for obj in items if _base_label(obj.get('item_name', '')) == wanted]
    if not matches:
        return None
    return min(matches, key=lambda obj: float(obj.get('distance', 999.0)))


def _run_execute_action(action_type: str, params: dict, timeout_sec: float, *, stopped_message: str, timeout_code: str, timeout_message: str, failure_code: str, failure_message: str, success_result: dict) -> dict:
    request = ExecuteAction.Request()
    request.action_type = action_type
    request.parameters = json.dumps(params)
    response = call_service('/execute_action', ExecuteAction, request, timeout_sec=timeout_sec)
    status = response_status(response)
    try:
        details = json.loads(getattr(response, 'result_data', '') or '{}')
        if not isinstance(details, dict):
            details = {}
    except (ValueError, TypeError):
        details = {}
    error_details = {'retryable': False,
        'execution_uncertain': details.get('execution_uncertain', status == STATUS_TIMEOUT)}
    if status == STATUS_STOPPED:
        return tool_result(ToolStatus.STOPPED, result=details, error={
            **error_details,
            'code': 'GLOBAL_STOP',
            'message': response_message(response, stopped_message)})
    if status == STATUS_TIMEOUT:
        return tool_result(ToolStatus.TIMEOUT, result=details, error={
            **error_details,
            'code': timeout_code,
            'message': response_message(response, timeout_message)})
    if status == STATUS_SUCCESS:
        if details.get('completion_confirmed') is not True or details.get('execution_uncertain'):
            return tool_result(ToolStatus.FAILURE, result=details, error={
                'code': 'COMPLETION_UNCONFIRMED', 'message': 'Controller completion was not confirmed',
                'retryable': False, 'execution_uncertain': True})
        return tool_result(ToolStatus.SUCCESS, result={**success_result, **details})
    return tool_result(ToolStatus.FAILURE, result=details, error={
        **error_details,
        'code': details.get('error_code') or failure_code,
        'message': response_message(response, failure_message)})


def _forward_tool_failure(result: dict, fallback_code: str, fallback_message: str) -> dict:
    status = str(result.get('status') or ToolStatus.FAILURE.value)
    error = result.get('error') if isinstance(result.get('error'), dict) else {}
    return tool_result(status, error={
        **error,
        'code': error.get('code') or fallback_code,
        'message': error.get('message') or fallback_message,
        **({'retryable': error['retryable']} if 'retryable' in error else {}), })


def _prepare_for_person_handoff(person: Optional[str] = None, *, approach: bool = True) -> dict:
    requested_name = str(person or '').strip() or None
    if not approach:
        return tool_result(ToolStatus.SUCCESS, result={
            'approached': False, 'preparation': 'handover_in_place'})
    standoff_distance = float(mcp_config.handoff.standoff_m)
    approach_result = navigation_tools.drive_to_person(distance=standoff_distance, person=requested_name)
    if approach_result.get('status') != ToolStatus.SUCCESS.value:
        return _forward_tool_failure(approach_result, 'PERSON_APPROACH_FAILED', 'Could not approach the person')
    result = dict(approach_result.get('result') or {})
    result.setdefault('approached', True)
    result['preparation'] = 'drive_to_person'
    resolved_name = str(result.get('person_name') or requested_name or '').strip()
    if requested_name:
        result.update({
            'recipient': resolved_name,
            'target_verified': result.get('target_verified', True),
            'verification_source': (result.get('verification_source') or 'drive_to_person'), })
    return tool_result(ToolStatus.SUCCESS, result=result)


def _valid_handoff_timeout(value):
    try:
        value = float(value)
        return math.isfinite(value) and 1.0 <= value <= 60.0
    except (ValueError, TypeError):
        return False


def _handoff_recipient_parameters(requested_name):
    # Selection belongs to the approach; names/YOLO IDs never gate transfer.
    return {'person': requested_name, 'recipient_policy': 'gesture_only'}


@tool_with_meta(announcement='I\'ve grabbed the {item}.', wait_duration=30.0, workflow_before=['position_for_action'], invalidates=[
    'scene_objects',
    'visible_objects'], )
def grab_item(item: str) -> dict:
    """
    Grab a nearby object.
    First drive close to the object and orient yourself using position_for_action.
    `item` is the base label without the `_N` suffix, like `bottle`.
    """
    try:
        log.info(f'Grab requested for {item}')
        return _run_execute_action('grab_item', {
            'item': item}, 120.0, stopped_message='Grab stopped', timeout_code='GRAB_TIMEOUT', timeout_message='Grab timed out', failure_code='GRAB_FAILED', failure_message='Unknown error', success_result={
            'message': f'Successfully grabbed {item}',
            'item': item}, )
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(announcement='The gripper close finished. I am assuming I hold the item.', wait_duration=240.0, invalidates=[
    'scene_objects',
    'visible_objects'], )
def take_from_hand(person: Optional[str] = None, timeout_seconds: float = 45.0, item: Optional[str] = None, approach: bool = True) -> dict:
    """
    Approach a person and take an object directly from their hand as one
    uninterrupted workflow.
    Use when the user asks the robot to take, hold, or grab an object they are handing over.
    In study mode, `item` must match the selected approved test object. If omitted,
    the selected trial object is assumed; staff check the actual object.
    Set `person` only when the user explicitly names the giver. Without an
    explicit name, omit it to select the nearest visible person, including
    "from my hand". Speaker attribution alone is not a target name. Additional
    visible people are allowed. The tool owns its preparation, so do
    not call perception, drive_to_person, or position_for_action separately.
    Select the person once using a current detection, a navigation-eligible
    last known position, or one search. After the approach, do not recheck the
    face, body, or track ID. A fresh thumbs-up in the handoff camera view
    confirms the transfer after the arm is ready.
    Speech recognition can confuse "hand" with "sand". When a request combines
    approaching a person with receiving an item "from the sand", this is the
    handover tool unless the user clearly means literal ground or sand.
    The user must place the object between the open gripper fingers and show a thumbs-up gesture to the head camera.
    This action turns the base and head camera toward the handoff pose, then restores both after the pickup.
    Set approach=False only for an explicitly requested handover at the current
    location with the giver already present. This skips person lookup, search
    and driving; the normal handoff turn, arm motion and gesture remain active.
    Do not use this for grabbing named visible objects from a table or floor; use grab_item for that.
    """
    try:
        if not _valid_handoff_timeout(timeout_seconds):
            return tool_result(ToolStatus.FAILURE, error={'code': 'INVALID_TIMEOUT',
                'message': 'Handover wait must be between 1 and 60 seconds', 'retryable': False})
        state_error = _require_object_state('empty')
        if state_error:
            return state_error
        requested_name = str(person or '').strip() or None
        log.info(f'Hand pickup requested from '
                 f'{requested_name or "nearest person"} with '
                 f'timeout={timeout_seconds}')
        preparation = (_prepare_for_person_handoff(requested_name) if approach else
                       _prepare_for_person_handoff(requested_name, approach=False))
        if preparation.get('status') != ToolStatus.SUCCESS.value:
            return preparation
        preparation_result = dict(preparation.get('result') or {})
        return _run_execute_action('take_from_hand', {
            'timeout_seconds': float(timeout_seconds), **_handoff_recipient_parameters(requested_name), **({'item': item} if item is not None else {})}, max(30.0, float(timeout_seconds) + 120.0), stopped_message='Hand pickup stopped', timeout_code='HAND_PICKUP_TIMEOUT', timeout_message='Hand pickup timed out', failure_code='HAND_PICKUP_FAILED', failure_message='Unknown error', success_result={
            **preparation_result, 'message': 'Gripper close completed; possession is assumed, not sensor-verified', }, )
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(announcement='I have handed the item to the person.', wait_duration=240.0, invalidates=['scene_objects',
    'visible_objects'], )
def give_to_person(person: Optional[str] = None, timeout_seconds: float = 45.0, approach: bool = True) -> dict:
    """
    Hand the currently held item to a person.
    Use when the user asks the robot to give, pass, or hand an item to someone.
    Set `person` to the requested person's recognized face name, for example `TestUserB`.
    Set `person` only for an explicitly named recipient. Otherwise select the
    nearest visible person, including "give it to me"; speaker attribution
    alone is not a target name. Additional visible people are allowed.
    A named request controls the initial approach, including an eligible last
    known position. After arrival, do not re-identify or select another person.
    Uses the same front-facing drive_to_person preparation as take_from_hand,
    then performs its own side-on handoff turn. It stays in place when already
    within handoff range and facing the recipient.
    The transfer uses a fresh thumbs-up in the handoff camera view without
    requiring a face, body detection, or stable track ID.
    For "take this item and bring it to TestUserB", call take_from_hand first, then give_to_person with person="TestUserB".
    The robot presents the item while keeping its gripper closed, then waits for a fresh thumbs-up gesture seen by the head camera before releasing it.
    If no gesture arrives before the timeout, the robot retracts while retaining the item.
    Set approach=False only for an explicitly requested handover at the current
    location with the recipient already present. This skips person lookup,
    search and driving; the normal handoff turn and gesture remain active.
    Do not use this to drop an item or place it on a surface; use release_item or place_item instead.
    """
    try:
        if not _valid_handoff_timeout(timeout_seconds):
            return tool_result(ToolStatus.FAILURE, error={'code': 'INVALID_TIMEOUT',
                'message': 'Handover wait must be between 1 and 60 seconds', 'retryable': False})
        state_error = _require_object_state('holding_assumed')
        if state_error:
            return state_error
        requested_name = str(person or '').strip() or None
        log.info(f'Person handoff requested for '
                 f'{requested_name or "nearest person"} with '
                 f'timeout={timeout_seconds}')
        preparation = (_prepare_for_person_handoff(requested_name) if approach else
                       _prepare_for_person_handoff(requested_name, approach=False))
        if preparation.get('status') != ToolStatus.SUCCESS.value:
            return preparation
        preparation_result = dict(preparation.get('result') or {})
        return _run_execute_action('give_to_person', {
            'timeout_seconds': float(timeout_seconds), **_handoff_recipient_parameters(requested_name)}, max(30.0, float(timeout_seconds) + 120.0), stopped_message='Item handoff stopped', timeout_code='GIVE_TO_PERSON_TIMEOUT', timeout_message='Item handoff timed out; release not requested', failure_code='GIVE_TO_PERSON_FAILED', failure_message='Unknown error', success_result={
            **{key:value for key,value in preparation_result.items() if key not in ('recipient','target_verified')},
            'approach_person':requested_name, 'recipient_policy':'gesture_only',
            'identity_continuity_verified':False, 'message': 'Item handoff completed after gesture confirmation', }, )
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(announcement='I\'ve released the item and stowed the arm.', wait_duration=30.0, )
def release_item() -> dict:
    """
    Release the held object.
    Opens the gripper, then stows the arm automatically.
    Use when the user wants to let go of the currently held item without a controlled placement.
    For disposal into a trash can, use put_item_in_trash, which locates the bin and positions the arm.
    """
    try:
        log.info('Release requested')
        return _run_execute_action('release_item', {}, 30.0, stopped_message='Release stopped',
            timeout_code='RELEASE_TIMEOUT', timeout_message='Release and stow timed out',
            failure_code='RELEASE_FAILED', failure_message='Release or resting posture failed',
            success_result={'message': 'Item released and empty gripper stowed'})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


def _arm_service_failure(operation, response):
    """Keep known driver refusals distinct from an unconfirmed physical RPC.

    A runstop refusal is a completed service call that did not start motion.
    Losing a service response, even one mentioning runstop, is not that proof.
    """
    valid_response = isinstance(response, dict)
    response = response if valid_response else {}
    message = str(response.get('message') or (
        'Arm calibration failed' if operation == 'home' else 'Arm stow failed'))
    code = 'CALIBRATION_FAILED' if operation == 'home' else 'STOW_FAILED'
    error = {'code': code, 'message': message, 'retryable': False,
             'execution_uncertain': bool(response.get('execution_uncertain', not valid_response))}
    if message.startswith(('ARM_OPERATION_INTERRUPTED:', 'ARM_OPERATION_FAILED:')):
        error.update(code=message.split(':', 1)[0], execution_started=True, execution_uncertain=True)
    if not error['execution_uncertain']:
        refused_mode = re.fullmatch(
            rf'Cannot {operation} while in mode=([a-z_]+)\.?', message.strip())
        if message.startswith('DRIVER_BUSY:'):
            error.update(code='DRIVER_BUSY', execution_started=False, completion_confirmed=True)
        elif refused_mode:
            mode = refused_mode.group(1)
            error['code'] = ('RUNSTOP_ACTIVE' if mode == 'runstopped' else
                             'DRIVER_BUSY' if mode in {'homing', 'stowing'} else 'DRIVER_MODE_UNAVAILABLE')
            error.update(driver_mode=mode, execution_started=False, completion_confirmed=True)
        elif message.strip().lower().rstrip('.') in {
                'robot not homed', 'robot is not homed',
                'cannot stow: robot not homed', 'cannot stow: robot is not homed'}:
            error.update(code='ROBOT_NOT_HOMED', execution_started=False, completion_confirmed=True)
        elif message == f'Service /{operation}_the_robot not available':
            error.update(code='ARM_SERVICE_UNAVAILABLE', execution_started=False, completion_confirmed=True)
    return tool_result(ToolStatus.FAILURE, error=error)


@tool_with_meta(announcement='I\'ve calibrated my arm.', wait_duration=60.0, )
def calibrate_arm() -> dict:
    """
    Calibrate and home the arm.
    Use only when the user explicitly asks to calibrate or home the arm.
    This is slow and physically disruptive because the arm moves through a wide range.
    """
    calibration_completed = False
    try:
        log.info('Arm calibration requested')
        started = time.time()
        result = call_trigger_service('/home_the_robot', timeout_sec=60.0)
        if result and result.get('success'):
            calibration_completed = True
            request = ExecuteAction.Request()
            request.action_type = 'calibration_completed'
            request.parameters = json.dumps({'_started_at': started, '_issued_at': time.time(),
                '_request_id': uuid.uuid4().hex})
            response = call_service('/k2/object_state', ExecuteAction, request, timeout_sec=2.)
            try:
                state = json.loads(getattr(response, 'result_data', '') or '{}')
                if not isinstance(state, dict): state = {}
            except (TypeError, ValueError):
                state = {}
            if (response_status(response) == STATUS_SUCCESS and state.get('calibration_completed') is True and
                    (state.get('calibration_state_reset') is True or state.get('newer_state_preserved') is True)):
                return tool_result(ToolStatus.SUCCESS, result={**state, 'message': 'Arm calibration complete'})
            return tool_result(ToolStatus.FAILURE, result={'calibration_completed': True,
                'calibration_state_reset': False,
                'calibration_report_message': response_message(response, 'No valid calibration state response')},
                error={'code': 'CALIBRATION_STATE_SYNC_FAILED',
                'message': 'Arm calibrated, but gripper-state update was not confirmed: ' +
                    response_message(response, 'No valid calibration state response'),
                'retryable': False, 'execution_uncertain': False})
        return _arm_service_failure('home', result)
    except Exception as e:
        if calibration_completed:
            return tool_result(ToolStatus.FAILURE, result={'calibration_completed': True,
                'calibration_state_reset': False}, error={'code': 'CALIBRATION_STATE_SYNC_FAILED',
                'message': str(e), 'retryable': False, 'execution_uncertain': False})
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(announcement='I\'ve stowed my arm.', wait_duration=30.0, )
def stow_arm() -> dict:
    """
    Stow the arm into travel position.
    Use when the user asks to stow the arm, or when compact travel posture is needed before motion.
    """
    try:
        log.info('Arm stow requested')
        result = call_trigger_service('/stow_the_robot', timeout_sec=30.0)
        if result and result.get('success'):
            return tool_result(ToolStatus.SUCCESS, result={'message': 'Arm stowed'})
        return _arm_service_failure('stow', result)
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(wait_duration=20.0)
def wave_hand(height: Optional[float] = None) -> dict:
    """
    Wave the robot's hand/gripper once as a social gesture.
    Use when the user says phrases like "wave your hand", "say hi with your hand",
    "wave hello", or asks the robot to perform a simple hand waving gesture.
    The action raises the arm to a visible height and performs one fixed waving gesture.
    Call this tool only once per user request. If the robot is already waving, do not call it again.
    """
    try:
        log.info(f'Wave requested height={height}')
        params = {}
        if height is not None:
            params['height'] = height
        return _run_execute_action('wave_hand', params, 25.0, stopped_message='Wave stopped', timeout_code='WAVE_TIMEOUT', timeout_message='Wave timed out', failure_code='WAVE_FAILED', failure_message='Unknown error', success_result={
            'message': 'Successfully waved hand'}, )
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(wait_duration=240.0, invalidates=['scene_objects', 'visible_objects'])
def put_item_in_trash() -> dict:
    """Put the currently held item into a nearby trash can (Mülleimer).

    Use for putting, dropping, throwing away or disposing of a held object:
    "in den Müll legen/werfen", "entsorgen", "put/drop/throw it in the trash/bin".
    If travel is needed, first drive to the saved trash-can waypoint. If already
    positioned there, call this tool directly, including after a user correction.
    This tool locates the configured ArUco marker, approaches to arm reach and
    turns left to point the gripper at the bin, then freshly localizes, releases the
    item and stows the arm. Do not call get_look_around_objects, get_visible_objects
    or position_for_action as preparation. No external detection/coordinates are needed.
    This does not pick up an item or navigate to a distant bin. If the local bin
    cannot be located, the action keeps the gripper closed and reports the problem.
    """
    try:
        state_error = _require_object_state('holding_assumed')
        if state_error:
            return state_error
        localized = _run_execute_action('localize_trash', {}, 40.0,
            stopped_message='Trash localization stopped', timeout_code='TRASH_LOCALIZATION_TIMEOUT',
            timeout_message='Trash localization timed out', failure_code='TRASH_TARGET_NOT_FOUND',
            failure_message='Could not localize the configured trash marker', success_result={})
        if localized.get('status') != ToolStatus.SUCCESS.value:
            return localized
        target = localized.get('result') or {}
        if target.get('positioned') is not True:
            approach = navigation_tools.position_for_action(target='trash_can',
                x=target['x'], y=target['y'], distance=target['approach_distance'])
            if approach.get('status') != ToolStatus.SUCCESS.value:
                return approach
        # These coordinates only aim the post-approach camera search. Placement
        # still requires a new observation of the configured marker before release.
        return _run_execute_action('place_item', {'surface': 'trash_can',
            'original_x': target['x'], 'original_y': target['y'], 'z': target['z']}, 90.0,
            stopped_message='Trash placement stopped', timeout_code='PLACE_TIMEOUT',
            timeout_message='Trash placement timed out', failure_code='PLACE_FAILED',
            failure_message='Trash placement failed', success_result={
                'surface': 'trash_can', 'message': 'Released the held item at the localized trash can'})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})


@tool_with_meta(announcement="I've placed the held item in the {surface}.", wait_duration=90.0, workflow_before=[
    'position_for_action'], invalidates=['scene_objects', 'visible_objects'], )
def place_item(surface: str = 'trash_can', height: Optional[float] = None, original_x: Optional[
    float] = None, original_y: Optional[float] = None, original_z: Optional[float] = None, ) -> dict:
    """
    Place the held object on a nearby surface.
    Use for controlled surface placement, such as on a table or shelf.
    `surface` is the base label without the `_N` suffix, like `table`.
    For disposal into a trash can use put_item_in_trash, which owns local bin
    detection and alignment; do not add a separate scan or positioning action.
    Legacy surface='trash_can' calls remain supported by the same robot routine.
    When available, pass `original_x`, `original_y`, and `original_z` from the scan result.
    `height` is optional. If `original_z` is available, it overrides `height`.
    """
    try:
        effective_z = original_z if original_z is not None else height
        log.info(f'Placement requested on {surface} with height={height} effective_z={effective_z}')
        params = {'surface': surface}
        if effective_z is not None:
            params['z'] = effective_z
        if original_x is not None:
            params['original_x'] = original_x
        if original_y is not None:
            params['original_y'] = original_y
        return _run_execute_action('place_item', params, 60.0, stopped_message='Placement stopped', timeout_code='PLACE_TIMEOUT', timeout_message='Placement timed out', failure_code='PLACE_FAILED', failure_message='Unknown error', success_result={
            'message': f'Successfully placed item on {surface}',
            'surface': surface}, )
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e)})

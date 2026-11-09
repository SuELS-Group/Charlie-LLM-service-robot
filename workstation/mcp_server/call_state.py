"""Carry RPC uncertainty through nested synchronous MCP tool wrappers."""
from contextlib import contextmanager
from contextvars import ContextVar
import time
from workstation_common.language import supported_language

_faults = ContextVar('ros_call_faults', default=None)
_issued_at = ContextVar('robot_request_time', default=0.0)
_language = ContextVar('robot_request_language', default='en')
_request_id = ContextVar('robot_request_id', default='')
_deadline = ContextVar('robot_operation_deadline', default=None)
_root_tool = ContextVar('robot_root_tool', default='')


@contextmanager
def tool_context(tool):
    token = _root_tool.set(_root_tool.get() or tool)
    try:
        yield
    finally:
        _root_tool.reset(token)


def request_tool():
    return _root_tool.get()

# One budget for the whole workflow, including nested perception/navigation.
OPERATION_BUDGETS = {'move_to': 120., 'move_to_waypoint': 120., 'move_relative': 60.,
    'turn_to': 30., 'turn_to_relative': 30., 'position_for_action': 120.,
    'drive_to_person': 90., 'take_from_hand': 240., 'give_to_person': 240.,
    'put_item_in_trash': 240.}


@contextmanager
def operation_budget(tool):
    previous = _deadline.get()
    seconds = OPERATION_BUDGETS.get(tool)
    deadline = time.monotonic() + seconds if seconds else previous
    if previous is not None:
        deadline = min(previous, deadline) if deadline is not None else previous
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining_budget(seconds):
    deadline = _deadline.get()
    return max(0., min(float(seconds), deadline - time.monotonic())) if deadline is not None else float(seconds)


@contextmanager
def capture_call_faults(issued_at=None, language=None, request_id=None):
    existing = _faults.get()
    if existing is not None:
        yield existing
        return
    faults = []
    token = _faults.set(faults)
    time_token = _issued_at.set(float(issued_at) if issued_at else time.time())
    language_token = _language.set(supported_language(language))
    request_token = _request_id.set(str(request_id or ''))
    try:
        yield faults
    finally:
        _faults.reset(token)
        _issued_at.reset(time_token)
        _language.reset(language_token)
        _request_id.reset(request_token)


def request_issue_time():
    return _issued_at.get() or time.time()


def request_language():
    return _language.get()


def request_id():
    return _request_id.get()


def record_call_fault(details):
    faults = _faults.get()
    if faults is not None:
        faults.append(details)


def unresolved_call():
    return any(fault.get('execution_uncertain') for fault in (_faults.get() or []))

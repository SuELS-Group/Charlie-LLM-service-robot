"""Optional local trial events. Never serialize prompts, people or raw results."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
import uuid

from workstation_common import get_logger

_lock = threading.Lock()
log = get_logger('mcp.k2.events')
TOOLS = frozenset({'take_from_hand', 'give_to_person', 'drive_to_person', 'move_to_waypoint',
    'move_to', 'move_relative', 'turn_to', 'turn_to_relative', 'grab_item', 'place_item',
    'release_item', 'stow_arm', 'get_transport_state', 'get_study_profile', 'report_empty_gripper',
    'report_holding_gripper'})
BOOLEAN_FIELDS = frozenset({'completion_confirmed', 'execution_uncertain', 'travel_ready',
    'arm_retracted', 'orientation_restored', 'navigation_mode_restored', 'gripper_opened',
    'gripper_close_accepted', 'gripper_goal_settled', 'gripper_position_reached', 'gripper_contact_indicated',
    'human_report_recorded', 'holding_item', 'handoff_motion_started', 'arm_motion_started'})


def record(tool, root, request_id, started, result, exception=False):
    destination = os.environ.get('ROBOT_K2_EVENT_LOG')
    if not destination or tool not in TOOLS:
        return
    try:
        try:
            request_id = str(uuid.UUID(str(request_id)))
        except (ValueError, TypeError, AttributeError):
            request_id = ''
        result = result if isinstance(result, dict) else {}
        details = result.get('result') if isinstance(result.get('result'), dict) else {}
        error = result.get('error') if isinstance(result.get('error'), dict) else {}
        facts = {key: details[key] for key in BOOLEAN_FIELDS if isinstance(details.get(key), bool)}
        if isinstance(error.get('execution_uncertain'), bool):
            facts['execution_uncertain'] = error['execution_uncertain']
        if details.get('possession') in {'empty', 'holding_assumed', 'unknown'}:
            facts['possession'] = details['possession']
        event = {'event_id': str(uuid.uuid4()), 'request_id': request_id,
            'timestamp_utc': datetime.now(timezone.utc).isoformat(), 'tool': tool,
            'root_tool': root if root in TOOLS else tool, 'status': 'exception' if exception else result.get('status', 'unknown'),
            'duration_s': round(max(0., time.monotonic() - started), 6),
            'error_code': str(error.get('code', ''))[:80], 'facts': facts}
        path = Path(destination).expanduser()
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(event, ensure_ascii=True) + '\n')
    except (OSError, TypeError, ValueError) as exc:
        # A log write must never start/retry/cancel a physical action.
        log.error(f'Could not write local K2 event: {type(exc).__name__}')

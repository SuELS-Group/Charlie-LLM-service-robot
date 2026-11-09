from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Dict, Optional, TYPE_CHECKING

import httpx

from models import ExecutionStatus, ToolCall, ToolResult
from workstation_common import get_logger
from workstation_common.tool_safety import needs_robot_state
from workstation_common.runtime_messages import result_failure_message, runtime_message

if TYPE_CHECKING:
    from mcp_client import MCPClient
    from user_context import UserContext
log = get_logger('llm.executor')

# These operations retain their controller goals until termination. Driver
# homing/stow services and arbitrary tools do not provide that same evidence.
RECHECKABLE_STOP_TOOLS = frozenset({'take_from_hand', 'give_to_person', 'move_to_waypoint',
    'drive_to_person', 'turn_to', 'turn_to_relative', 'move_relative',
    'grab_item', 'release_item', 'place_item', 'put_item_in_trash'})
TRACKED_OBJECT_TOOLS = frozenset({'take_from_hand', 'give_to_person', 'grab_item', 'release_item', 'place_item', 'put_item_in_trash'})
ARM_RECOVERY_TOOLS = frozenset({'calibrate_arm', 'stow_arm'})


class ToolExecutor:

    def __init__(self, mcp_client: 'MCPClient', user_context: Optional['UserContext'] = None, recovery_path=None):
        self.mcp = mcp_client
        self.user_context = user_context
        self.failure_counts: Dict[str, int] = {}
        self.recovery_path = Path(recovery_path).expanduser() if recovery_path else None
        self.execution_context = None
        self.recovery_cause = None
        self.recovery_check_pending = False
        self.recovery_check_error = None
        try:
            self.recovery_required = bool(self.recovery_path and self.recovery_path.exists())
            if self.recovery_required:
                record = json.loads(self.recovery_path.read_text())
                if (isinstance(record, dict) and type(record.get('version')) is int and record['version'] == 1 and
                        all(isinstance(record.get(key), str) for key in ('tool', 'status', 'request_id'))):
                    self.recovery_cause = record
        except (ValueError, TypeError):
            pass  # Legacy/invalid markers still require an explicit operator check.
        except OSError:
            self.recovery_required = True
        self._meta: Dict[str, Dict[str, Any]] = {}
        for tool in getattr(mcp_client, 'llm_callable_tools', []) or []:
            self._meta[tool.get('name', '')] = tool.get('meta', {}) or {}

    def set_user_context(self, user_context: 'UserContext') -> None:
        self.user_context = user_context

    def reset_failures(self) -> None:
        self.failure_counts.clear()

    def acknowledge_recovery(self):
        """Operator acknowledgement after checking the actual robot state."""
        try:
            if self.recovery_path:
                self.recovery_path.unlink(missing_ok=True)
        except OSError as exc:
            log.error(f'Could not clear recovery marker: {exc}')
            return False
        self.recovery_required = False
        self.recovery_cause = None
        return True

    async def revalidate_after_empty_report(self):
        """Compatibility entry point for the original empty-only input route."""
        return await self.revalidate_after_possession_report()

    async def revalidate_after_possession_report(self):
        """A human correction establishes possession, never motion completion."""
        context = self.execution_context
        last = context.previous_results[-1] if context is not None and context.previous_results else None
        data = last.result.get('result') if last is not None and isinstance(last.result, dict) else {}
        expected = {'report_empty_gripper': ('empty', 'explicit_user_empty_report'),
                    'report_holding_gripper': ('holding_assumed', 'explicit_user_holding_report')}
        wanted = expected.get(last.tool_name) if last is not None else None
        if (last is None or not last.success or wanted is None or
                not isinstance(data, dict) or data.get('human_report_recorded') is not True or
                (data.get('possession'), data.get('possession_source')) != wanted):
            return False
        return await self._refresh_after_stop(after_human_report=True)

    @staticmethod
    def _recheckable_cause(cause):
        return bool(cause and cause.get('tool') in RECHECKABLE_STOP_TOOLS and cause.get('request_id') and
            (cause.get('status') == 'stopped' or (cause.get('tool') in TRACKED_OBJECT_TOOLS and
             cause.get('status') == 'failure' and cause.get('error_code') == 'ROBOT_STATE_UNCERTAIN')))

    async def _refresh_after_stop(self, *, for_maintenance=False, after_human_report=False):
        """Read-only revalidation for a new request; never replay the old action."""
        self.recovery_check_pending = False
        self.recovery_check_error = None
        cause, context = self.recovery_cause, self.execution_context
        if (not self._recheckable_cause(cause) or
                context is None or context.is_cancelled() or not context.request_id or
                not cause.get('request_id') or context.request_id == cause['request_id']):
            return False
        try:
            recorded, issued = float(cause['recorded_at']), float(cause['issued_at'])
            if not all(math.isfinite(value) for value in (recorded, issued, context.issued_at)) or not 0 < issued <= recorded < context.issued_at:
                return False
            raw = await asyncio.wait_for(self.mcp.call_tool('get_transport_state', {}), timeout=3.)
            if not isinstance(raw, dict) or raw.get('status') != 'success' or raw.get('error'):
                return False
            state = raw.get('result')
            if not isinstance(state, dict):
                return False
            checked, started, stopped = (float(state[key]) for key in ('checked_at', 'runtime_started_at', 'last_stop_at'))
            if (not all(math.isfinite(value) for value in (checked, started, stopped)) or
                    not 0 <= time.time() - checked <= 2.5 or checked < context.issued_at):
                return False
            driver = state.get('driver_state')
            if not self._fresh_driver_state(driver, checked):
                self.recovery_check_error = ('DRIVER_STATE_UNAVAILABLE', 'Fresh driver status is unavailable.')
                return False
            if driver['runstop_active']:
                self.recovery_check_error = ('RUNSTOP_ACTIVE', 'The hardware Runstop is active; release it and give a fresh request.')
                return False
            if (driver['mode'] not in {'position', 'navigation', 'trajectory'} or
                    driver['arm_service_active'] or driver['streaming_position_active'] or not driver['motion_idle']):
                self.recovery_check_pending = True
                return False
            same_runtime = 0 < started <= issued <= stopped <= checked
            # Restarted action workers AND driver cannot still execute the old
            # tracked stop. Require fresh quiet hardware, not just an empty lock
            # in a newly started action service. Unknown timeouts never qualify.
            restarted_after_stop = (cause['status'] == 'stopped' and
                cause['tool'] in TRACKED_OBJECT_TOOLS and
                recorded < started <= checked and
                recorded < driver['driver_started_at'] <= checked and context.issued_at <= checked and
                driver.get('stationary_for_seconds', 0.) >= .5)
            # A fresh maintenance request may repair the arm after an action
            # service restart as well. The checks below still require that all
            # owned work ended and that the live driver is idle, including when
            # that driver itself never restarted. Ordinary motion keeps the
            # stricter causal-stop check.
            if not (same_runtime or restarted_after_stop or for_maintenance or after_human_report):
                return False
            self.recovery_check_pending = (state.get('controller_work_finished') is False or
                                           state.get('driver_stop_acknowledged') is False)
            if (state.get('controller_work_finished') is not True or
                    state.get('driver_stop_acknowledged') is not True or state.get('joint_feedback_error') is not None):
                return False
            if self.execution_context is not context or context.is_cancelled() or self.recovery_cause is not cause:
                return False
            if state.get('possession') not in {'empty', 'holding_assumed'}:
                # Homing/stowing are how a user restores an unusable posture.
                # They need confirmed idle controllers, not an already repaired
                # object ledger. Keep the latch for ordinary actions meanwhile.
                if for_maintenance and state.get('possession') == 'unknown':
                    return True
                self.recovery_check_error = ('OBJECT_STATE_INVALID',
                    'Gripper state is unknown. Check whether the gripper is empty or holding an item and report it.')
                return False
            if state.get('recovery_ready') is not True:
                return False
            if self.acknowledge_recovery():
                log.info('Stop completion revalidated for a fresh request; previous action will not be replayed')
                return True
        except Exception as exc:
            log.warning(f'Could not revalidate stop completion: {exc}')
        return False

    @staticmethod
    def _fresh_driver_state(state, checked):
        if not isinstance(state, dict) or state.get('fresh') is not True:
            return False
        if any(type(state.get(key)) is not bool for key in (
                'runstop_active', 'arm_service_active', 'streaming_position_active', 'motion_idle')):
            return False
        for key in ('sampled_at', 'driver_started_at', 'stationary_for_seconds'):
            value = state.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return False
        return (isinstance(state.get('mode'), str) and
                0 < state['driver_started_at'] <= state['sampled_at'] and
                0 <= checked - state['sampled_at'] <= 1.5 and
                0 <= time.time() - state['sampled_at'] <= 2.5 and
                state['stationary_for_seconds'] >= 0.)

    async def execute_tool_call(self, call: ToolCall) -> ToolResult:
        if self.execution_context is not None and self.execution_context.is_cancelled():
            return ToolResult(call.name, ExecutionStatus.STOPPED, error='Request cancelled before dispatch', args=call.args or {})
        if self.recovery_required and needs_robot_state(call.name) and call.name != 'stop_following':
            if not await self._refresh_after_stop(for_maintenance=call.name in ARM_RECOVERY_TOOLS):
                if self.recovery_check_error:
                    code, message = self.recovery_check_error
                    return ToolResult(call.name, ExecutionStatus.FAILURE, error=message,
                        error_details={'code': code, 'retryable': False, 'execution_started': False,
                                       'execution_uncertain': False, 'recovery_required': True}, args=call.args or {})
                return ToolResult(call.name, ExecutionStatus.FAILURE,
                    error=('Previous controller work is still finishing; wait and give a fresh request.'
                           if self.recovery_check_pending else
                           'Robot state is not yet confirmed. Check controller status and report the gripper state.'),
                    error_details={'code': 'RECOVERY_REQUIRED', 'retryable': False,
                                   'execution_started': False, 'execution_uncertain': False,
                                   'recovery_check_pending': self.recovery_check_pending,
                                   'recovery_required': True}, args=call.args or {})
        meta = self._meta.get(call.name, {})
        has_data = bool(meta.get('returns_data'))
        log.info(f'Calling tool {call.name} args={call.args}')
        if self.user_context is not None:
            self.user_context.announce_action_start(call)
        if self.execution_context is not None and self.execution_context.is_cancelled():
            return ToolResult(call.name, ExecutionStatus.STOPPED, error='Request cancelled before dispatch', args=call.args or {})
        try:
            raw = await self.mcp.call_tool(call.name, call.args or {})
        except Exception as exc:
            self.failure_counts[call.name] = self.failure_counts.get(call.name, 0) + 1
            log.warning(f'Tool {call.name} raised: {exc}')
            timed_out = isinstance(exc, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException))
            result = ToolResult(tool_name=call.name,
                status=ExecutionStatus.TIMEOUT if timed_out else ExecutionStatus.FAILURE,
                error=str(exc), error_details={
                    'code': 'TRANSPORT_TIMEOUT' if timed_out else 'TRANSPORT_ERROR',
                    'exception_type': type(exc).__name__, 'retryable': False,
                    'execution_uncertain': True}, has_return_data=has_data, args=call.args or {})
            self._record_uncertainty(call, result)
            return result
        result = self._parse(call, raw, has_data)
        self._record_uncertainty(call, result)
        if self.recovery_required and call.name == 'calibrate_arm' and result.success:
            state = raw.get('result') if isinstance(raw, dict) else None
            if (isinstance(state, dict) and state.get('calibration_completed') is True and
                    (state.get('calibration_state_reset') is True or state.get('newer_state_preserved') is True) and
                    state.get('possession') in {'empty', 'holding_assumed'}):
                # Successful homing and its acknowledged ledger update resolve
                # both the old motion and unknown possession, even before the
                # driver's idle debounce expires for the next stow step.
                self.acknowledge_recovery()
        if result.success:
            self.failure_counts[call.name] = 0
        else:
            self.failure_counts[call.name] = self.failure_counts.get(call.name, 0) + 1
        log.info(f'Tool {call.name} finished status={result.status.value}')
        return result

    def _record_uncertainty(self, call, result):
        if needs_robot_state(call.name) and (result.error_details or {}).get('execution_uncertain'):
            was_required = self.recovery_required
            self.recovery_required = True
            if not was_required:
                context = self.execution_context
                self.recovery_cause = {'version': 1, 'tool': call.name, 'status': result.status.value,
                    'error_code': (result.error_details or {}).get('code', ''),
                    'request_id': getattr(context, 'request_id', ''),
                    'issued_at': getattr(context, 'issued_at', 0.), 'recorded_at': time.time()}
            if self.recovery_path and not was_required:
                temporary = None
                try:
                    self.recovery_path.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(mode='w', dir=self.recovery_path.parent,
                                                     prefix=self.recovery_path.name + '.', delete=False) as stream:
                        temporary = Path(stream.name)
                        json.dump(self.recovery_cause, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.recovery_path)
                    directory = os.open(self.recovery_path.parent, os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                except OSError as exc:
                    self.recovery_cause = None  # Failed persistence cannot enable automatic recovery.
                    log.error(f'Could not persist recovery marker; do not restart to bypass recovery: {exc}')
                finally:
                    if temporary is not None:
                        try:
                            temporary.unlink(missing_ok=True)
                        except OSError:
                            log.warning('Could not remove temporary recovery marker')
            node = getattr(self.user_context, 'llm_node', None)
            if node is not None and not was_required:
                language = getattr(self.user_context, 'user_language', 'en')
                message = result_failure_message(result, language) + ' ' + self.recovery_guidance(language)
                node.request_stop('uncertain_completion', announcement=message)

    def recovery_guidance(self, language):
        cause = self.recovery_cause or {}
        recheckable = self._recheckable_cause(cause)
        return runtime_message('recovery_recheck' if recheckable else 'recovery_required', language)

    def _parse(self, call: ToolCall, raw: Any, has_data: bool) -> ToolResult:
        if (not isinstance(raw, dict) or not isinstance(raw.get('status'), str) or raw['status'] not in {
                'success', 'failure', 'stopped', 'timeout', 'pending'}):
            return self._invalid_result(call, has_data)
        status = raw['status']
        error = raw.get('error')
        if status == 'success' and error:
            return self._invalid_result(call, has_data)
        error_message = self._error_message(error)
        announcement = None
        meta = raw.get('meta') if isinstance(raw.get('meta'), dict) else {}
        if status == 'success':
            announcement = meta.get('announcement')
            return ToolResult(tool_name=call.name, status=ExecutionStatus.SUCCESS, result=raw, has_return_data=has_data or raw.get('result') is not None, args=call.args or {}, announcement=announcement)
        details = dict(error) if isinstance(error, dict) else {}
        if status in {'timeout', 'pending'}:
            details['retryable'] = False
            details.setdefault('execution_uncertain', True)
        defaults = {'stopped': 'Execution stopped', 'timeout': 'Execution timed out',
                    'pending': 'Completion has not been confirmed', 'failure': f'{call.name} failed'}
        return ToolResult(tool_name=call.name, status=ExecutionStatus(status), result=raw,
            error=error_message or defaults[status], error_details=details or None,
            has_return_data=has_data or raw.get('result') is not None, args=call.args or {})

    @staticmethod
    def _invalid_result(call: ToolCall, has_data: bool) -> ToolResult:
        return ToolResult(tool_name=call.name, status=ExecutionStatus.FAILURE,
            error='The tool returned an invalid result; completion could not be confirmed.',
            error_details={'code': 'INVALID_TOOL_RESULT', 'retryable': False,
                           'execution_uncertain': True},
            has_return_data=has_data, args=call.args or {})

    @staticmethod
    def _error_message(error: Any) -> str:
        if isinstance(error, dict):
            return str(error.get('message') or error)
        return str(error or '')

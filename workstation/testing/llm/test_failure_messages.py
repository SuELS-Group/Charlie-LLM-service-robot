from models import ExecutionStatus, ToolCall, ToolResult
from types import SimpleNamespace
from tool_access import ToolExecutor
from workstation_common.runtime_messages import result_failure_message
import pytest


def test_partial_open_is_explained_without_claiming_receipt():
    result = ToolResult('give_to_person', ExecutionStatus.FAILURE,
        result={'result': {'gripper_opened': True, 'travel_ready': False}},
        error_details={'execution_uncertain': True})
    assert 'gripper opened' in result_failure_message(result)
    assert 'Greifer wurde geöffnet' in result_failure_message(result, 'de')


def test_unknown_completion_takes_priority_over_runstop_diagnosis():
    result = ToolResult('turn_to_relative', ExecutionStatus.FAILURE,
        error_details={'code': 'RUNSTOP_ACTIVE', 'execution_uncertain': True})
    assert 'nicht bestätigen' in result_failure_message(result, 'de')


def test_invalid_arguments_do_not_hide_uncertain_completion():
    result = ToolResult('give_to_person', ExecutionStatus.FAILURE,
        error_details={'code': 'INVALID_ARGUMENTS', 'execution_started': False, 'execution_uncertain': True})
    message = result_failure_message(result, 'de')
    assert 'nicht bestätigen' in message and 'nicht gestartet' not in message


@pytest.mark.asyncio
async def test_known_runstop_refusal_does_not_latch_recovery_for_a_new_arm_request(tmp_path):
    calls = []
    async def call(name, args):
        calls.append(name)
        if len(calls) == 1:
            return {'status': 'failure', 'error': {'code': 'RUNSTOP_ACTIVE',
                'execution_started': False, 'execution_uncertain': False}}
        return {'status': 'success', 'result': {}}
    marker = tmp_path / 'recovery-required'
    executor = ToolExecutor(SimpleNamespace(call_tool=call), recovery_path=marker)
    assert not (await executor.execute_tool_call(ToolCall('calibrate_arm'))).success
    assert not executor.recovery_required and not marker.exists()
    assert (await executor.execute_tool_call(ToolCall('calibrate_arm'))).success
    assert (await executor.execute_tool_call(ToolCall('stow_arm'))).success
    assert calls == ['calibrate_arm', 'calibrate_arm', 'stow_arm']

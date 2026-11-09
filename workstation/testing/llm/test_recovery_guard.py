from types import SimpleNamespace
import json
import time

import pytest
from models import ExecutionContext, ToolCall
from tool_access import ToolExecutor


@pytest.mark.asyncio
async def test_unknown_motion_blocks_later_motion_but_allows_questions():
    calls = []
    async def call(name, args):
        calls.append(name)
        if name == 'move_to_waypoint':
            raise TimeoutError('connection lost after send')
        return {'status': 'success', 'result': {'value': 1}}
    executor = ToolExecutor(SimpleNamespace(call_tool=call))
    await executor.execute_tool_call(ToolCall('move_to_waypoint'))
    executor.reset_failures()
    blocked = await executor.execute_tool_call(ToolCall('give_to_person'))
    assert blocked.error_details['code'] == 'RECOVERY_REQUIRED'
    assert blocked.error_details['execution_started'] is False
    assert blocked.error_details['execution_uncertain'] is False
    assert (await executor.execute_tool_call(ToolCall('current_time'))).success
    assert calls == ['move_to_waypoint', 'current_time']
    executor.acknowledge_recovery()
    assert (await executor.execute_tool_call(ToolCall('give_to_person'))).success


@pytest.mark.asyncio
async def test_read_only_timeout_does_not_block_robot_state():
    async def call(*_):
        raise TimeoutError()
    executor = ToolExecutor(SimpleNamespace(call_tool=call))
    await executor.execute_tool_call(ToolCall('query_knowledge_base'))
    assert not executor.recovery_required


@pytest.mark.asyncio
async def test_recovery_marker_survives_agent_restart(tmp_path):
    marker = tmp_path / 'recovery-required'
    async def call(*_):
        raise ConnectionError('lost reply')
    executor = ToolExecutor(SimpleNamespace(call_tool=call), recovery_path=marker)
    await executor.execute_tool_call(ToolCall('dock_robot'))
    replacement = ToolExecutor(SimpleNamespace(), recovery_path=marker)
    assert replacement.recovery_required
    assert replacement.acknowledge_recovery()
    assert not marker.exists()


def recheckable_executor(call, marker=None):
    now = time.time()
    executor = ToolExecutor(SimpleNamespace(call_tool=call), recovery_path=marker)
    executor.recovery_required = True
    executor.recovery_cause = {'version': 1, 'tool': 'take_from_hand', 'status': 'stopped',
        'request_id': 'old', 'issued_at': now - 20., 'recorded_at': now - 10.}
    executor.execution_context = ExecutionContext('New explicit request', request_id='fresh', issued_at=now)
    return executor


def ready_state():
    now = time.time()
    return {'recovery_ready': True, 'controller_work_finished': True, 'driver_stop_acknowledged': True,
        'joint_feedback_error': None, 'possession': 'empty', 'checked_at': now,
        'runtime_started_at': now - 100., 'last_stop_at': now - 15.,
        'driver_state': {'fresh': True, 'sampled_at': now, 'driver_started_at': now - 100.,
            'mode': 'navigation', 'runstop_active': False, 'arm_service_active': False,
            'streaming_position_active': False, 'motion_idle': True, 'stationary_for_seconds': 2.}}


@pytest.mark.asyncio
async def test_fresh_request_revalidates_terminal_stop_without_replaying_old_action(tmp_path):
    calls = []
    async def call(name, args):
        calls.append(name)
        return {'status': 'success', 'result': ready_state() if name == 'get_transport_state' else {}}
    marker = tmp_path / 'recovery-required'
    marker.touch()
    executor = recheckable_executor(call, marker)
    assert (await executor.execute_tool_call(ToolCall('stow_arm'))).success
    assert calls == ['get_transport_state', 'stow_arm']
    assert not executor.recovery_required and not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [
    'timeout',
    'disconnect',
    'same_request',
    'legacy_marker',
])
async def test_unsupported_uncertainty_cannot_be_automatically_cleared(fault):
    calls = []
    async def call(name, args):
        calls.append(name)
        return {'status': 'success', 'result': ready_state()}
    executor = recheckable_executor(call)
    if fault == 'timeout': executor.recovery_cause['status'] = 'timeout'
    if fault == 'disconnect': executor.recovery_cause['status'] = 'failure'
    if fault == 'driver_stow': executor.recovery_cause['tool'] = 'stow_arm'
    if fault == 'same_request': executor.execution_context.request_id = 'old'
    if fault == 'older_request': executor.execution_context.issued_at = time.time() - 11.
    if fault == 'legacy_marker': executor.recovery_cause = None
    if fault == 'missing_request_id': executor.recovery_cause['request_id'] = ''
    result = await executor.execute_tool_call(ToolCall('calibrate_arm'))
    assert result.error_details['code'] == 'RECOVERY_REQUIRED' and executor.recovery_required
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['controller', 'driver', 'unknown_object', 'missing_object',
    'stale_joints', 'stale_response', 'future_response', 'runtime_restart', 'no_matching_stop',
    'missing_fields', 'error_response', 'read_timeout', 'cancelled_during_read'])
async def test_missing_live_evidence_keeps_motion_blocked(fault):
    calls = []
    async def call(name, args):
        calls.append(name)
        state = ready_state()
        if fault == 'controller': state['controller_work_finished'] = False
        if fault == 'driver': state['driver_stop_acknowledged'] = False
        if fault == 'unknown_object': state['possession'] = 'unknown'
        if fault == 'missing_object': del state['possession']
        if fault == 'stale_joints': state['joint_feedback_error'] = 'stale'
        if fault == 'stale_response': state['checked_at'] -= 10.
        if fault == 'future_response': state['checked_at'] += 10.
        if fault == 'runtime_restart': state['runtime_started_at'] = time.time() - 5.
        if fault == 'no_matching_stop': state['last_stop_at'] = time.time() - 30.
        if fault == 'missing_fields': state = {'recovery_ready': True}
        if fault == 'error_response': return {'status': 'failure', 'result': state}
        if fault == 'read_timeout': raise TimeoutError('read state')
        if fault == 'cancelled_during_read': executor.execution_context.cancel_event.set()
        return {'status': 'success', 'result': state}
    executor = recheckable_executor(call)
    assert not (await executor.execute_tool_call(ToolCall('move_to_waypoint'))).success
    assert executor.recovery_required and calls == ['get_transport_state']


@pytest.mark.asyncio
async def test_malformed_structured_marker_stays_blocked_without_raising(tmp_path):
    field = 'tool'
    calls = []
    async def call(name, args):
        calls.append(name)
        return {'status': 'success', 'result': ready_state()}
    record = dict(recheckable_executor(call).recovery_cause)
    record[field] = ['invalid']
    marker = tmp_path / 'recovery-required'
    marker.write_text(json.dumps(record))
    executor = ToolExecutor(SimpleNamespace(call_tool=call), recovery_path=marker)
    executor.execution_context = ExecutionContext('Stow', request_id='new')
    result = await executor.execute_tool_call(ToolCall('stow_arm'))
    assert result.error_details['code'] == 'RECOVERY_REQUIRED' and executor.recovery_required
    assert calls == [] and 'Greifer' in executor.recovery_guidance('de')


@pytest.mark.asyncio
@pytest.mark.parametrize('cause_status,cause_code,allowed', [('failure','ROBOT_STATE_UNCERTAIN',True),
    ('timeout','ROBOT_STATE_UNCERTAIN',False), ('failure','TRANSPORT_ERROR',False)])
@pytest.mark.parametrize('tool,state,source', [('report_empty_gripper','empty','explicit_user_empty_report'),
    ('report_holding_gripper','holding_assumed','explicit_user_holding_report')])
async def test_saved_human_report_only_releases_a_resolved_tracked_robot_fault(cause_status, cause_code, allowed, tool, state, source):
    calls = []
    async def call(name,args):
        calls.append(name)
        return {'status':'success','result':{'possession':state,'possession_source':source,
            'human_report_recorded':True} if name == tool else ready_state()}
    executor = recheckable_executor(call)
    executor.recovery_cause.update(status=cause_status,error_code=cause_code)
    result = await executor.execute_tool_call(ToolCall(tool))
    assert result.success
    executor.execution_context.add_result(result)
    assert await executor.revalidate_after_possession_report() is allowed
    assert executor.recovery_required is not allowed
    assert calls == [tool, 'get_transport_state'] if allowed else calls == [tool]


@pytest.mark.asyncio
async def test_stow_can_repair_posture_without_claiming_unknown_possession_is_resolved():
    calls = []
    async def call(name, args):
        calls.append(name)
        state = ready_state()
        state.update(possession='unknown', recovery_ready=False)
        return {'status': 'success', 'result': state if name == 'get_transport_state' else {}}
    executor = recheckable_executor(call)
    executor.recovery_cause.update(tool='grab_item', status='failure', error_code='ROBOT_STATE_UNCERTAIN')
    assert (await executor.execute_tool_call(ToolCall('stow_arm'))).success
    assert executor.recovery_required
    blocked = await executor.execute_tool_call(ToolCall('move_to_waypoint'))
    assert blocked.error_details['code'] == 'OBJECT_STATE_INVALID'
    assert calls == ['get_transport_state', 'stow_arm', 'get_transport_state']


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['controller', 'driver', 'runstop', 'stale', 'moving'])
@pytest.mark.parametrize('tool', ['calibrate_arm', 'stow_arm'])
async def test_arm_recovery_never_bypasses_unfinished_or_unavailable_hardware(fault, tool):
    calls = []
    async def call(name, args):
        calls.append(name)
        state = ready_state()
        state.update(possession='unknown', recovery_ready=False)
        if fault == 'controller': state['controller_work_finished'] = False
        if fault == 'driver': state['driver_stop_acknowledged'] = False
        if fault == 'runstop': state['driver_state']['runstop_active'] = True
        if fault == 'stale': state['driver_state']['sampled_at'] -= 10.
        if fault == 'moving': state['driver_state']['motion_idle'] = False
        return {'status': 'success', 'result': state}
    executor = recheckable_executor(call)
    executor.recovery_cause.update(tool='grab_item', status='failure', error_code='ROBOT_STATE_UNCERTAIN')
    assert not (await executor.execute_tool_call(ToolCall(tool))).success
    assert calls == ['get_transport_state'] and executor.recovery_required


@pytest.mark.asyncio
async def test_runstop_then_fresh_request_after_restart_releases_only_a_tracked_stop(tmp_path):
    calls, runstop = [], True
    async def call(name, args):
        calls.append(name)
        state = ready_state()
        state['runtime_started_at'] = time.time() - 5.
        state['last_stop_at'] = 0.  # Fresh process did not receive the old stop.
        state['driver_state'].update(driver_started_at=time.time() - 6., runstop_active=runstop)
        return {'status': 'success', 'result': state if name == 'get_transport_state' else {}}
    marker = tmp_path / 'recovery-required'
    marker.touch()
    executor = recheckable_executor(call, marker)
    blocked = await executor.execute_tool_call(ToolCall('calibrate_arm'))
    assert blocked.error_details['code'] == 'RUNSTOP_ACTIVE'
    assert blocked.error_details['execution_started'] is False
    assert executor.recovery_required and marker.exists()
    assert calls == ['get_transport_state']
    runstop = False
    executor.execution_context = ExecutionContext('Calibrate and stow', request_id='next')
    assert (await executor.execute_tool_call(ToolCall('calibrate_arm'))).success
    assert (await executor.execute_tool_call(ToolCall('stow_arm'))).success
    assert calls == ['get_transport_state', 'get_transport_state', 'calibrate_arm', 'stow_arm']
    assert not executor.recovery_required and not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [
    'driver_not_restarted',
    'action_service_not_restarted',
    'stale_driver',
    'missing_driver',
    'arm_busy',
    'streaming',
    'moving',
    'gamepad',
    'timeout',
])
async def test_restart_does_not_bypass_unconfirmed_hardware(fault):
    calls = []
    async def call(name, args):
        calls.append(name)
        state = ready_state()
        state.update(runtime_started_at=time.time() - 5., last_stop_at=0.)
        driver = state['driver_state']
        driver['driver_started_at'] = time.time() - 6.
        if fault == 'driver_not_restarted': driver['driver_started_at'] -= 100.
        if fault == 'action_service_not_restarted': state['runtime_started_at'] -= 100.
        if fault == 'stale_driver': driver['sampled_at'] -= 10.
        if fault == 'future_driver': driver['sampled_at'] += 10.
        if fault == 'missing_driver': state.pop('driver_state')
        if fault == 'arm_busy': driver['arm_service_active'] = True
        if fault == 'streaming': driver['streaming_position_active'] = True
        if fault == 'moving': driver['motion_idle'] = False
        if fault == 'too_brief': driver['stationary_for_seconds'] = .1
        if fault == 'invalid_idle_duration': driver['stationary_for_seconds'] = float('inf')
        if fault == 'gamepad': driver['mode'] = 'gamepad'
        return {'status': 'success', 'result': state}
    executor = recheckable_executor(call)
    if fault == 'failure_not_stop':
        executor.recovery_cause.update(status='failure', error_code='ROBOT_STATE_UNCERTAIN')
    if fault == 'timeout': executor.recovery_cause['status'] = 'timeout'
    assert not (await executor.execute_tool_call(ToolCall('move_to_waypoint'))).success
    assert executor.recovery_required and 'move_to_waypoint' not in calls

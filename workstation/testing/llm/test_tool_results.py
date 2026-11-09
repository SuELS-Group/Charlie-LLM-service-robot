import asyncio
from types import SimpleNamespace

import httpx
import pytest
from models import ExecutionStatus, ToolCall
from tool_access import ToolExecutor


@pytest.mark.parametrize('raw', [
    None,
    'Error: unavailable',
    [],
    {'status': 'unexpected'},
    {'status': 'success', 'error': {'message': 'failed'}},
])
def test_invalid_result_never_means_success(raw):
    result = ToolExecutor(SimpleNamespace())._parse(ToolCall('move_to'), raw, False)
    assert not result.success
    assert result.error_details['retryable'] is False
    assert result.error_details['execution_uncertain'] is True


@pytest.mark.parametrize('status', [
    'failure',
    'stopped',
])
def test_partial_state_and_error_details_survive(status):
    raw = {'status': status, 'result': {'item_released': True},
           'error': {'code': 'TEST', 'retryable': False, 'message': 'test'}}
    result = ToolExecutor(SimpleNamespace())._parse(ToolCall('give_to_person'), raw, True)
    assert result.status == ExecutionStatus(status)
    assert result.result['result']['item_released'] is True
    assert result.error_details['code'] == 'TEST'
    assert not result.success


@pytest.mark.parametrize('exception', [
    TimeoutError('wait expired'),
    httpx.ReadTimeout('wait expired'),
    ConnectionError('disconnected'),
])
@pytest.mark.asyncio
async def test_transport_errors_are_nonretryable(exception):
    async def fail(*_args):
        raise exception
    result = await ToolExecutor(SimpleNamespace(call_tool=fail)).execute_tool_call(ToolCall('move_to'))
    assert result.status == (ExecutionStatus.FAILURE if isinstance(exception, ConnectionError) else ExecutionStatus.TIMEOUT)
    assert result.error_details['execution_uncertain'] is True
    assert result.error_details['retryable'] is False


def test_explicit_success_remains_success():
    raw = {'status': 'success', 'result': {'arrived': True}, 'meta': {'announcement': 'Arrived.'}}
    result = ToolExecutor(SimpleNamespace())._parse(ToolCall('move_to'), raw, False)
    assert result.success and result.announcement == 'Arrived.'

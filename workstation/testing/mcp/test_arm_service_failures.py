from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mcp_server.call_state import record_call_fault
from mcp_server.tools import manipulation


@pytest.mark.parametrize('tool,operation', [(manipulation.calibrate_arm, 'home'), (manipulation.stow_arm, 'stow')])
def test_runstop_refusal_has_typed_reason_and_proves_no_execution(monkeypatch, tool, operation):
    driver = Mock(return_value={'success': False, 'message': f'Cannot {operation} while in mode=runstopped.'})
    state_update = Mock()
    monkeypatch.setattr(manipulation, 'call_trigger_service', driver)
    monkeypatch.setattr(manipulation, 'call_service', state_update)
    result = tool()
    assert result['status'] == 'failure'
    assert result['error']['code'] == 'RUNSTOP_ACTIVE'
    assert result['error']['execution_started'] is False
    assert result['error']['execution_uncertain'] is False
    assert result['error']['completion_confirmed'] is True
    driver.assert_called_once()
    state_update.assert_not_called()


@pytest.mark.parametrize('tool,operation', [(manipulation.calibrate_arm, 'home'), (manipulation.stow_arm, 'stow')])
def test_runstop_then_new_request_can_succeed_without_internal_retry(monkeypatch, tool, operation):
    driver = Mock(side_effect=[
        {'success': False, 'message': f'Cannot {operation} while in mode=runstopped.'},
        {'success': True, 'message': 'Completed.'},
    ])
    monkeypatch.setattr(manipulation, 'call_trigger_service', driver)
    monkeypatch.setattr(manipulation, 'call_service', Mock(return_value=SimpleNamespace(
        status=SimpleNamespace(status=manipulation.STATUS_SUCCESS),
        result_data='{"calibration_completed": true, "calibration_state_reset": true}')))
    assert tool()['error']['code'] == 'RUNSTOP_ACTIVE'
    assert driver.call_count == 1
    assert tool()['status'] == 'success'
    assert driver.call_count == 2


@pytest.mark.parametrize('tool,operation', [(manipulation.calibrate_arm, 'home'), (manipulation.stow_arm, 'stow')])
def test_transport_uncertainty_cannot_be_downgraded_by_a_runstop_message(monkeypatch, tool, operation):
    def timeout(*a, **k):
        record_call_fault({'code': 'SERVICE_TIMEOUT', 'retryable': False, 'execution_uncertain': True})
        return {'success': False, 'message': f'Cannot {operation} while in mode=runstopped.',
                'execution_uncertain': True}
    monkeypatch.setattr(manipulation, 'call_trigger_service', timeout)
    result = tool()
    assert result['status'] == 'timeout' and result['error']['code'] == 'SERVICE_TIMEOUT'
    assert result['error']['execution_uncertain'] is True
    assert result['error'].get('execution_started') is not False


@pytest.mark.parametrize('tool', [manipulation.calibrate_arm, manipulation.stow_arm])
@pytest.mark.parametrize('prefix', ['ARM_OPERATION_INTERRUPTED', 'ARM_OPERATION_FAILED'])
def test_driver_failure_after_start_retains_physical_uncertainty(monkeypatch, tool, prefix):
    monkeypatch.setattr(manipulation, 'call_trigger_service', lambda *a, **k: {
        'success': False, 'message': f'{prefix}: runstop or motor fault during operation'})
    error = tool()['error']
    assert error['code'] == prefix and error['execution_started'] is True
    assert error['execution_uncertain'] is True


@pytest.mark.parametrize('tool', [manipulation.calibrate_arm, manipulation.stow_arm])
def test_missing_response_does_not_claim_confirmed_rejection(monkeypatch, tool):
    monkeypatch.setattr(manipulation, 'call_trigger_service', lambda *a, **k: None)
    assert tool()['error']['execution_uncertain'] is True

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mcp_server.tools import manipulation


def test_clean_gesture_timeout_keeps_safe_cleanup(monkeypatch):
    details = {'completion_confirmed': True, 'execution_uncertain': False, 'travel_ready': True}
    response = SimpleNamespace(status=SimpleNamespace(status=manipulation.STATUS_TIMEOUT, message='no gesture'),
                               result_data=json.dumps(details))
    monkeypatch.setattr(manipulation, 'call_service', lambda *_a, **_k: response)
    result = manipulation._run_execute_action('take_from_hand', {}, 100,
        stopped_message='stopped', timeout_code='HAND_PICKUP_TIMEOUT', timeout_message='timeout',
        failure_code='FAILED', failure_message='failed', success_result={})
    assert result['status'] == 'timeout' and result['result']['travel_ready']
    assert result['error']['execution_uncertain'] is False


def test_unconfirmed_success_is_not_accepted(monkeypatch):
    response = SimpleNamespace(status=SimpleNamespace(status=manipulation.STATUS_SUCCESS, message='done'), result_data='')
    monkeypatch.setattr(manipulation, 'call_service', lambda *_a, **_k: response)
    result = manipulation._run_execute_action('take_from_hand', {}, 100,
        stopped_message='stopped', timeout_code='TIMEOUT', timeout_message='timeout',
        failure_code='FAILED', failure_message='failed', success_result={})
    assert result['status'] == 'failure' and result['error']['execution_uncertain']


@pytest.mark.parametrize('timeout', [float('inf'), float('nan'), -1, 1000])
def test_invalid_timeout_is_rejected_before_person_approach(monkeypatch, timeout):
    approach = Mock()
    monkeypatch.setattr(manipulation, '_prepare_for_person_handoff', approach)
    result = manipulation.take_from_hand(timeout_seconds=timeout)
    assert result['error']['code'] == 'INVALID_ARGUMENTS'
    approach.assert_not_called()

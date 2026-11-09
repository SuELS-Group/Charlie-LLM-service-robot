from types import SimpleNamespace

import pytest
from mcp_server.tools import navigation


@pytest.mark.parametrize('confirmed,success', [(True, True), (True, False), (False, False)])
def test_timeout_keeps_known_completion_and_uncertainty(monkeypatch, confirmed, success):
    response = {'accepted': True, 'timed_out': True, 'completion_confirmed': confirmed,
        'cancellation_confirmed': confirmed and not success,
        'result': SimpleNamespace(status=SimpleNamespace(
            status=navigation.STATUS_SUCCESS if success else navigation.STATUS_STOPPED)),
        'message': 'timeout'}
    monkeypatch.setattr(navigation, 'call_action', lambda *_args, **_kwargs: response)
    result = navigation._run_navigation_action('/test', object, object(), 10,
        failure_code='FAIL', timeout_code='TIMEOUT')
    assert result['status'] == 'timeout'
    assert result['error']['execution_uncertain'] is not confirmed
    assert result['result']['completed_late'] is (confirmed and success)

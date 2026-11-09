import json
from types import SimpleNamespace

import pytest
from mcp_server.tools import docking


@pytest.mark.parametrize('evidence,announcement', [
    ({'charger_connected': True, 'charging': True}, "I'm now charging"),
    ({'charger_connected': True, 'battery_full': True}, 'already full')])
def test_docking_reports_the_verified_charge_state(monkeypatch, evidence, announcement):
    evidence = {**evidence, 'completion_confirmed': True, 'execution_uncertain': False}
    response = SimpleNamespace(status=SimpleNamespace(status=docking.STATUS_SUCCESS, message='done'),
                               result_data=json.dumps(evidence))
    monkeypatch.setattr(docking, 'call_service', lambda *_args, **_kwargs: response)
    result = docking.dock_robot()
    assert result['status'] == 'success'
    assert announcement in result['meta']['announcement']


@pytest.mark.parametrize('evidence', [{}, {'charger_connected': True, 'charging': False},
                                     {'charger_connected': False, 'charging': True}])
def test_success_without_charging_evidence_is_rejected(monkeypatch, evidence):
    response = SimpleNamespace(status=SimpleNamespace(status=docking.STATUS_SUCCESS, message='done'),
                               result_data=json.dumps(evidence))
    monkeypatch.setattr(docking, 'call_service', lambda *_args, **_kwargs: response)
    assert docking.dock_robot()['status'] == 'failure'


def test_charging_without_controller_completion_is_not_overall_success(monkeypatch):
    response = SimpleNamespace(status=SimpleNamespace(status=docking.STATUS_SUCCESS, message='done'),
        result_data=json.dumps({'charger_connected': True, 'charging': True}))
    monkeypatch.setattr(docking, 'call_service', lambda *_args, **_kwargs: response)
    result = docking.dock_robot()
    assert result['error']['code'] == 'COMPLETION_UNCONFIRMED'
    assert result['error']['execution_uncertain']


def test_clean_docking_timeout_preserves_confirmed_completion(monkeypatch):
    response = SimpleNamespace(status=SimpleNamespace(status=docking.STATUS_TIMEOUT, message='timed out'),
        result_data=json.dumps({'completion_confirmed': True, 'execution_uncertain': False}))
    monkeypatch.setattr(docking, 'call_service', lambda *_args, **_kwargs: response)
    result = docking.dock_robot()
    assert result['status'] == 'timeout' and result['result']['completion_confirmed']
    assert result['error']['execution_uncertain'] is False

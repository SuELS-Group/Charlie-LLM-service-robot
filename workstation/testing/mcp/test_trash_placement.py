"""Disposal owns marker localization, guarded approach and fresh placement."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mcp_server.decorators import TOOL_REGISTRY
from mcp_server.tools import manipulation


@pytest.mark.parametrize('failure', [None, 'TRASH_TARGET_NOT_FOUND', 'TRASH_TARGET_UNREACHABLE'])
def test_disposal_approaches_verified_marker_then_places_and_preserves_failure(monkeypatch, failure):
    details = {'completion_confirmed': True, 'execution_uncertain': False,
        'gripper_opened': not failure, 'release_attempted': not failure,
        'possession': 'holding_assumed' if failure else 'empty'}
    if failure:
        details['error_code'] = failure
    placement = SimpleNamespace(status=SimpleNamespace(
        status=0 if failure else 1, message=failure or 'Released at localized bin'),
        result_data=json.dumps(details))
    localized = SimpleNamespace(status=SimpleNamespace(status=1, message='localized'),
        result_data=json.dumps({'completion_confirmed': True, 'source': 'aruco', 'marker_id': 18,
            'x': 1.5, 'y': .2, 'z': .48, 'approach_distance': .65, 'positioned': False}))
    service = Mock(side_effect=[localized, placement])
    monkeypatch.setattr(manipulation, '_require_object_state', lambda expected: None)
    approach = Mock(return_value={'status': 'success', 'result': {}})
    monkeypatch.setattr(manipulation.navigation_tools, 'position_for_action', approach)
    monkeypatch.setattr(manipulation, 'call_service', service)
    result = manipulation.put_item_in_trash()
    assert [call.args[2].action_type for call in service.call_args_list] == ['localize_trash', 'place_item']
    approach.assert_called_once_with(target='trash_can', x=1.5, y=.2, distance=.65)
    endpoint, _, request = service.call_args.args
    assert endpoint == '/execute_action' and request.action_type == 'place_item'
    assert json.loads(request.parameters) == {'surface': 'trash_can',
        'original_x': 1.5, 'original_y': .2, 'z': .48}
    assert result['result']['possession'] == details['possession']
    if failure:
        assert result['status'] == 'failure' and result['error']['code'] == failure
        assert not result['error']['retryable'] and not result['error']['execution_uncertain']
    else:
        assert result['status'] == 'success' and result['result']['surface'] == 'trash_can'


@pytest.mark.parametrize('phase,status', [('localize_trash', 'failure'), ('localize_trash', 'stopped'),
    ('localize_trash', 'timeout'), ('approach', 'failure'), ('approach', 'stopped'), ('approach', 'timeout')])
def test_failed_preparation_never_starts_release(monkeypatch, phase, status):
    monkeypatch.setattr(manipulation, '_require_object_state', lambda expected: None)
    failure = {'status': status, 'error': {'code': 'PREPARATION_FAILED', 'execution_uncertain': False}}
    localized = {'status': 'success', 'result': {'x': 1.5, 'y': .2, 'approach_distance': .65}}
    execute = Mock(return_value=failure if phase == 'localize_trash' else localized)
    approach = Mock(return_value=failure)
    monkeypatch.setattr(manipulation, '_run_execute_action', execute)
    monkeypatch.setattr(manipulation.navigation_tools, 'position_for_action', approach)
    assert manipulation.put_item_in_trash() == failure
    assert execute.call_args.args[0] == 'localize_trash'
    execute.assert_called_once()
    if phase == 'localize_trash':
        approach.assert_not_called()


def test_already_reachable_and_aligned_bin_needs_no_navigation(monkeypatch):
    monkeypatch.setattr(manipulation, '_require_object_state', lambda expected: None)
    execute = Mock(side_effect=[{'status': 'success', 'result': {'positioned': True, 'x': 1.5, 'y': .2, 'z': .48}},
                               {'status': 'success', 'result': {'surface': 'trash_can'}}])
    approach = Mock()
    monkeypatch.setattr(manipulation, '_run_execute_action', execute)
    monkeypatch.setattr(manipulation.navigation_tools, 'position_for_action', approach)
    assert manipulation.put_item_in_trash()['status'] == 'success'
    approach.assert_not_called()
    assert [call.args[0] for call in execute.call_args_list] == ['localize_trash', 'place_item']


def test_disposal_catalog_has_no_external_scan_or_position_prerequisite(monkeypatch):
    from test_tool_metadata import catalog
    tools = catalog(monkeypatch, TOOL_REGISTRY['put_item_in_trash'])
    expanded = tools.expand([tools.by_name['put_item_in_trash']])
    assert [tool.name for tool in expanded] == ['put_item_in_trash']
    assert not expanded[0].meta.get('workflow_before') and not expanded[0].meta.get('requires')


@pytest.mark.parametrize('utterance', [
    'Charlie, wirf den Ball in den Müll.', 'Lege das Item in den Mülleimer.',
    'Bitte entsorge den Gegenstand.', 'Wirf es in den Muell.',
    'Put the item in the trash can.', 'Throw the ball into the bin.',
    'Dispose of the held object.', 'Drop this in the rubbish bin.'])
def test_real_tool_registry_finds_disposal_in_german_and_english(monkeypatch, utterance):
    import sys
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'llm_node'))
    from tool_catalog import ToolCatalog
    tools = ToolCatalog(list(TOOL_REGISTRY.values()))
    assert tools.initial_tools(utterance)[0].name == 'put_item_in_trash'

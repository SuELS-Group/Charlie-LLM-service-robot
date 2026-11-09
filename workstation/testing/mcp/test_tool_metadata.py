from typing import List, Optional, Union

import pytest
from mcp_server.decorators import TOOL_REGISTRY, _build_tool_metadata
from mcp_server.tools import manipulation, navigation  # Register the real tools.


def catalog(monkeypatch, metadata):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'llm_node'))
    from tool_catalog import ToolCatalog
    return ToolCatalog([metadata])


@pytest.mark.parametrize('tool', ['give_to_person', 'take_from_hand', 'drive_to_person'])
def test_real_person_tools_keep_optional_text_contract(monkeypatch, tool):
    person = None
    tools = catalog(monkeypatch, TOOL_REGISTRY[tool])
    from models import ToolCall
    assert tools.validate_call(ToolCall(tool, {'person': person}))[0]
    assert tools.validate_call(ToolCall(tool, {}))[0]


@pytest.mark.parametrize('person', [
    {'person': 'TestUserA'},
    123,
])
def test_real_person_tools_reject_nested_objects_before_mcp(monkeypatch, person):
    tool = 'give_to_person'
    tools = catalog(monkeypatch, TOOL_REGISTRY[tool])
    from models import ToolCall
    valid, error, _ = tools.validate_call(ToolCall(tool, {'person': person}))
    assert not valid and 'person' in error


@pytest.mark.parametrize('annotation,valid,invalid', [
    (Optional[float], None, True),
    (Union[str, int], 'TestUserA', {'person': 'TestUserA'}),
    (List[float], [1.0, 2.0], ['two']),
])
def test_generic_parameter_contracts_survive_registry_roundtrip(monkeypatch, annotation, valid, invalid):
    def tool(value=None):
        pass
    tool.__annotations__ = {'value': annotation}
    tools = catalog(monkeypatch, _build_tool_metadata(tool, name='tool', description=''))
    from models import ToolCall
    assert tools.validate_call(ToolCall('tool', {'value': valid}))[0]
    assert not tools.validate_call(ToolCall('tool', {'value': invalid}))[0]

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_store import store
from test_evidence import approve
from mcp_server.tools import knowledge
from mcp_server.knowledge_query import BoundedKnowledgeQuery


@pytest.fixture(autouse=True)
def isolate_tool(monkeypatch, store):
    monkeypatch.setenv('ROBOT_OPERATION_MODE', 'development')
    monkeypatch.setenv('ROBOT_KNOWLEDGE_MODE', 'local')
    monkeypatch.setattr(knowledge, '_store', store)
    monkeypatch.setattr(knowledge, '_query_runner', BoundedKnowledgeQuery())


def test_tool_returns_sources_and_generation_only_after_approval(store):
    manifest = store.rebuild()
    unapproved = knowledge.query_knowledge_base('Where is the lab?')
    assert unapproved['error']['code'] == 'KNOWLEDGE_UNAPPROVED'
    assert not unapproved['error']['retryable'] and not unapproved['error']['execution_uncertain']
    approve(store)
    result = knowledge.query_knowledge_base('Where is the lab?')
    assert result['status'] == 'success' and result['result']['generation'] == manifest['collection']
    assert result['result']['chunks'][0]['source'] == 'lab.TXT'


@pytest.mark.parametrize('fault,code', [('outdated','KNOWLEDGE_OUTDATED'), ('embedding','KNOWLEDGE_UNAVAILABLE'),
    ('timeout','KNOWLEDGE_UNAVAILABLE'), ('unavailable','KNOWLEDGE_UNAVAILABLE')])
def test_tool_failure_reasons_do_not_masquerade_as_no_matches(monkeypatch, store, fault, code):
    if fault != 'unavailable':
        store.rebuild(); approve(store)
    if fault == 'outdated': (store.docs_dir/'lab.TXT').write_text('Updated')
    elif fault == 'embedding': store.embed = Mock(side_effect=RuntimeError('offline'))
    elif fault == 'timeout': monkeypatch.setattr(knowledge, '_query_runner', SimpleNamespace(run=Mock(side_effect=TimeoutError())))
    result = knowledge.query_knowledge_base('Where is the lab?')
    assert result['status'] == 'failure' and result['error']['code'] == code
    assert result['error']['retryable'] is False
    assert result['error']['execution_uncertain'] is False

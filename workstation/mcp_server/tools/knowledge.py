from __future__ import annotations

from pathlib import Path

from mcp_server.config import config
from mcp_server.decorators import ToolStatus, tool_result, tool_with_meta
from workstation_common import get_logger
from workstation_common.knowledge_store import KnowledgeStore
from workstation_common.knowledge_policy import local_knowledge_mode
from mcp_server.knowledge_query import BoundedKnowledgeQuery

log = get_logger('mcp.knowledge')
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_store = None
_query_runner = BoundedKnowledgeQuery()


def _get_store():
    global _store
    if _store is None:
        _store = KnowledgeStore(config, _PROJECT_ROOT)
    return _store


@tool_with_meta(wait_duration=10.0, returns_data=True)
def query_knowledge_base(query: str) -> dict:
    """
    Retrieve relevant knowledge from the knowledge base to answer a factual question.
    Use when the user asks about room locations, procedures, objects, or
    environment-specific information not available through other tools.
    Args:
        query: The question or topic to search for.
    """
    try:
        log.info(f'[TOOL] query_knowledge_base(query={query!r})')
        strict = local_knowledge_mode()
        evidence = _query_runner.run(lambda: _get_store().query_evidence(query, require_approval=strict),
            timeout=min(10., max(.1, float(getattr(config, 'knowledge_query_timeout_s', 10.)))))
        reason = evidence['evidence_status']
        if reason in {'outdated', 'unapproved', 'unavailable'}:
            return tool_result(ToolStatus.FAILURE, result=evidence, error={
                'code': 'KNOWLEDGE_' + reason.upper(), 'message': 'Knowledge evidence is ' + reason,
                'retryable': False, 'execution_uncertain': False})
        return tool_result(ToolStatus.SUCCESS, result=evidence)
    except Exception as e:
        log.error(f'query_knowledge_base failed: {e}')
        return tool_result(ToolStatus.FAILURE, error={
            'code': 'KNOWLEDGE_UNAVAILABLE',
            'message': str(e),
            'retryable': False, 'execution_uncertain': False, })


@tool_with_meta(wait_duration=3.0)
def store_memory(fact: str) -> dict:
    """
    Store a fact in shared long-term textual memory so it can be recalled later.
    Facts are shared across conversations, with no per-person ownership.
    Use when the user explicitly asks the robot to remember something.
    Examples: "remember that Peter's favorite country is Italy",
              "remember that the meeting starts at 10am on Fridays".
    Args:
        fact: The exact fact to remember, as a short natural language sentence.
    """
    try:
        log.info(f'[TOOL] store_memory(fact={fact!r})')
        record = _get_store().store_memory(fact)
        return tool_result(ToolStatus.SUCCESS, result=record, meta={
            'announcement': "I already remember that." if record['duplicate'] else "Got it, I'll remember that."})
    except Exception as e:
        log.error(f'store_memory failed: {e}')
        return tool_result(ToolStatus.FAILURE, error={'code': 'MEMORY_ERROR', 'message': str(e), 'retryable': False, })


@tool_with_meta(wait_duration=3.0)
def recall_memory(query: str) -> dict:
    """
    Retrieve facts explicitly saved in shared textual memory.
    This is not physical-location or person-tracking memory; do not use it to
    find where a person was last seen.
    Use when the user asks about something the robot was told to remember,
    or when recalling personal details that were stored earlier.
    Args:
        query: The topic or question to look up in memory.
    """
    try:
        log.info(f'[TOOL] recall_memory(query={query!r})')
        memories = _get_store().recall(query)
        if not memories:
            return tool_result(ToolStatus.SUCCESS, result={
                'count': 0,
                'memories': [],
                'message': 'No relevant memories found.'})
        return tool_result(ToolStatus.SUCCESS, result={'count': len(memories), 'memories': memories})
    except Exception as e:
        log.error(f'recall_memory failed: {e}')
        return tool_result(ToolStatus.FAILURE, error={'code': 'MEMORY_ERROR', 'message': str(e), 'retryable': False, })

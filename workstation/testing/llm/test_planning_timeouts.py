import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from llm_test_helpers import _workspace
from agent import Agent
from llm_client import LLMClient
from models import ExecutionContext, ExecutionStatus
from planner_telemetry import PlannerTelemetryClient
from tool_catalog import ToolCatalog
import user_context


def test_decisions_use_planning_transport_and_text_uses_short_transport():
    calls = []
    def respond(kind):
        def chat(**kwargs):
            calls.append((kind, kwargs))
            content = '{"action":"call_tool","tool":"calibrate_arm","args":{}}' if kind == 'planning' else 'Hello'
            return SimpleNamespace(message=SimpleNamespace(content=content))
        return SimpleNamespace(chat=chat)
    llm = LLMClient(respond('text'), planning_client=respond('planning'))
    assert llm.decision([{'role': 'user', 'content': 'Calibrate and stow'}], ['calibrate_arm']).decision.tool == 'calibrate_arm'
    assert llm.text('Translate', 'Hallo') == 'Hello'
    assert [kind for kind, _ in calls] == ['planning', 'text']
    planning = calls[0][1]
    assert {branch['properties']['action']['const'] for branch in planning['format']['oneOf']} == {
        'answer', 'call_tool', 'select_tools', 'finish', 'ask_user', 'refuse', 'replace_request'}
    assert planning['think'] is True
    assert planning['options']['num_predict'] == min(4096, planning['options']['num_ctx'] - 256)
    assert 'format' not in calls[1][1] and calls[1][1]['think'] is False
    assert calls[1][1]['options']['num_predict'] == 1024


def test_even_complete_looking_json_is_rejected_if_generation_was_truncated():
    client = SimpleNamespace(chat=lambda **_kwargs: SimpleNamespace(
        message=SimpleNamespace(content='{"action":"call_tool","tool":"calibrate_arm","args":{}}'),
        done_reason='length'))
    with pytest.raises(RuntimeError, match='truncated decisions are not executed'):
        LLMClient(client).decision([{'role': 'user', 'content': 'Calibrate'}], ['calibrate_arm'])


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [httpx.ReadTimeout('slow model'), httpx.ConnectTimeout('server unavailable')])
async def test_planning_transport_timeout_is_terminal_without_tool_dispatch(error):
    decisions = []
    def decide(*_args):
        decisions.append(True)
        raise error
    async def execute(_call):
        pytest.fail('A timed-out planning request must not dispatch an action')
    agent = Agent(SimpleNamespace(decision=decide), ToolCatalog([{'name': 'calibrate_arm'}]),
        SimpleNamespace(execute_tool_call=execute), PlannerTelemetryClient(enabled=False))
    try:
        status, message, steps, results = await agent.run(ExecutionContext('Calibrate your arm'))
        assert status == ExecutionStatus.TIMEOUT
        assert 'could not plan' in message
        assert len(decisions) == 1 and not steps and not results
    finally:
        agent.close()

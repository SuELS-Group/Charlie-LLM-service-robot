import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from llm_test_helpers import _workspace
from agent import Agent
from decision_contract import PlanningOutputLimitError
from models import AgentDecision, AgentTurn, ExecutionContext, ExecutionStatus, PlannerPromptMetrics, ToolResult
from planner_telemetry import PlannerTelemetryClient
from tool_catalog import ToolCatalog


def truncated(ctx=65536, prompt=3000):
    return PlanningOutputLimitError('truncated', metrics=PlannerPromptMetrics('test', ctx,
        prompt_tokens=prompt, response_tokens=4096, output_token_limit=4096, finish_reason='length'))


def make_agent(sequence):
    prompts, budgets, calls = [], [], []
    decisions = iter(sequence)
    def decide(messages, tools, **kwargs):
        prompts.append(json.loads(messages[-1]['content']))
        budgets.append(kwargs.get('max_output_tokens'))
        decision = next(decisions)
        if isinstance(decision, Exception):
            raise decision
        if callable(decision):
            decision = decision()
        return AgentTurn(decision, '{}', messages, PlannerPromptMetrics('test', 65536), tools)
    async def execute(call):
        calls.append(call.name)
        return ToolResult(call.name, ExecutionStatus.SUCCESS, args=call.args)
    agent = Agent(SimpleNamespace(decision=decide), ToolCatalog([
        {'name': 'calibrate_arm'}, {'name': 'stow_arm'}]),
        SimpleNamespace(execute_tool_call=execute), PlannerTelemetryClient(enabled=False))
    return agent, prompts, budgets, calls


@pytest.mark.asyncio
async def test_retry_replans_only_next_step_preserving_completed_physical_action():
    agent, prompts, budgets, calls = make_agent([
        AgentDecision('call_tool', tool='calibrate_arm'), truncated(),
        AgentDecision('call_tool', tool='stow_arm'), AgentDecision('finish', answer='Done')])
    try:
        status, _, _, _ = await agent.run(ExecutionContext('Calibrate and stow'))
        assert status == ExecutionStatus.SUCCESS and calls == ['calibrate_arm', 'stow_arm']
        assert budgets == [None, None, 8192, None]
        assert prompts[2]['current_task_results'][0]['tool'] == 'calibrate_arm'
        assert 'never repeat completed actions' in prompts[2]['feedback'][-1]
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_second_truncation_is_terminal_and_never_dispatches_a_tool():
    agent, _, budgets, calls = make_agent([truncated(), truncated()])
    try:
        status, _, _, _ = await agent.run(ExecutionContext('Calibrate'))
        assert status == ExecutionStatus.FAILURE and budgets == [None, 8192] and not calls
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_context_capacity_clamps_retry():
    agent, _, budgets, _ = make_agent([truncated(ctx=8192, prompt=3000), AgentDecision('finish', answer='Hello')])
    try:
        assert (await agent.run(ExecutionContext('Hello')))[0] == ExecutionStatus.SUCCESS
        assert budgets == [None, 4936]
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_no_room_for_retry_is_terminal():
    agent, _, budgets, calls = make_agent([truncated(ctx=4096, prompt=3000)])
    try:
        assert (await agent.run(ExecutionContext('Calibrate')))[0] == ExecutionStatus.FAILURE
        assert budgets == [None] and not calls
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_retry_shares_original_time_budget():
    release = threading.Event()
    agent, _, budgets, calls = make_agent([truncated(), lambda: release.wait(1)])
    agent.decision_timeout_s = .07
    started = asyncio.get_running_loop().time()
    try:
        assert (await agent.run(ExecutionContext('Calibrate')))[0] == ExecutionStatus.TIMEOUT
        assert asyncio.get_running_loop().time() - started < .2
        assert budgets == [None, 8192] and not calls
    finally:
        release.set()
        agent.close()


@pytest.mark.asyncio
async def test_stop_between_truncation_and_retry_prevents_retry():
    context = ExecutionContext('Calibrate')
    def stop():
        context.cancel_event.set()
        raise truncated()
    agent, _, budgets, calls = make_agent([stop])
    try:
        assert (await agent.run(context))[0] == ExecutionStatus.STOPPED
        assert budgets == [None] and not calls
    finally:
        agent.close()

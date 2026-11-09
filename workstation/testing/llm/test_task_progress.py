import json
from types import SimpleNamespace

import pytest
from llm_test_helpers import _workspace
import agent as agent_module
from agent import Agent
from models import AgentDecision, AgentTurn, ExecutionContext, ExecutionStatus, PlannerPromptMetrics, ToolResult
from planner_telemetry import PlannerTelemetryClient
from tool_catalog import ToolCatalog


def goal(id, tool='move_relative'):
    return {'id': str(id), 'label': f'Goal {id}', 'completion_tool': tool}


def call(id, *, plan=None, tool='move_relative', args=None):
    return AgentDecision('call_tool', tool=tool, args=args if args is not None else {'distance': 1},
        task_id=str(id), task_plan=plan or [])


def make_agent(sequence, *, result_status=ExecutionStatus.SUCCESS):
    sequence = iter(sequence)
    prompts, calls = [], []
    def decide(messages, tools):
        prompts.append(json.loads(messages[-1]['content']))
        return AgentTurn(next(sequence), '{}', messages, PlannerPromptMetrics('test', 65536), tools)
    async def execute(action):
        calls.append(action)
        return ToolResult(action.name, result_status, args=action.args,
            error_details={'retryable': False} if result_status != ExecutionStatus.SUCCESS else None)
    catalog = ToolCatalog([{'name': 'move_relative', 'parameters': [{'name': 'distance', 'type': 'float', 'required': True}]},
        {'name': 'get_waypoints'}, {'name': 'move_to_waypoint', 'parameters': [{'name': 'name', 'type': 'str', 'required': True}]},
        {'name': 'battery_status'}])
    return Agent(SimpleNamespace(decision=decide), catalog, SimpleNamespace(execute_tool_call=execute),
        PlannerTelemetryClient(enabled=False)), prompts, calls


@pytest.mark.asyncio
async def test_long_request_continues_after_twenty_with_complete_goal_evidence():
    plan = [goal(i) for i in range(25)]
    agent, prompts, calls = make_agent([call(i, plan=plan if i == 0 else None) for i in range(25)] +
        [AgentDecision('finish', answer='Done')])
    context = ExecutionContext('25 explicitly requested sequential movements')
    try:
        assert (await agent.run(context))[0] == ExecutionStatus.SUCCESS
        assert len(calls) == 25 and context.planning_decisions == 26
        assert all(task['status'] == 'completed' and len(task['evidence']) == 1 for task in context.task_progress)
        assert len(prompts[-1]['current_task_results']) == 10
        assert prompts[-1]['task_progress'][0]['evidence'][0]['args'] == {'distance': 1}
        assert prompts[-1]['task_progress'][0]['status'] == 'completed'
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_premature_finish_is_corrected_and_completed_goal_cannot_replay():
    plan = [goal('first'), goal('second')]
    agent, _, calls = make_agent([call('first', plan=plan), AgentDecision('finish', answer='Done'),
        call('first'), call('second'), AgentDecision('finish', answer='Done')])
    try:
        assert (await agent.run(ExecutionContext('Two movements')))[0] == ExecutionStatus.SUCCESS
        assert [action.task_id for action in calls] == ['first', 'second']
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_helper_lookup_does_not_complete_navigation_and_ledger_survives_reply():
    plan = [goal('navigate', 'move_to_waypoint')]
    agent, _, calls = make_agent([call('navigate', plan=plan, tool='get_waypoints', args={}),
        AgentDecision('ask_user', answer='Which waypoint?'),
        call('navigate', tool='move_to_waypoint', args={'name': 'desk'}), AgentDecision('finish', answer='Arrived')])
    context = ExecutionContext('Go to a waypoint')
    try:
        assert (await agent.run(context))[0] == ExecutionStatus.AWAITING_INPUT
        assert context.task_progress[0]['status'] == 'in_progress'
        context.followups.append({'question': 'Which waypoint?', 'answer': 'Charlie, desk'})
        assert (await agent.run(context))[0] == ExecutionStatus.SUCCESS
        assert context.task_progress[0]['status'] == 'completed'
        assert [action.name for action in calls] == ['get_waypoints', 'move_to_waypoint']
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_failed_action_does_not_mark_goal_complete():
    agent, _, calls = make_agent([call('drive', plan=[goal('drive')])], result_status=ExecutionStatus.FAILURE)
    context = ExecutionContext('Drive')
    try:
        assert (await agent.run(context))[0] == ExecutionStatus.FAILURE
        assert context.task_progress[0]['status'] == 'failed' and len(calls) == 1
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_hard_request_budget_is_finite_even_with_continued_progress(monkeypatch):
    monkeypatch.setattr(agent_module.config, 'agent_max_total_decisions', 5)
    plan = [goal(i) for i in range(7)]
    agent, _, calls = make_agent([call(i, plan=plan if i == 0 else None) for i in range(7)])
    agent.max_steps = 2
    context = ExecutionContext('Seven movements')
    try:
        assert (await agent.run(context))[0] == ExecutionStatus.FAILURE
        assert context.planning_decisions == 5 and len(calls) == 5
        assert [task['status'] for task in context.task_progress] == ['completed'] * 5 + ['pending'] * 2
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_requested_fact_is_delivered_before_the_next_action_even_without_model_flag():
    plan = [goal('battery', 'battery_status'), goal('drive')]
    agent, _, calls = make_agent([call('battery', plan=plan, tool='battery_status', args={}),
        call('drive'), AgentDecision('finish', answer='Done')])
    original = agent.executor.execute_tool_call
    order = []
    async def execute(action):
        order.append(action.name)
        result = await original(action)
        result.announcement = 'Battery at 80 percent'
        return result
    agent.executor.execute_tool_call = execute
    context = ExecutionContext('Tell me battery level, then move')
    try:
        assert (await agent.run(context, {'announce': lambda text: order.append(text)}))[0] == ExecutionStatus.SUCCESS
        assert order == ['battery_status', 'Battery at 80 percent', 'move_relative']
        assert context.task_progress[0]['status'] == 'completed'
    finally:
        agent.close()

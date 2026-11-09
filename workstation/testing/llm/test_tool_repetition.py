import json
from types import SimpleNamespace

import pytest
from llm_test_helpers import _workspace
from agent import Agent
from models import AgentDecision, AgentTurn, ExecutionContext, ExecutionStatus, PlannerPromptMetrics, ToolResult
from planner_telemetry import PlannerTelemetryClient
from tool_catalog import ToolCatalog
from tool_repetition import hidden_cached_helpers, reusable_result
import tool_repetition


def catalog():
    return ToolCatalog([{'name': 'get_waypoints', 'meta': {'provides': [{'token': 'waypoints'}]}},
        {'name': 'move_to_waypoint', 'parameters': [{'name': 'name', 'type': 'str', 'required': True}],
            'meta': {'requires': [{'provider': 'get_waypoints'}], 'invalidates': ['visible_objects']}},
        {'name': 'query_knowledge_base', 'parameters': [{'name': 'query', 'type': 'str', 'required': True}]},
        {'name': 'wiki_summary', 'parameters': [{'name': 'title_query', 'type': 'str', 'required': True},
            {'name': 'sentence_limit', 'type': 'int', 'default': 5}]},
        {'name': 'get_visible_objects'}, {'name': 'battery_status'},
        {'name': 'turn_to_relative', 'parameters': [{'name': 'angle', 'type': 'int', 'required': True}]},
        {'name': 'move_relative', 'parameters': [{'name': 'distance', 'type': 'float', 'required': True}]},
        {'name': 'save_waypoint', 'meta': {'invalidates': ['waypoints']}}])


def observation(name='get_waypoints', args=None, **kwargs):
    return ToolResult(name, ExecutionStatus.SUCCESS, result={'result': {'waypoints': ['desk', 'door']}},
        args=args or {}, **kwargs)


def context(*results, tool='move_to_waypoint'):
    ctx = ExecutionContext('A user request', previous_results=list(results))
    ctx.task_progress = [{'id': 'goal', 'label': 'Current goal', 'completion_tool': tool,
        'status': 'pending', 'evidence': []}]
    return ctx


@pytest.mark.parametrize('mutation,status', [('save_waypoint', ExecutionStatus.SUCCESS),
    ('delete_waypoint', ExecutionStatus.SUCCESS), ('save_pointed_waypoint', ExecutionStatus.FAILURE),
    ('switch_to_localization_mode', ExecutionStatus.SUCCESS), ('switch_to_mapping_mode', ExecutionStatus.SUCCESS)])
def test_waypoint_changes_invalidate_the_snapshot_even_after_partial_failure(mutation, status):
    ctx = context(observation(), ToolResult(mutation, status))
    assert reusable_result(ctx, 'get_waypoints', {}, catalog().by_name) is None


def test_expired_snapshot_is_available_for_a_new_read(monkeypatch):
    original = observation(observed_at=100)
    monkeypatch.setattr(tool_repetition, 'monotonic', lambda: 221)
    ctx = context(original)
    specs = catalog().by_name
    assert reusable_result(ctx, 'get_waypoints', {}, specs) is None
    assert not hidden_cached_helpers(ctx, list(specs.values()), specs)


@pytest.mark.parametrize('name,args', [('battery_status', {}), ('get_visible_objects', {}),
    ('move_relative', {'distance': 1}), ('turn_to_relative', {'angle': -90})])
def test_sensor_state_and_physical_calls_are_never_memoized(name, args):
    assert reusable_result(context(observation(name, args)), name, args, catalog().by_name) is None


def test_failed_lookup_is_not_reused():
    ctx = context(ToolResult('get_waypoints', ExecutionStatus.FAILURE, result={'result': {'waypoints': []}}))
    assert reusable_result(ctx, 'get_waypoints', {}, catalog().by_name) is None


@pytest.mark.asyncio
async def test_repeated_waypoint_helpers_continue_with_one_execution_and_no_duplicate_speech():
    plan = [{'id': 'nav', 'label': 'Navigate', 'completion_tool': 'move_to_waypoint'}]
    decisions = iter([
        AgentDecision('call_tool', tool='get_waypoints', task_id='nav', task_plan=plan),
        AgentDecision('call_tool', tool='get_waypoints', task_id='nav', should_announce=True),
        AgentDecision('call_tool', tool='move_to_waypoint', args={'name': 'desk'}, task_id='nav'),
        AgentDecision('finish', answer='Arrived')])
    prompts, exposed, calls, reused, speech = [], [], [], [], []
    def decide(messages, names):
        prompts.append(json.loads(messages[-1]['content']))
        exposed.append(names)
        return AgentTurn(next(decisions), '{}', messages, PlannerPromptMetrics('test', 65536), names)
    async def execute(call):
        calls.append(call.name)
        return observation(call.name, call.args)
    agent = Agent(SimpleNamespace(decision=decide), catalog(), SimpleNamespace(execute_tool_call=execute),
        PlannerTelemetryClient(enabled=False))
    try:
        status, _, steps, results = await agent.run(ExecutionContext('Navigate to desk'), {
            'reused': lambda index, source, result: reused.append(source), 'announce': speech.append})
        assert status == ExecutionStatus.SUCCESS
        assert calls == ['get_waypoints', 'move_to_waypoint'] and len(steps) == len(results) == 2
        assert 'get_waypoints' not in exposed[1] and 'move_to_waypoint' in exposed[1]
        assert reused == [0] and not speech
        assert 'not executed or spoken again' in prompts[2]['feedback'][-1]
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_explicit_later_lookup_goal_is_executed_again():
    plan = [{'id': str(i), 'label': 'Read waypoints', 'completion_tool': 'get_waypoints'} for i in range(2)]
    decisions = iter([AgentDecision('call_tool', tool='get_waypoints', task_id='0', task_plan=plan),
        AgentDecision('call_tool', tool='get_waypoints', task_id='1'), AgentDecision('finish', answer='Done')])
    calls = []
    def decide(messages, names):
        return AgentTurn(next(decisions), '{}', messages, PlannerPromptMetrics('test', 65536), names)
    async def execute(call):
        calls.append(call.task_id)
        return observation()
    agent = Agent(SimpleNamespace(decision=decide), catalog(), SimpleNamespace(execute_tool_call=execute),
        PlannerTelemetryClient(enabled=False))
    try:
        assert (await agent.run(ExecutionContext('Read the list, then read it again')))[0] == ExecutionStatus.SUCCESS
        assert calls == ['0', '1']
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_retrieval_reuses_identical_queries_and_permits_refined_queries():
    plan = [{'id': 'nav', 'label': 'Find the desk', 'completion_tool': 'move_to_waypoint'}]
    queries = ['where desk?', 'where desk?', 'desk location']
    decisions = iter([*[AgentDecision('call_tool', tool='query_knowledge_base', args={'query': query},
        task_id='nav', task_plan=plan if i == 0 else []) for i, query in enumerate(queries)],
        AgentDecision('call_tool', tool='move_to_waypoint', args={'name': 'desk'}, task_id='nav'),
        AgentDecision('finish', answer='Arrived')])
    calls = []
    def decide(messages, names):
        return AgentTurn(next(decisions), '{}', messages, PlannerPromptMetrics('test', 65536), names)
    async def execute(call):
        calls.append((call.name, call.args))
        return ToolResult(call.name, ExecutionStatus.SUCCESS, result={'result': {'count': 0, 'chunks': []}}, args=call.args)
    agent = Agent(SimpleNamespace(decision=decide), catalog(), SimpleNamespace(execute_tool_call=execute),
        PlannerTelemetryClient(enabled=False))
    try:
        assert (await agent.run(ExecutionContext('Find desk')))[0] == ExecutionStatus.SUCCESS
        assert [args['query'] for name, args in calls if name == 'query_knowledge_base'] == ['where desk?', 'desk location']
        assert calls[-1][0] == 'move_to_waypoint'
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_forever_ignored_lookup_feedback_remains_bounded_without_repeated_tool_calls():
    plan = [{'id': 'nav', 'label': 'Navigate', 'completion_tool': 'move_to_waypoint'}]
    calls, turns = [], []
    def decide(messages, names):
        turns.append(True)
        return AgentTurn(AgentDecision('call_tool', tool='get_waypoints', task_id='nav', task_plan=plan if len(turns)==1 else []),
            '{}', messages, PlannerPromptMetrics('test', 65536), names)
    async def execute(call):
        calls.append(call.name)
        return observation()
    agent = Agent(SimpleNamespace(decision=decide), catalog(), SimpleNamespace(execute_tool_call=execute),
        PlannerTelemetryClient(enabled=False))
    try:
        status, _, _, _ = await agent.run(ExecutionContext('Navigate'))
        assert status == ExecutionStatus.FAILURE and calls == ['get_waypoints']
        assert len(turns) == 4
    finally:
        agent.close()

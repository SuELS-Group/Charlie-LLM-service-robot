import json
from types import SimpleNamespace

import pytest
from llm_test_helpers import _workspace
from agent import Agent
from models import AgentDecision, AgentTurn, ExecutionContext, ExecutionStatus, PlannerPromptMetrics, ToolResult
from planner_telemetry import PlannerTelemetryClient
from tool_catalog import ToolCatalog


def build(sequence, tools):
    sequence = iter(sequence)
    prompts, exposed, calls = [], [], []
    def decide(messages, names):
        prompts.append(json.loads(messages[-1]['content']))
        exposed.append(names)
        return AgentTurn(next(sequence), '{}', messages, PlannerPromptMetrics('test', 65536), names)
    async def execute(call):
        calls.append(call.name)
        return ToolResult(call.name, ExecutionStatus.SUCCESS, result={'ok': True}, args=call.args)
    catalog = ToolCatalog(tools)
    catalog.initial_tools = lambda *_a, **_k: [catalog.by_name[next(iter(catalog.by_name))]]
    return Agent(SimpleNamespace(decision=decide), catalog, SimpleNamespace(execute_tool_call=execute),
        PlannerTelemetryClient(enabled=False)), prompts, exposed, calls


@pytest.mark.asyncio
async def test_current_goal_tools_are_available_even_when_initial_search_omitted_them():
    plan = [{'id': 'nav', 'label': 'Go to service point', 'completion_tool': 'move_to_waypoint'},
        {'id': 'turn', 'label': 'Turn right', 'completion_tool': 'turn_to_relative'},
        {'id': 'dock', 'label': 'Dock', 'completion_tool': 'dock_robot'}]
    agent, _, exposed, calls = build([
        AgentDecision('call_tool', tool='get_waypoints', task_id='nav', task_plan=plan),
        AgentDecision('call_tool', tool='move_to_waypoint', args={'name': 'desk'}, task_id='nav'),
        AgentDecision('call_tool', tool='turn_to_relative', args={'angle': -90}, task_id='turn'),
        AgentDecision('call_tool', tool='dock_robot', task_id='dock'), AgentDecision('finish', answer='Done')],
        [{'name': 'get_waypoints'},
         {'name': 'move_to_waypoint', 'parameters': [{'name': 'name', 'type': 'str', 'required': True}]},
         {'name': 'turn_to_relative', 'parameters': [{'name': 'angle', 'type': 'int', 'required': True}]},
         {'name': 'dock_robot'}])
    try:
        assert (await agent.run(ExecutionContext('Go, turn, dock')))[0] == ExecutionStatus.SUCCESS
        assert calls == ['get_waypoints', 'move_to_waypoint', 'turn_to_relative', 'dock_robot']
        assert 'move_to_waypoint' in exposed[1]
        assert exposed[2] == ['turn_to_relative'] and exposed[3] == ['dock_robot']
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_battery_goal_does_not_allow_knowledge_search_as_a_substitute():
    plan = [{'id': 'battery', 'label': 'Check charging', 'completion_tool': 'battery_status'}]
    agent, prompts, exposed, calls = build([
        AgentDecision('call_tool', tool='query_knowledge_base', args={'query': 'Am I charging?'},
            task_id='battery', task_plan=plan),
        AgentDecision('call_tool', tool='battery_status', task_id='battery'),
        AgentDecision('finish', answer='Charging')],
        [{'name': 'query_knowledge_base', 'parameters': [{'name': 'query', 'type': 'str', 'required': True}]},
         {'name': 'battery_status'}])
    try:
        assert (await agent.run(ExecutionContext('Am I charging?')))[0] == ExecutionStatus.SUCCESS
        assert calls == ['battery_status'] and exposed[1] == ['battery_status']
        assert 'cannot substitute' in prompts[1]['feedback'][-1]
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_open_gripper_does_not_inherit_calibration_from_an_earlier_request():
    from models import CompletedTask
    wrong=[{'id':'home','label':'Calibrate','completion_tool':'calibrate_arm'}]
    corrected=[{'id':'release','label':'Release','completion_tool':'release_item'}]
    agent, prompts, exposed, calls=build([
        AgentDecision('select_tools',query='calibrate_arm',task_plan=wrong),
        AgentDecision('call_tool',tool='release_item',task_id='release',task_plan=corrected),
        AgentDecision('finish',answer='Done')], [{'name':'calibrate_arm'},{'name':'release_item'}])
    context=ExecutionContext('Charlie, öffne zum Test deinen leeren Greifer und fahre ihn danach wieder ein.')
    context.task_history=[CompletedTask(request='Calibrate and stow your arm')]
    try:
        assert (await agent.run(context))[0]==ExecutionStatus.SUCCESS
        assert calls==['release_item'] and 'calibrate_arm' not in exposed[0]
        assert 'Calibration was not requested' in prompts[1]['feedback'][-1]
    finally:agent.close()


@pytest.mark.parametrize('utterance', ['Lege den gehaltenen Ball in den Müll.',
    'I am already in front of the trash can. Put the item in it.'])
@pytest.mark.asyncio
async def test_disposal_goal_does_not_dispatch_an_external_search(utterance):
    plan = [{'id': 'bin', 'label': utterance, 'completion_tool': 'put_item_in_trash'}]
    agent, prompts, exposed, calls = build([
        AgentDecision('call_tool', tool='get_look_around_objects', task_id='bin', task_plan=plan),
        AgentDecision('call_tool', tool='put_item_in_trash', task_id='bin'),
        AgentDecision('finish', answer='Done')],
        [{'name': 'get_look_around_objects'}, {'name': 'put_item_in_trash'}, {'name': 'place_item'}])
    try:
        assert (await agent.run(ExecutionContext(utterance)))[0] == ExecutionStatus.SUCCESS
        assert calls == ['put_item_in_trash']
        assert exposed[1] == ['put_item_in_trash']
        assert 'cannot substitute' in prompts[1]['feedback'][-1]
    finally:
        agent.close()

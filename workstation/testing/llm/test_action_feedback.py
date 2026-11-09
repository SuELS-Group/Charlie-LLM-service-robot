import json
import asyncio
import threading
from types import SimpleNamespace

import pytest
from llm_test_helpers import _workspace
from agent import Agent
from communication_node import LLMNode
from models import AgentDecision, AgentTurn, ExecutionContext, ExecutionStatus, PlannerPromptMetrics, ToolCall, ToolResult
from planner_telemetry import PlannerTelemetryClient
from tool_access import ToolExecutor
from tool_catalog import ToolCatalog
from user_context import UserContext
from std_msgs.msg import String


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['de', 'en'])
@pytest.mark.parametrize('tools', [('calibrate_arm',), ('stow_arm',), ('calibrate_arm', 'stow_arm')])
async def test_arm_maintenance_acknowledges_only_executed_goals(language, tools):
    plan = [{'id': name, 'label': name, 'completion_tool': name} for name in tools]
    turns = [AgentDecision('call_tool', tool=name, task_id=name, task_plan=plan if index == 0 else [])
        for index, name in enumerate(tools)]
    turns.append(AgentDecision('finish', answer='I calibrated, stowed, and checked my empty gripper.'))
    sequence = iter(turns)
    def decide(messages, active_tools):
        return AgentTurn(next(sequence), '{}', messages, PlannerPromptMetrics('test', 65536), active_tools)
    async def execute(call):
        return ToolResult(call.name, ExecutionStatus.SUCCESS)
    agent = Agent(SimpleNamespace(decision=decide), ToolCatalog([{'name': name} for name in tools]),
        SimpleNamespace(execute_tool_call=execute), PlannerTelemetryClient(enabled=False))
    context = ExecutionContext('Calibrate and story run')
    context.user_language = language
    try:
        status, answer, steps, _ = await agent.run(context)
        assert status == ExecutionStatus.SUCCESS and [step.name for step in steps] == list(tools)
        calibrated = 'Armkalibrierung' if language == 'de' else 'calibration'
        stowed = 'Einfahren' if language == 'de' else 'stow'
        assert (calibrated in answer) == ('calibrate_arm' in tools)
        assert (stowed in answer) == ('stow_arm' in tools)
        assert 'gripper' not in answer and 'empty' not in answer and 'leer' not in answer
    finally:
        agent.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('language,phrase', [('de','gehe davon aus'),('en','assuming')])
async def test_completed_pickup_cannot_present_assumed_possession_as_verified(language, phrase):
    plan = [{'id':'pickup','label':'Take item','completion_tool':'take_from_hand'}]
    sequence = iter([AgentDecision('call_tool',tool='take_from_hand',task_id='pickup',task_plan=plan),
        AgentDecision('finish',answer='I definitely have the item.')])
    def decision(messages,tools):
        return AgentTurn(next(sequence),'{}',messages,PlannerPromptMetrics('test',65536),tools)
    async def execute(call):
        return ToolResult(call.name,ExecutionStatus.SUCCESS,result={'status':'success','result':{
            'possession':'holding_assumed','possession_sensor_verified':False}})
    agent = Agent(SimpleNamespace(decision=decision),ToolCatalog([{'name':'take_from_hand'}]),
        SimpleNamespace(execute_tool_call=execute),PlannerTelemetryClient(enabled=False))
    context = ExecutionContext('Take an item from my hand')
    context.user_language = language
    try:
        status,answer,_,_ = await agent.run(context)
        assert status == ExecutionStatus.SUCCESS and phrase in answer and 'definitely' not in answer
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_start_speech_precedes_dispatch_and_blocked_actions_are_silent():
    order = []
    async def call(*_args):
        order.append('dispatch')
        return {'status': 'success'}
    context = SimpleNamespace(announce_action_start=lambda _call: order.append('announce'))
    executor = ToolExecutor(SimpleNamespace(call_tool=call), context)
    await executor.execute_tool_call(ToolCall('move_to_waypoint'))
    assert order == ['announce', 'dispatch']
    executor.recovery_required = True
    await executor.execute_tool_call(ToolCall('move_to_waypoint'))
    assert order == ['announce', 'dispatch']


def test_server_speech_can_arrive_late_but_cannot_survive_stop():
    node = LLMNode('out', 'commands', 'input', 'info', 'response')
    context = ExecutionContext('hello', request_id='speech-1')
    node.begin_request(context)
    node.end_request(context)
    message = String(data=json.dumps({'type': 'tts_request', 'text': 'Hallo', 'lang': 'de',
                                     'request_id': 'speech-1', 'issued_at': context.issued_at}))
    node.response_callback(message)
    assert json.loads(node.communication_publisher.published_messages[-1].data)['lang'] == 'de'
    stop_time = int(context.issued_at) + 1
    node.global_stop_callback(SimpleNamespace(stamp=SimpleNamespace(sec=stop_time, nanosec=0), reason='test'))
    node.response_callback(message)
    assert len(node.communication_publisher.published_messages) == 1


@pytest.mark.asyncio
async def test_stop_interrupts_announcement_translation_without_waiting_for_the_model():
    context = UserContext(SimpleNamespace(llm_callable_tools=[]), client=SimpleNamespace())
    execution = ExecutionContext('move')
    context.executor.execution_context = execution
    context.user_language = 'de'
    entered, release = threading.Event(), threading.Event()
    def translate(*_args, **_kwargs):
        entered.set()
        release.wait(1)
        return 'Veraltete Antwort'
    context.llm.text = translate
    task = asyncio.create_task(context._translate_to_user_language('Done.'))
    try:
        deadline = asyncio.get_running_loop().time() + 1
        while not entered.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(.01)
        assert entered.is_set()
        execution.cancel_event.set()
        assert await asyncio.wait_for(task, .5) == ''
    finally:
        release.set()
        context.agent.close()


@pytest.mark.asyncio
async def test_planning_timeout_after_success_preserves_the_known_completed_step():
    decisions = 0
    agent = None
    def decide(messages, tools):
        nonlocal decisions
        decisions += 1
        if decisions == 1:
            action = AgentDecision('call_tool', tool='move_to_waypoint')
        else:
            threading.Event().wait(.05)
            action = AgentDecision('finish')
        return AgentTurn(action, '{}', messages, PlannerPromptMetrics('test', 1024), tools)
    async def execute(call):
        agent.decision_timeout_s = .005
        return ToolResult(call.name, ExecutionStatus.SUCCESS)
    agent = Agent(SimpleNamespace(decision=decide), ToolCatalog([{'name': 'move_to_waypoint'}]),
        SimpleNamespace(execute_tool_call=execute), PlannerTelemetryClient(enabled=False))
    try:
        status, response, _, results = await agent.run(ExecutionContext('go to the desk'))
    finally:
        agent.close()
    assert status == ExecutionStatus.TIMEOUT and results[-1].success
    assert 'last step finished' in response
    assert 'could not confirm' not in response

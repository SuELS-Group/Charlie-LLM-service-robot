import pytest
from llm_test_helpers import _workspace  # noqa: F401 - installs test import paths
from agent import Agent
from models import (AgentDecision, AgentTurn, ExecutionContext, ExecutionStatus, PlannerPromptMetrics, ToolResult)
from planner_telemetry import PlannerTelemetryClient
from tool_catalog import ToolCatalog
from workstation_common.interaction import handoff_target_person


@pytest.mark.parametrize('person,utterance,expected',[
    ('TestUserB', '[User TestUserB]: Charlie, gib mir den Gegenstand.', None),
    ('TestUserA', '[User TestUserB]: Charlie, nimm es aus TestUserAs Hand.', 'TestUserA'),
    ('TestUserB', '[User TestUserA]: Charlie, gib TestUserB den Gegenstand.', 'TestUserB'),
])
def test_handover_target_uses_spoken_name_or_nearest_instead_of_speaker_metadata(person,utterance,expected):
    assert handoff_target_person(person,utterance) == expected


def test_an_unspoken_non_speaker_name_is_replanned_instead_of_executed():
    with pytest.raises(ValueError,match='not spoken'):
        handoff_target_person('TestUserC','[User TestUserB]: Charlie, gib mir das Item.')


def test_a_name_given_in_the_current_clarification_is_an_explicit_target():
    assert handoff_target_person('TestUserA','Charlie, gib es der Person.', ['[User TestUserB]: TestUserA.']) == 'TestUserA'


@pytest.mark.parametrize('tool',['take_from_hand','give_to_person'])
def test_actual_planning_boundary_discards_a_speaker_only_handover_target(tool):
    from types import SimpleNamespace
    llm = RepeatingLLM(tool=tool,args={'person':'TestUserB'})
    agent = Agent(llm,ToolCatalog([{'name':tool}]),SimpleNamespace(),PlannerTelemetryClient(enabled=False))
    try:
        turn = agent._decide(ExecutionContext('[User TestUserB]: Charlie, gib mir das Item.'),agent.catalog.tools,[])
        assert turn.decision.args['person'] is None
    finally:
        agent.close()


class RepeatingLLM:

    def __init__(self, tool='drive_to_person', args=None):
        self.tool = tool
        self.args = args or {'person': 'TestUserC'}
        self.decisions = 0
        self.text_prompts = []

    def decision(self, messages, available_tools):
        self.decisions += 1
        metrics = PlannerPromptMetrics(model='test', max_context_tokens=1024)
        return AgentTurn(decision=AgentDecision(action='call_tool', tool=self.tool, args=self.args), raw_response='{}', messages=messages, metrics=metrics, available_tools=available_tools)

    def text(self, system_prompt, *_args, **_kwargs):
        self.text_prompts.append(system_prompt)
        return 'The robot action failed.'


class ResultExecutor:

    def __init__(self, status):
        self.status = status
        self.calls = 0

    async def execute_tool_call(self, call):
        self.calls += 1
        return ToolResult(tool_name=call.name, status=self.status, error='Timeout waiting for action result' if self.status == ExecutionStatus.TIMEOUT else 'Action failed', args=call.args)


def make_agent(status):
    llm = RepeatingLLM()
    catalog = ToolCatalog([{
        'name': 'drive_to_person',
        'description': 'Drive to a named person',
        'parameters': [{'name': 'person', 'type': 'str', 'required': False}],
        'meta': {'category': 'navigation'}, }])
    executor = ResultExecutor(status)
    agent = Agent(llm, catalog, executor, telemetry=PlannerTelemetryClient(enabled=False))
    agent.max_steps = 10
    agent.max_identical_calls = 2
    agent.max_same_failed_call_attempts = 2
    return agent, executor, llm


@pytest.mark.asyncio
async def test_timeout_is_terminal_and_is_not_retried():
    agent, executor, llm = make_agent(ExecutionStatus.TIMEOUT)
    status, _message, steps, results = await agent.run(ExecutionContext(user_request='Drive to TestUserC'))
    assert status == ExecutionStatus.TIMEOUT
    assert executor.calls == 1
    assert llm.decisions == 1
    assert len(steps) == len(results) == 1
    assert llm.text_prompts == []


@pytest.mark.asyncio
async def test_same_failed_action_is_limited_to_two_attempts():
    agent, executor, llm = make_agent(ExecutionStatus.FAILURE)
    status, _message, steps, results = await agent.run(ExecutionContext(user_request='Drive to TestUserC'))
    assert status == ExecutionStatus.FAILURE
    assert executor.calls == 2
    assert llm.decisions == 2
    assert len(steps) == len(results) == 2


@pytest.mark.asyncio
async def test_nonretryable_failure_is_terminal():
    agent, executor, llm = make_agent(ExecutionStatus.FAILURE)
    original = executor.execute_tool_call
    async def nonretryable(call):
        result = await original(call)
        result.error_details = {'retryable': False}
        return result
    executor.execute_tool_call = nonretryable
    status, _, _, _ = await agent.run(ExecutionContext(user_request='Drive to TestUserC'))
    assert status == ExecutionStatus.FAILURE
    assert executor.calls == llm.decisions == 1


@pytest.mark.asyncio
async def test_compound_request_stops_at_driver_runstop_without_replaying_or_driving():
    from types import SimpleNamespace
    plan = [{'id': str(index), 'label': label, 'completion_tool': tool} for index, (label, tool) in enumerate([
        ('Akkustand nennen', 'battery_status'), ('Rechts drehen', 'turn_to_relative'),
        ('Vorwärts fahren', 'move_relative'), ('Links drehen', 'turn_to_relative'),
        ('Akkustand erneut nennen', 'battery_status')])]
    decisions = iter([AgentDecision('call_tool', tool='battery_status', task_id='0', task_plan=plan),
        AgentDecision('call_tool', tool='turn_to_relative', args={'angle': -90}, task_id='1')])
    def decide(messages, tools):
        return AgentTurn(next(decisions), '{}', messages, PlannerPromptMetrics('test', 65536), tools)
    calls = []
    async def execute(call):
        calls.append(call.name)
        if call.name == 'battery_status':
            return ToolResult(call.name, ExecutionStatus.SUCCESS, announcement='Akku 96 Prozent')
        return ToolResult(call.name, ExecutionStatus.FAILURE,
            result={'result': {'phase': 'head_preparation', 'base_started': False}},
            error='Cannot execute goals while in mode=runstopped',
            error_details={'code': 'RUNSTOP_ACTIVE', 'retryable': False, 'execution_uncertain': False})
    catalog = ToolCatalog([{'name': 'battery_status'}, {'name': 'move_relative'},
        {'name': 'turn_to_relative', 'parameters': [{'name': 'angle', 'type': 'int', 'required': True}]}])
    agent = Agent(SimpleNamespace(decision=decide), catalog, SimpleNamespace(execute_tool_call=execute),
        PlannerTelemetryClient(enabled=False))
    context = ExecutionContext('Charlie, Batteriestatus, 90 Grad rechts, 1 Meter vorwärts, 90 Grad links, Batteriestatus')
    context.user_language = 'de'
    try:
        status, message, _, _ = await agent.run(context, {'announce': lambda text: None})
        assert status == ExecutionStatus.FAILURE and 'Sicherheitsstopp' in message
        assert calls == ['battery_status', 'turn_to_relative'] and context.planning_decisions == 2
        assert [task['status'] for task in context.task_progress] == ['completed', 'failed', 'pending', 'pending', 'pending']
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_uncertain_failure_speaks_without_another_model_call():
    agent, executor, llm = make_agent(ExecutionStatus.FAILURE)
    original = executor.execute_tool_call
    async def uncertain(call):
        result = await original(call)
        result.error_details = {'retryable': False, 'execution_uncertain': True}
        return result
    executor.execute_tool_call = uncertain
    context = ExecutionContext(user_request='Drive to TestUserC')
    context.user_language = 'de'
    status, message, _, _ = await agent.run(context)
    assert status == ExecutionStatus.FAILURE
    assert 'nicht bestätigen' in message
    assert 'nicht automatisch' in message
    assert llm.text_prompts == []


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', ['query_knowledge_base', 'recall_memory'])
async def test_retrieval_is_not_announced_as_raw_data(tool):
    class RetrievalLLM(RepeatingLLM):
        def decision(self, messages, available_tools):
            self.decisions += 1
            decision = AgentDecision(action='call_tool', tool=tool, args={'query': 'lab'}, should_announce=True) if self.decisions == 1 else AgentDecision(action='answer', answer='The lab is upstairs.')
            return AgentTurn(decision=decision, raw_response='{}', messages=messages,
                             metrics=PlannerPromptMetrics(model='test', max_context_tokens=1024), available_tools=available_tools)
    class RetrievalExecutor:
        async def execute_tool_call(self, call):
            return ToolResult(tool_name=call.name, status=ExecutionStatus.SUCCESS,
                              result={'chunks': [{'text': 'raw document', 'distance': .123}]}, has_return_data=True)
    catalog = ToolCatalog([{'name': tool, 'description': 'Retrieve lab facts',
                           'parameters': [{'name': 'query', 'type': 'str', 'required': True}]}])
    agent = Agent(RetrievalLLM(), catalog, RetrievalExecutor(), telemetry=PlannerTelemetryClient(enabled=False))
    announcements = []
    status, answer, _, _ = await agent.run(ExecutionContext(user_request='Where is the lab?'), {'announce': announcements.append})
    assert status == ExecutionStatus.SUCCESS
    assert answer == 'The lab is upstairs.'
    assert announcements == []

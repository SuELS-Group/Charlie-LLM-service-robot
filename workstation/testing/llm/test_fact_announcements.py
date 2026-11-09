import json
from types import SimpleNamespace

import httpx
import pytest
from llm_test_helpers import _workspace
from communication_node import LLMNode
from models import AgentDecision, AgentTurn, ExecutionStatus, PlannerPromptMetrics
from planner_telemetry import PlannerTelemetryClient
from user_context import UserContext


@pytest.fixture
def dialogue():
    contexts = []
    def make(decisions, responses=None):
        calls, prompts = [], []
        responses = responses or {
            'battery_status': {'status': 'success', 'result': {'percentage': 85.3},
                               'meta': {'announcement': 'Battery is at 85.3%.'}},
            'current_time': {'status': 'success', 'result': {'timestamp': '15:31:00'},
                             'meta': {'announcement': "It's 15:31:00."}},
        }
        async def call(name, args):
            calls.append(name)
            return responses[name]
        node = LLMNode('out', 'commands', 'input', 'info', 'response')
        context = UserContext(SimpleNamespace(llm_callable_tools=[{'name': name} for name in responses],
            call_tool=call), node, client=SimpleNamespace())
        context.executor.recovery_path = None
        context.executor.recovery_required = False
        context.agent.telemetry = PlannerTelemetryClient(enabled=False)
        sequence = iter(decisions)
        def decide(messages, tools):
            prompts.append(json.loads(messages[-1]['content']))
            decision = next(sequence)
            if isinstance(decision, Exception):
                raise decision
            if callable(decision):
                decision = decision(context)
            return AgentTurn(decision, '{}', messages, PlannerPromptMetrics('test', 1024), tools)
        context.llm.decision = decide
        context.llm.text = lambda *_a, **_k: pytest.fail('Unexpected model text/translation call')
        contexts.append(context)
        return context, node, calls, prompts
    yield make
    for context in contexts:
        context.agent.close()


async def process_and_publish(context, node, request):
    answer = await context.process(request)
    if answer:
        node.publish_message(answer, context.user_language, generation=context.response_generation)
    return [json.loads(message.data)['text'] for message in node.communication_publisher.published_messages]


@pytest.mark.asyncio
async def test_consecutive_fact_lookups_produce_one_combined_final_reply(dialogue):
    final = 'My battery is at 85.3 percent and the time is 15:31.'
    context, node, calls, prompts = dialogue([
        AgentDecision('call_tool', tool='battery_status', should_announce=True),
        AgentDecision('call_tool', tool='current_time', should_announce=True),
        AgentDecision('finish', answer=final)])
    spoken = await process_and_publish(context, node, 'Charlie tell me the battery level and time')
    assert spoken == [final] and calls == ['battery_status', 'current_time']
    assert all(result['announcement_pending'] for result in prompts[2]['current_task_results'])


@pytest.mark.asyncio
async def test_stop_discards_queued_fact_announcement(dialogue):
    def stop(context):
        context.executor.execution_context.cancel_event.set()
        return AgentDecision('finish', answer='GREEN')
    context, node, _, _ = dialogue([
        AgentDecision('call_tool', tool='battery_status', should_announce=True), stop])
    spoken = await process_and_publish(context, node, 'Charlie tell me your battery level')
    assert context.task_history[-1].status == ExecutionStatus.STOPPED
    assert len(spoken) == 1 and '85.3' not in spoken[0] and 'GREEN' not in spoken[0]
    assert not context.all_results[0].announcement_pending
    assert not context.all_results[0].announcement_submitted


@pytest.mark.asyncio
async def test_battery_failure_emits_only_the_failure(dialogue):
    context, node, calls, _ = dialogue([
        AgentDecision('call_tool', tool='battery_status', should_announce=True)], {
        'battery_status': {'status': 'failure', 'error': {'message': 'Battery data unavailable', 'retryable': False}}})
    spoken = await process_and_publish(context, node, 'Charlie tell me your battery level')
    assert len(spoken) == 1 and 'GREEN' not in spoken[0]
    assert calls == ['battery_status'] and context.task_history[-1].status == ExecutionStatus.FAILURE


@pytest.mark.asyncio
@pytest.mark.parametrize('final', ['The item has been handed over.', ''])
async def test_final_action_completion_and_final_answer_are_spoken_once(dialogue, final):
    context, node, calls, _ = dialogue([
        AgentDecision('call_tool', tool='give_to_person', should_announce=True),
        AgentDecision('finish', answer=final)], {'give_to_person': {
            'status': 'success', 'meta': {'announcement': 'The item has been handed over.'}}})
    spoken = await process_and_publish(context, node, 'Charlie give the item to the person')
    # The start announcement is separate from the sole completion utterance.
    assert spoken.count('The item has been handed over.') == 1
    assert calls == ['give_to_person']


@pytest.mark.asyncio
async def test_intermediate_action_is_announced_before_next_action_and_final_is_not_duplicated(dialogue):
    context, node, calls, _ = dialogue([
        AgentDecision('call_tool', tool='wave_hand', should_announce=True),
        AgentDecision('call_tool', tool='stow_arm', should_announce=True),
        AgentDecision('finish', answer='My arm is stowed.')], {
            'wave_hand': {'status': 'success', 'meta': {'announcement': 'I waved.'}},
            'stow_arm': {'status': 'success', 'meta': {'announcement': 'My arm is stowed.'}}})
    spoken = await process_and_publish(context, node, 'Charlie wave and then stow your arm')
    assert spoken.count('I waved.') == 1 and spoken.count('My arm is stowed.') == 1
    assert spoken.index('I waved.') < spoken.index('My arm is stowed.')
    assert calls == ['wave_hand', 'stow_arm']

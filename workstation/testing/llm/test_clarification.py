import json
import time
from types import SimpleNamespace

import pytest
from llm_test_helpers import _workspace
from communication_node import LLMNode
from models import AgentDecision, AgentTurn, ExecutionStatus, PlannerPromptMetrics
from planner_telemetry import PlannerTelemetryClient
from planner_live_view import PlannerLiveView
from user_context import UserContext
from workstation_common.interaction import matching_voice_window


@pytest.fixture
def make_context():
    contexts = []
    def make(decisions):
        calls, prompts = [], []
        async def call(name, args):
            calls.append((name, args))
            return {'status': 'success', 'result': {'arrived': True}}
        mcp = SimpleNamespace(llm_callable_tools=[{'name': 'move_to_waypoint',
            'parameters': [{'name': 'name', 'required': True}]}], call_tool=call)
        node = LLMNode('out', 'commands', 'input', 'info', 'response', PlannerLiveView(enabled=True))
        context = UserContext(mcp, node, client=SimpleNamespace())
        node.user_context = context
        context.executor.recovery_path = None
        context.executor.recovery_required = False
        context.executor.recovery_cause = None
        context.agent.telemetry = PlannerTelemetryClient(enabled=False)
        sequence = iter(decisions)
        def decide(messages, tools):
            prompts.append(json.loads(messages[-1]['content']))
            return AgentTurn(next(sequence), '{}', messages, PlannerPromptMetrics('test', 1024), tools)
        context.llm.decision = decide
        context.llm.text = lambda *_a, **_k: pytest.fail('Unexpected model text call')
        contexts.append(context)
        return context, node, calls, prompts
    yield make
    for context in contexts:
        context.agent.close()


def message(text, reply_id=None, speaker='TestUserC'):
    return json.dumps({'text': f'[User {speaker}]: {text}' if speaker else text,
                      'lang': 'de', 'clarification_id': reply_id})


@pytest.mark.asyncio
async def test_clarification_resumes_original_task_and_preserves_completed_steps(make_context):
    ctx, node, calls, prompts = make_context([
        AgentDecision('call_tool', tool='move_to_waypoint', args={'name': 'entry'}),
        AgentDecision('ask_user', answer='Welches Büro?', reasoning='Destination is missing'),
        AgentDecision('call_tool', tool='move_to_waypoint', args={'name': 'office_b'}),
        AgentDecision('finish', answer='Büro B wurde erreicht.')])
    original = message('Charlie fahre zum Eingang und dann zu einem Büro')
    assert await ctx.process(original) == 'Welches Büro?'
    pending = ctx.pending_interaction
    assert pending and not ctx.task_history
    assert pending.context.clarification_reason == 'Destination is missing'
    assert node._waiting_context is pending.context
    snapshot = node.planner_live_view.get_snapshot()
    assert snapshot['current_session']['status'] == 'awaiting_input'
    assert snapshot['current_session']['completed_at'] is None and not snapshot['history']
    assert await ctx.process(message('Büro B', pending.context.request_id)) == 'Büro B wurde erreicht.'
    assert ctx.pending_interaction is None and node._waiting_context is None
    assert calls == [('move_to_waypoint', {'name': 'entry'}), ('move_to_waypoint', {'name': 'office_b'})]
    assert prompts[2]['request'] == json.loads(original)['text']
    assert prompts[2]['current_task_results'][0]['tool'] == 'move_to_waypoint'
    assert 'Büro B' in prompts[2]['clarification_replies'][0]['answer']
    assert len(ctx.task_history) == 1 and ctx.task_history[0].status == ExecutionStatus.SUCCESS
    assert len(ctx.task_history[0].steps) == len(ctx.all_results) == 2
    snapshot = node.planner_live_view.get_snapshot()
    assert snapshot['current_session'] is None and len(snapshot['history']) == 1
    assert len(snapshot['history'][0]['plans']) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(('original_speaker', 'reply_speaker'), [
    ('TestUserD', 'TestUserB'),
    (None, None),
])
async def test_changed_or_missing_speaker_still_resumes_the_original_question(make_context, original_speaker, reply_speaker):
    ctx, node, calls, prompts = make_context([
        AgentDecision('ask_user', answer='Welches Büro?'),
        AgentDecision('call_tool', tool='move_to_waypoint', args={'name': 'office_b'}),
        AgentDecision('finish', answer='Angekommen.')])
    await ctx.process(message('Charlie fahre zum Büro', speaker=original_speaker))
    request_id = ctx.pending_interaction.context.request_id
    reply = message('Hey, Charlie Büro B', request_id, speaker=reply_speaker)
    assert await ctx.process(reply) == 'Angekommen.'
    assert ctx.pending_interaction is None
    assert calls == [('move_to_waypoint', {'name': 'office_b'})]
    assert 'Hey, Charlie Büro B' in prompts[1]['clarification_replies'][0]['answer']
    snapshot = node.planner_live_view.get_snapshot()
    assert snapshot['history'][0]['clarifications'][0]['state'] == 'answered'
    assert snapshot['history'][0]['id'] == request_id


@pytest.mark.asyncio
async def test_reply_for_another_request_does_not_consume_the_open_question(make_context):
    ctx, node, calls, prompts = make_context([AgentDecision('ask_user', answer='Welches Büro?')])
    await ctx.process(message('Charlie fahre zum Büro'))
    request_id = ctx.pending_interaction.context.request_id
    assert 'abgelaufen' in await ctx.process(message('Hey Charlie Büro B', 'another-request', speaker=None))
    assert ctx.pending_interaction.context.request_id == request_id
    assert not calls and len(prompts) == 1
    question = node.planner_live_view.get_snapshot()['current_session']['clarifications'][0]
    assert question['state'] == 'waiting'
    assert 'Büro B' in question['rejected_replies'][0]['text']


@pytest.mark.asyncio
@pytest.mark.parametrize(('notification', 'expected'), [
    ('Battery status green. Briefly say that your battery status is GREEN.', ''),
    ('Battery status orange: Briefly say that your battery status is ORANGE. Then go to the laboratory and then dock to recharge.', 'schwach'),
])
async def test_background_battery_events_do_not_answer_the_question_or_start_navigation(make_context, notification, expected):
    structured = True
    ctx, node, calls, prompts = make_context([
        AgentDecision('ask_user', answer='Welches Büro?'),
        AgentDecision('call_tool', tool='move_to_waypoint', args={'name': 'office_b'}),
        AgentDecision('finish', answer='Angekommen.')])
    await ctx.process(message('Charlie fahre zum Büro', speaker='TestUserD'))
    pending = ctx.pending_interaction
    raw = json.dumps({'text': notification, 'lang': 'en', 'source': 'battery_monitor'}) if structured else notification
    response = await ctx.process(raw)
    assert expected in response if expected else response == ''
    assert ctx.user_language == 'de'
    assert ctx.pending_interaction is pending and node._waiting_context is pending.context
    assert not pending.context.followups and not calls and len(prompts) == 1
    assert node.planner_live_view.get_snapshot()['current_session']['status'] == 'awaiting_input'
    assert await ctx.process(message('Hey Charlie Büro B', pending.context.request_id, speaker='TestUserB')) == 'Angekommen.'
    assert calls == [('move_to_waypoint', {'name': 'office_b'})]


@pytest.mark.asyncio
async def test_new_physical_request_has_fresh_id_parameters_and_only_its_own_results(make_context):
    ctx, _, calls, prompts = make_context([
        AgentDecision('ask_user', answer='Welchen Gegenstand soll ich bringen?'),
        AgentDecision('replace_request'),
        AgentDecision('call_tool', tool='move_to_waypoint', args={'name': 'entrance'}),
        AgentDecision('finish', answer='Am Eingang angekommen.')])
    await ctx.process(message('Charlie bring mir einen Gegenstand'))
    old = ctx.pending_interaction.context
    assert await ctx.process(message('Charlie fahre stattdessen zum Eingang', old.request_id)) == 'Am Eingang angekommen.'
    assert calls == [('move_to_waypoint', {'name': 'entrance'})]
    assert ctx.mcp.request_id != old.request_id and ctx.mcp.request_issued_at > old.issued_at
    assert prompts[2]['clarification_replies'] == prompts[2]['current_task_results'] == []
    assert ctx.task_history[0].steps == []
    assert len(ctx.task_history[1].steps) == len(ctx.all_results) == 1


@pytest.mark.asyncio
async def test_replacement_can_ask_its_own_question_and_old_reply_id_cannot_resume_it(make_context):
    ctx, node, calls, _ = make_context([
        AgentDecision('ask_user', answer='Welches Büro?'),
        AgentDecision('replace_request'),
        AgentDecision('ask_user', answer='Welchen Gegenstand?')])
    await ctx.process(message('Charlie fahre zum Büro'))
    old_id = ctx.pending_interaction.context.request_id
    assert await ctx.process(message('Charlie bring mir lieber etwas', old_id)) == 'Welchen Gegenstand?'
    new = ctx.pending_interaction
    assert new.context.request_id != old_id and new.context.followups == []
    assert 'abgelaufen' in await ctx.process(message('Büro B', old_id))
    assert ctx.pending_interaction is new and not calls
    assert node.planner_live_view.get_snapshot()['current_session']['id'] == new.context.request_id


@pytest.mark.asyncio
async def test_unclear_reply_can_keep_clarifying_without_failure_or_motion(make_context):
    ctx, _, calls, _ = make_context([
        AgentDecision('ask_user', answer='Welches Büro?'),
        AgentDecision('ask_user', answer='Meinst du Büro A oder Büro B?')])
    await ctx.process(message('Charlie fahre zum Büro'))
    old = ctx.pending_interaction.context
    assert await ctx.process(message('Charlie, ich weiß nicht', old.request_id)) == 'Meinst du Büro A oder Büro B?'
    assert ctx.pending_interaction.context is old and not calls and not ctx.task_history


@pytest.mark.asyncio
async def test_stop_during_semantic_replacement_prevents_fresh_motion(make_context):
    ctx, node, calls, prompts = make_context([
        AgentDecision('ask_user', answer='Welcher Gegenstand?'),
        AgentDecision('replace_request'),
        AgentDecision('call_tool', tool='move_to_waypoint', args={'name': 'entrance'})])
    await ctx.process(message('Charlie bring etwas'))
    old = ctx.pending_interaction.context
    original_end = node.end_request
    def end_and_stop(context):
        original_end(context)
        if context is old:
            node.request_stop('test')
    node.end_request = end_and_stop
    assert 'abgebrochen' in await ctx.process(message('Charlie fahre zum Eingang', old.request_id))
    assert not calls and len(prompts) == 2
    assert ctx.task_history[-1].status == ExecutionStatus.STOPPED


@pytest.mark.asyncio
async def test_switch_does_not_clear_an_existing_physical_recovery_block(make_context):
    ctx, _, calls, _ = make_context([
        AgentDecision('ask_user', answer='Welcher Gegenstand?'),
        AgentDecision('replace_request'),
        AgentDecision('call_tool', tool='move_to_waypoint', args={'name': 'entrance'})])
    await ctx.process(message('Charlie bring etwas'))
    ctx.executor.recovery_required = True
    await ctx.process(message('Charlie fahre lieber zum Eingang', ctx.pending_interaction.context.request_id))
    assert not calls and ctx.executor.recovery_required
    assert ctx.task_history[0].status == ExecutionStatus.SUPERSEDED
    assert ctx.task_history[-1].status != ExecutionStatus.SUCCESS


@pytest.mark.asyncio
async def test_expired_answer_cannot_execute_an_old_task(make_context):
    ctx, node, calls, _ = make_context([AgentDecision('ask_user', answer='Welches Büro?')])
    await ctx.process(message('Charlie fahre zum Büro'))
    pending = ctx.pending_interaction
    pending.context.clarification_deadline = time.monotonic() - 1
    node.expire_clarification_window()
    assert 'abgelaufen' in await ctx.process(message('Büro B', pending.context.request_id))
    assert not calls and ctx.pending_interaction is None
    assert ctx.task_history[0].status == ExecutionStatus.TIMEOUT


@pytest.mark.asyncio
async def test_stop_invalidates_pending_question(make_context):
    ctx, node, calls, _ = make_context([AgentDecision('ask_user', answer='Welches Büro?')])
    await ctx.process(message('Charlie fahre zum Büro'))
    pending = ctx.pending_interaction
    node.request_stop('test')
    assert pending.context.is_cancelled() and node._waiting_context is None
    await ctx.process(message('Büro B', pending.context.request_id))
    assert not calls and ctx.pending_interaction is None
    assert ctx.task_history[0].status == ExecutionStatus.STOPPED


@pytest.mark.asyncio
async def test_refusal_and_orphan_yes_do_not_execute_and_a_new_request_can_succeed(make_context):
    ctx, _, calls, prompts = make_context([
        AgentDecision('refuse', answer='Ich kann keine Treppen steigen.'),
        AgentDecision('answer', answer='Hallo!')])
    assert 'Treppen' in await ctx.process(message('Charlie steig die Treppe hoch'))
    assert ctx.task_history[-1].status == ExecutionStatus.REFUSED
    assert 'keine Bestätigung' in await ctx.process(message('Charlie ja'))
    assert len(prompts) == 1 and not calls
    assert await ctx.process(message('Charlie sag Hallo')) == 'Hallo!'
    assert ctx.task_history[-1].status == ExecutionStatus.SUCCESS


@pytest.mark.parametrize('expected,wake,issued,now,match', [
    ('testuserc', True, 110, 115, True),
    ('other', True, 110, 115, True),
    ('testuserc', False, 110, 115, False),
    ('testuserc', True, 99, 115, False),
    ('testuserc', True, 110, 130, False),
    ('testuserc', True, float('nan'), 115, False),
])
def test_voice_window_requires_freshness_and_charlie_independent_of_speaker(expected, wake, issued, now, match):
    window = {'request_id': 'task', 'speaker': expected, 'opened_at': 100, 'expires_at': 130}
    assert matching_voice_window(window, wake, issued, now) is match

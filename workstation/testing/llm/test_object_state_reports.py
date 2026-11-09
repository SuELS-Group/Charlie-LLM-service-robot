import pytest

from models import AgentDecision
from test_clarification import make_context, message
from workstation_common.interaction import gripper_possession_report, gripper_possession_query


LIVE_CORRECTIONS = [
    'Charlie? Doch, du hältst dein Item, Charlie.',
    'Und ich sage dir, du hältst jetzt dein Item. Das ist ein Befehl. '
    'Du hältst jetzt dein Item. Aktualisiere deinen Haltestatus. '
    'Du hältst jetzt dein Item, Charlie.',
]


@pytest.mark.parametrize('text,state', [
    ('Charlie, du hältst ein Item.', 'holding_assumed'),
    ('Charlie, dein Greifer ist leer.', 'empty'),
    ('Charlie, you are holding an item.', 'holding_assumed'),
    ("Charlie, you aren't holding anything anymore.", 'empty'),
])
def test_spoken_corrections_preserve_the_asserted_state(text, state):
    assert gripper_possession_report(text) == state
    assert not gripper_possession_query(text)


@pytest.mark.parametrize('text', [
    'Charlie, hältst du jetzt noch ein Item?',
    'Charlie? Are you still holding an item?',
])
def test_spoken_questions_never_turn_into_corrections(text):
    assert gripper_possession_query(text)
    assert gripper_possession_report(text) is None


@pytest.mark.parametrize('text', [
    'Du hältst ein Item. Öffne den Greifer.',
    'Du hältst ein Item. Du hältst kein Item.',
    'Wenn dein Greifer leer ist. Du hältst kein Item.',
    'Ich glaube, du hältst ein Item. Aktualisiere deinen Haltestatus.',
    'Charlie? Sag "du hältst dein Item".',
    'Charlie, du hältst dein Wort.',
])
def test_correction_wrappers_do_not_authorize_ambiguous_or_non_state_writes(text):
    assert gripper_possession_report(text) is None


@pytest.mark.parametrize('text', [
    'Charlie, du hältst einen Gegenstand.',
    'Charlie, dein Greifer ist belegt.',
    'Charlie, dein Greifer ist nicht leer.',
    'Charlie, you are holding an item.',
])
def test_positive_human_possession_report_is_recognized(text):
    assert gripper_possession_report(text) == 'holding_assumed'


@pytest.mark.parametrize('text', [
    'Hältst du einen Gegenstand?',
    'Charlie, du hältst vielleicht einen Gegenstand.',
    'Wenn du einen Gegenstand hältst, fahre los.',
    'Charlie, du hältst einen Gegenstand und gib ihn mir.',
    'Ich halte einen Gegenstand.',
    'Er sagt: Du hältst einen Gegenstand.',
    'You are not holding a red cup.',
])
def test_ambiguous_or_non_object_positive_phrases_do_not_mutate_possession(text):
    assert gripper_possession_report(text) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('state,phrase', [('empty','als leer vermerkt'),
    ('holding_assumed','einen Gegenstand halte'), ('unknown','Greiferzustand ist unklar')])
async def test_possession_answer_reads_robot_instead_of_history_or_model(make_context, state, phrase):
    ctx, _, calls, prompts = make_context([])
    async def read(name, args):
        calls.append((name,args))
        return {'status':'success','result':{'possession':state,'possession_sensor_verified':False}}
    ctx.mcp.call_tool = read
    answer = await ctx.process(message('Charlie, hast du etwas in der Hand?', reply_id='old-question'))
    assert phrase in answer and calls == [('get_transport_state', {})] and not prompts


@pytest.mark.asyncio
@pytest.mark.parametrize('correction', LIVE_CORRECTIONS)
async def test_live_correction_overrides_calibration_and_queries_follow_both_directions(make_context, correction):
    ctx, _, calls, prompts = make_context([])
    state = {'possession': 'empty', 'possession_source': 'completed_arm_calibration'}
    async def execute(name, args):
        calls.append((name, args))
        if name in {'report_holding_gripper', 'report_empty_gripper'}:
            empty = name == 'report_empty_gripper'
            state.update(possession='empty' if empty else 'holding_assumed',
                possession_source='explicit_user_empty_report' if empty else 'explicit_user_holding_report',
                human_report_recorded=True)
        return {'status': 'success', 'result': dict(state)}
    ctx.mcp.call_tool = execute
    question = 'Charlie, hältst du jetzt noch ein Item?'
    assert 'als leer vermerkt' in await ctx.process(message(question))
    assert 'Korrektur übernommen' in await ctx.process(message(correction))
    assert 'einen Gegenstand halte' in await ctx.process(message(question))
    assert 'Korrektur übernommen' in await ctx.process(message('Charlie? Nein, du hältst kein Item mehr.'))
    assert 'als leer vermerkt' in await ctx.process(message(question))
    assert [name for name, _ in calls] == ['get_transport_state', 'report_holding_gripper',
        'get_transport_state', 'report_empty_gripper', 'get_transport_state']
    assert not prompts  # A model must not dispute a human correction using the old ledger.


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['busy', 'missing_proof', 'wrong_source', 'transport'])
async def test_missing_state_write_never_receives_a_false_acknowledgement(make_context, failure):
    ctx, _, calls, prompts = make_context([])
    async def record(name, args):
        calls.append((name, args))
        if failure == 'transport': raise TimeoutError('state service unavailable')
        if failure == 'busy': return {'status':'failure','error':{'code':'OBJECT_STATE_BUSY','retryable':False}}
        return {'status':'success','result':{'possession':'empty',
            'possession_source':'startup_assumed_empty' if failure == 'wrong_source' else 'explicit_user_empty_report'}}
    ctx.mcp.call_tool = record
    answer = await ctx.process(message('Charlie, dein Greifer ist leer.'))
    assert 'übernommen' not in answer
    assert ('nicht gespeichert' if failure == 'busy' else 'nicht speichern') in answer
    assert len(calls) == 1 and not prompts


@pytest.mark.asyncio
async def test_report_replaces_an_unrelated_question_without_resuming_old_motion(make_context):
    ctx, _, calls, prompts = make_context([AgentDecision('ask_user',answer='Welches Büro?')])
    await ctx.process(message('Charlie, fahre zum Büro.'))
    async def record(name, args):
        calls.append((name,args))
        return {'status':'success','result':{'possession':'empty','possession_source':'explicit_user_empty_report',
            'human_report_recorded':True}}
    ctx.mcp.call_tool = record
    answer = await ctx.process(message('Charlie, dein Greifer ist leer.'))
    assert 'übernommen' in answer and ctx.pending_interaction is None
    assert calls == [('report_empty_gripper', {})] and len(prompts) == 1

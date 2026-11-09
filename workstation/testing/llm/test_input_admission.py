import json
import time

import pytest
from llm_test_helpers import _workspace
from communication_node import LLMNode
from models import ExecutionContext
from std_msgs.msg import String


def node():
    return LLMNode('out', 'commands', 'input', 'info', 'response')


def voice(text='Charlie grabbing point', **kwargs):
    return String(data=json.dumps({'text': text, 'lang': 'de', 'issued_at': time.time(), **kwargs}))


@pytest.mark.parametrize('incoming', ['voice', 'plain'])
def test_busy_input_gets_prompt_feedback_and_is_not_replayed(incoming):
    n = node()
    context = ExecutionContext('go to a waypoint')
    n.begin_request(context)
    msg = voice() if incoming == 'voice' else String(data='Charlie grabbing point')
    for _ in range(3):
        n.robot_message_callback(msg)
    assert not n.pending_user_messages
    assert len(n.communication_publisher.published_messages) == 1
    assert not n.function_call_publisher.published_messages
    n.end_request(context)
    n.robot_message_callback(voice('Charlie wie geht es dir?'))
    assert len(n.pending_user_messages) == 1
    assert 'wie geht' in n.wait_for_user_message()


def test_delayed_asr_from_the_previous_task_cannot_become_a_new_motion_request():
    n = node()
    context = ExecutionContext('choose a waypoint')
    n.begin_request(context)
    spoken_during_task = time.time()
    n.end_request(context)
    n.robot_message_callback(voice(issued_at=spoken_during_task))
    assert not n.pending_user_messages
    assert len(n.communication_publisher.published_messages) == 1
    assert not n.function_call_publisher.published_messages


@pytest.mark.parametrize('structured', [False, True])
def test_monitor_alerts_never_enter_the_user_queue(structured):
    n = node()
    context = ExecutionContext('current task')
    n.begin_request(context)
    text = 'Battery status green. Briefly say that your battery status is GREEN.'
    data = json.dumps({'text': text, 'lang': 'en', 'issued_at': time.time(), 'source': 'battery_monitor'}) if structured else text
    n.robot_message_callback(String(data=data))
    n.robot_message_callback(voice())
    n.end_request(context)
    assert not n.pending_user_messages
    assert n.system_status_snapshot()['battery']['level']=='green'
    assert n.system_status_snapshot()['battery']['fresh'] is False


def test_invalid_input_timestamp_is_ignored():
    n = node()
    n.robot_message_callback(voice(issued_at=float('nan')))
    assert not n.pending_user_messages


def test_human_corrections_are_queued_during_a_task_and_latest_report_wins():
    n = node()
    context = ExecutionContext('current pickup')
    n.begin_request(context)
    n.robot_message_callback(voice('Charlie, dein Greifer ist leer.'))
    n.robot_message_callback(voice('Charlie, du hältst ein Item.'))
    assert len(n.pending_user_messages) == 1
    assert 'hältst ein Item' in json.loads(n.pending_user_messages[0][0])['text']
    assert 'vorgemerkt' in n.communication_publisher.published_messages[-1].data
    assert not n.function_call_publisher.published_messages
    assert not context.is_cancelled()
    n.end_request(context)
    assert len(n.pending_user_messages) == 1
    assert 'hältst ein Item' in json.loads(n.wait_for_user_message())['text']


def test_out_of_order_report_cannot_replace_a_newer_queued_correction():
    n = node()
    captured = time.time()
    n.robot_message_callback(voice('Charlie, du hältst ein Item.', issued_at=captured))
    n.robot_message_callback(voice('Charlie, dein Greifer ist leer.', issued_at=captured-1))
    assert len(n.pending_user_messages) == 1
    assert 'hältst ein Item' in json.loads(n.pending_user_messages[0][0])['text']


def test_global_stop_discards_queued_state_corrections():
    from types import SimpleNamespace
    n = node()
    n.begin_request(ExecutionContext('current pickup'))
    n.robot_message_callback(voice('Charlie, dein Greifer ist leer.'))
    n.global_stop_callback(SimpleNamespace(stamp=SimpleNamespace(sec=0,nanosec=0),reason='test stop'))
    assert not n.pending_user_messages

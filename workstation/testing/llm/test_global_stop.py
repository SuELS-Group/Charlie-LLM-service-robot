import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from llm_test_helpers import _workspace
from communication_node import LLMNode
from models import ExecutionContext, ExecutionStatus
from std_msgs.msg import String
from test_agent_safeguards import make_agent


def node():
    return LLMNode('out', 'commands', 'input', 'info', 'response')


def stop():
    return SimpleNamespace(stamp=SimpleNamespace(sec=100, nanosec=0), reason='test')


def test_stop_cancels_active_context_and_clears_old_queue():
    n = node()
    context = ExecutionContext('move')
    n.begin_request(context)
    n.robot_message_callback(String(data='old queued request'))
    n.global_stop_callback(stop())
    assert context.is_cancelled() and not n.pending_user_messages
    n.end_request(context)
    n.robot_message_callback(String(data='fresh request'))
    assert n.wait_for_user_message() == 'fresh request'
    fresh = ExecutionContext('fresh request')
    n.begin_request(fresh)
    assert not fresh.is_cancelled()


def test_stop_between_dequeue_and_context_binding_is_not_lost():
    n = node()
    n.robot_message_callback(String(data='move'))
    assert n.wait_for_user_message() == 'move'
    n.global_stop_callback(stop())
    context = ExecutionContext('move')
    n.begin_request(context)
    assert context.is_cancelled()


def test_late_old_input_and_response_are_discarded():
    n = node()
    n.global_stop_callback(stop())
    n.robot_message_callback(String(data=json.dumps({'text': 'old', 'issued_at': 90})))
    assert not n.pending_user_messages
    n.publish_message('old result', 'en', generation=0)
    assert not n.communication_publisher.published_messages
    n.robot_message_callback(String(data=json.dumps({'text': 'new', 'issued_at': 101})))
    assert len(n.pending_user_messages) == 1


@pytest.mark.asyncio
async def test_stop_during_planning_discards_late_decision():
    agent, executor, llm = make_agent(ExecutionStatus.SUCCESS)
    entered, release = threading.Event(), threading.Event()
    original = llm.decision
    def blocking(*args):
        entered.set()
        release.wait(1)
        return original(*args)
    llm.decision = blocking
    context = ExecutionContext('Drive to TestUserC')
    task = asyncio.create_task(agent.run(context))
    try:
        deadline = asyncio.get_running_loop().time() + 1
        while not entered.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(.01)
        assert entered.is_set()
        context.cancel_event.set()
        status, _, _, _ = await asyncio.wait_for(task, .5)
        assert status == ExecutionStatus.STOPPED and executor.calls == 0
    finally:
        release.set()


@pytest.mark.asyncio
async def test_stop_immediately_before_dispatch_prevents_call():
    agent, executor, _ = make_agent(ExecutionStatus.SUCCESS)
    context = ExecutionContext('Drive to TestUserC')
    hooks = {'phase': lambda phase, *_: context.cancel_event.set() if phase == 'executing' else None}
    status, _, steps, _ = await agent.run(context, hooks)
    assert status == ExecutionStatus.STOPPED and not steps and executor.calls == 0


@pytest.mark.asyncio
async def test_model_decision_has_an_absolute_budget():
    agent, executor, llm = make_agent(ExecutionStatus.SUCCESS)
    agent.decision_timeout_s = .01
    original = llm.decision
    def slow(*args):
        threading.Event().wait(.04)
        return original(*args)
    llm.decision = slow
    status, _, _, _ = await agent.run(ExecutionContext('Drive to TestUserC'))
    assert status == ExecutionStatus.TIMEOUT and executor.calls == 0

from __future__ import annotations

from pathlib import Path
from typing import Any, List

from workstation_common import load

from llm_client import LLMClient
from models import ExecutionContext, ExecutionPlan, ToolCall
from tool_catalog import ToolCatalog

config = load(Path(__file__).resolve().parent / 'config' / 'llm.yaml', profile_key='model_profiles')


class Planner:

    def __init__(self, client: Any):
        self.llm = LLMClient(client)

    def create_plan(self, context: ExecutionContext, available_tools: List[Any]) -> ExecutionPlan:
        catalog = ToolCatalog(available_tools)
        tools = catalog.initial_tools(context.user_request or context.english_request)
        actions = '{"action":"answer","answer":"...","reasoning":"..."} or {"action":"call_tool","tool":"name","args":{},"should_announce":false,"reasoning":"..."} or {"action":"finish","answer":"","reasoning":"..."}'
        system = str(config.prompt_planner_compat).format(action_formats=actions, available_tools=catalog.tool_cards(tools))
        turn = self.llm.decision([{'role': 'system', 'content': system},
                                  {'role': 'user', 'content': context.user_request or context.english_request}], [
                                     tool.name for tool in tools])
        steps = []
        if turn.decision.action == 'call_tool' and turn.decision.tool:
            tool = catalog.by_name.get(turn.decision.tool)
            steps.append(ToolCall(name=turn.decision.tool, args=turn.decision.args, reason=turn.decision.reasoning, should_announce=turn.decision.should_announce, returns_data=bool(tool and tool.returns_data)))
        return ExecutionPlan(reasoning=turn.decision.reasoning, steps=steps, needs_more_planning=bool(steps), metrics=turn.metrics, prompt_debug={
            'raw_response': turn.raw_response,
            'decision': turn.decision.__dict__,
            'available_tools': [tool.name for tool in tools]})

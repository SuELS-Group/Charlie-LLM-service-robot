from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any, Dict, List

from workstation_common import get_logger, load

from models import AgentDecision, AgentTurn, PlannerPromptMetrics, extract_json_value
from decision_contract import PlanningDecisionError, PlanningOutputLimitError, decision_schema, validation_error
from task_progress import goal_tool_names, task_plan_schema

config = load(Path(__file__).resolve().parent / 'config' / 'llm.yaml', profile_key='model_profiles')
log = get_logger('llm.client')
DECISION_SCHEMA = decision_schema([])


class LLMClient:

    def __init__(self, client: Any, planning_client: Any | None = None):
        self.client = client
        self.planning_client = planning_client if planning_client is not None else client
        self.tool_specs = []

    def bind_tools(self, tools):
        self.tool_specs = list(tools)

    def chat(self, model: str, messages: List[Dict[str, str]], temperature: float = 0.1, num_ctx: int | None = None, *, planning: bool = False, schema=None, max_output_tokens=None) -> \
            tuple[str, PlannerPromptMetrics]:
        ctx = int(num_ctx or config.llm_model_fast_ctx)
        client = self.planning_client if planning else self.client
        options = {'num_ctx': ctx, 'temperature': temperature}
        extra = {'think': False}
        if planning:
            options['num_predict'] = max(1, min(int(max_output_tokens or getattr(config, 'llm_planning_max_tokens', 4096)), ctx - 256))
            extra = {'format': schema or DECISION_SCHEMA, 'think': getattr(config, 'llm_planning_think', True)}
        else:
            options['num_predict'] = max(1, int(getattr(config, 'llm_text_max_tokens', 1024)))
        started = time.monotonic()
        response = client.chat(model=model, messages=messages, stream=False,
            keep_alive=config.ollama_keep_alive, options=options, **extra)
        load_duration = getattr(response, 'load_duration', None)
        load_summary = f'{load_duration / 1e9:.2f}s' if load_duration is not None else 'unknown'
        log.info(f'Model response kind={"planning" if planning else "text"} model={model} '
                 f'elapsed={time.monotonic() - started:.2f}s load={load_summary} '
                 f'prompt_tokens={getattr(response, "prompt_eval_count", None)} '
                 f'generated_tokens={getattr(response, "eval_count", None)}')
        if not planning and getattr(response, 'done_reason', None) == 'length':
            raise RuntimeError('Text generation reached its token limit; incomplete output is discarded')
        content = response.message.content or ''
        metrics = PlannerPromptMetrics(model=str(model), max_context_tokens=ctx, prompt_tokens=getattr(response, 'prompt_eval_count', None), response_tokens=getattr(response, 'eval_count', None), system_prompt_chars=sum(
            len(m.get('content', '')) for m in messages if m.get('role') == 'system'), user_prompt_chars=sum(
            len(m.get('content', '')) for m in messages if m.get('role') != 'system'),
            output_token_limit=options['num_predict'], finish_reason=getattr(response, 'done_reason', None),
            elapsed_s=round(time.monotonic() - started, 3))
        if planning and getattr(response, 'done_reason', None) == 'length':
            raise PlanningOutputLimitError('Planning output reached its token limit; truncated decisions are not executed',
                raw_response=content, metrics=metrics, messages=messages)
        return content, metrics

    def decision(self, messages: List[Dict[str, str]], available_tools: List[str], *, max_output_tokens=None) -> AgentTurn:
        try:
            state = json.loads(messages[-1]['content'])
        except (ValueError, KeyError, IndexError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        use_progress = 'task_progress' in state
        names = available_tools
        current_task = None
        if state.get('task_progress'):
            current_task = next((task for task in state['task_progress']
                if task['status'] not in {'completed', 'ready'}), None)
            allowed = goal_tool_names(state['task_progress'], {tool.name: tool for tool in self.tool_specs})
            names = [name for name in available_tools if name in allowed]
        schema = decision_schema(self.tool_specs, names,
            task_plan=task_plan_schema([tool.name for tool in self.tool_specs]) if use_progress else None,
            task_active=bool(state.get('task_progress')),
            current_task_id=current_task['id'] if current_task else None,
            knowledge_evidence=state.get('knowledge_evidence', {}) if
                state.get('knowledge_policy') == 'approved_local_excerpts_only' else None)
        raw, metrics = self.chat(str(config.llm_model_smart), messages, temperature=float(getattr(config, 'agent_temperature', 0.0)), num_ctx=int(config.llm_model_smart_ctx), planning=True, schema=schema, max_output_tokens=max_output_tokens)
        try:
            payload = extract_json_value(raw, expected_types=(dict,))
            error = validation_error(payload, schema)
        except ValueError as exc:
            error = str(exc)
        if error:
            raise PlanningDecisionError(error, raw_response=raw, metrics=metrics, messages=messages)
        action = str(payload.get('action') or '').strip()
        tool = str(payload.get('tool') or payload.get('name') or '').strip()
        decision = AgentDecision(action=action, reasoning=str(payload.get('reasoning') or '').strip(), tool=tool, args=payload.get('args') if isinstance(payload.get('args'), dict) else {}, answer=str(payload.get('answer') or '').strip(), query=str(payload.get('query') or '').strip(), categories=payload.get('categories') if isinstance(payload.get('categories'), list) else [], should_announce=bool(payload.get('should_announce', False)))
        decision.task_plan = payload.get('task_plan', [])
        decision.task_id = payload.get('task_id', '')
        decision.evidence_ids = payload.get('evidence_ids', [])
        decision.knowledge_outcome = payload.get('knowledge_outcome', '')
        return AgentTurn(decision=decision, raw_response=raw, messages=messages, metrics=metrics, available_tools=available_tools)

    def text(self, system_prompt: str, user_payload: str, model: str | None = None, temperature: float = 0.1) -> str:
        raw, _ = self.chat(model or str(config.llm_model_fast), [{'role': 'system', 'content': system_prompt},
                                                                 {
                                                                     'role': 'user',
                                                                     'content': user_payload}], temperature=temperature, num_ctx=int(config.llm_model_fast_ctx))
        return raw.strip()

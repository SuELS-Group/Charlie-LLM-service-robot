from __future__ import annotations

import json
import httpx
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from uuid import uuid4

from ollama import Client
from workstation_common import get_logger, load
from workstation_common.language import request_language
from workstation_common.interaction import interaction_message, is_bare_confirmation, monitor_message, requests_new_task, gripper_possession_report, gripper_possession_query
from workstation_common.runtime_messages import action_start_message, battery_notification, battery_notification_level, runtime_message
from workstation_common.battery_announcements import BatteryAnnouncements

from agent import Agent
from llm_client import LLMClient
from mcp_client import MCPClient
from models import CompletedStep, CompletedTask, ConversationMessage, ExecutionContext, ExecutionStatus, ToolCall, ToolResult, summarize_result_for_context
from tool_access import ToolExecutor
from tool_catalog import ToolCatalog, ToolSpec

if TYPE_CHECKING:
    from communication_node import LLMNode
config = load(Path(__file__).resolve().parent / 'config' / 'llm.yaml', profile_key='model_profiles')
log = get_logger('llm.context')


@dataclass
class PendingInteraction:
    context: ExecutionContext
    speaker: str | None
    question: str
    steps: List[ToolCall] = field(default_factory=list)
    results: List[ToolResult] = field(default_factory=list)


class UserContext:

    def __init__(self, mcp_client: MCPClient, llm_node: Optional[
        'LLMNode'] = None, client: Any | None = None, base_url: str | None = None):
        self.mcp = mcp_client
        self.llm_node = llm_node
        ollama_base_url = (base_url or str(config.ollama_base_url)).removesuffix('/v1')
        if client is None:
            self.client = Client(host=ollama_base_url,
                timeout=httpx.Timeout(float(getattr(config, 'llm_request_timeout_s', 30.0)), connect=5.0))
            planning_client = Client(host=ollama_base_url,
                timeout=httpx.Timeout(float(getattr(config, 'llm_planning_timeout_s', 120.0)), connect=5.0))
        else:
            self.client = planning_client = client
        self.llm = LLMClient(self.client, planning_client=planning_client)
        self.catalog = ToolCatalog(getattr(mcp_client, 'llm_callable_tools', []) or [])
        self.executor = ToolExecutor(mcp_client, self,
            recovery_path=getattr(config, 'recovery_state_file', None))
        self.agent = Agent(self.llm, self.catalog, self.executor)
        self.conversation_history: List[ConversationMessage] = []
        self.all_results: List[ToolResult] = []
        self.task_history: List[CompletedTask] = []
        self.user_language = 'en'
        self._battery_announcements = BatteryAnnouncements()
        self.response_generation = None
        self.pending_interaction: PendingInteraction | None = None
        self.clarification_timeout_s = min(30.0, max(1.0, float(getattr(config, 'clarification_timeout_s', 30.0))))

    async def process(self, user_input: str, *, _expected_generation: int | None = None) -> str:
        notification = monitor_message(user_input)
        pending = self.pending_interaction
        if notification is not None:
            # Background events cannot fill a missing destination or replace the
            # open human conversation. Keep warnings audible without planning a
            # separate autonomous task while waiting for the user's answer.
            self.user_language = getattr(pending.context,'user_language',self.user_language) if pending else self.user_language
            self.response_generation = self.llm_node.stop_generation if self.llm_node else None
            log.info('Handling legacy battery notification without a planner task')
            level = battery_notification_level(notification)
            allowed = (self.llm_node.battery_policy.legacy_notice(level) if self.llm_node else
                self._battery_announcements.choose(level))
            return battery_notification(notification, self.user_language) if allowed else ''
        user_input, asr_language, speaker, reply_id, new_request = interaction_message(user_input)
        log.info(f'Processing user input: {user_input}')
        self.all_results.clear()
        self.executor.reset_failures()
        if self.llm_node:
            self.response_generation = self.llm_node.stop_generation
        pending = self.pending_interaction
        expired = False
        if pending and (pending.context.is_cancelled() or time.monotonic() >= pending.context.clarification_deadline):
            expired = True
            self._close_pending(ExecutionStatus.STOPPED if pending.context.is_cancelled() else ExecutionStatus.TIMEOUT)
            pending = None
        possession_report = gripper_possession_report(user_input)
        possession_input = possession_report is not None or gripper_possession_query(user_input)
        if reply_id and not possession_input and (pending is None or reply_id != pending.context.request_id):
            return self._reject_input(user_input, asr_language, 'clarification_expired')
        new_request = new_request or requests_new_task(user_input) or possession_input
        switch_notice = ''
        previous_steps, previous_results = [], []
        if pending and not new_request:
            context = pending.context
            if self.llm_node and not self.llm_node.consume_clarification_window(context):
                self._close_pending(ExecutionStatus.STOPPED if context.is_cancelled() else ExecutionStatus.TIMEOUT)
                return self._reject_input(user_input, asr_language, 'clarification_expired')
            self.user_language = request_language(user_input, getattr(context, 'user_language', 'en'),
                                                  language_hint=asr_language)
            context.followups.append({'question': pending.question, 'answer': user_input})
            log.info('Clarification reply accepted: request_id=%s speaker=%s', context.request_id, speaker)
            context.clarification_question = ''
            previous_steps, previous_results = pending.steps, pending.results
            self.pending_interaction = None
        else:
            if pending:
                _expected_generation = pending.context.stop_generation
                switch_notice = self._close_pending(ExecutionStatus.SUPERSEDED, replacement_request=user_input)
            if is_bare_confirmation(user_input):
                return self._reject_input(user_input, asr_language,
                    'clarification_expired' if expired else 'no_pending_question')
            self.user_language = request_language(user_input, self.user_language, language_hint=asr_language)
            request_id = str(uuid4())
            context = ExecutionContext(user_request=user_input, english_request=user_input,
                request_id=request_id, planning_session_id=request_id, task_history=self.task_history.copy())
        if asr_language and asr_language != self.user_language:
            log.info('Request language corrected from %s to %s based on text', asr_language, self.user_language)
        setattr(context, 'user_language', self.user_language)
        context.system_status_provider=getattr(self.llm_node,'system_status_snapshot',None)
        if self.llm_node:
            self.llm_node.begin_request(context, expected_generation=_expected_generation)
            self.response_generation = context.stop_generation
        self.executor.execution_context = context
        self.mcp.cancel_check = context.is_cancelled
        self.mcp.request_language = self.user_language
        self.mcp.request_id = context.request_id
        self.mcp.request_issued_at = context.issued_at
        self._start_live_session(context)
        self._set_led_status('thinking')
        self.conversation_history.append(ConversationMessage(role='user', content=user_input))
        hooks = {
            'tools': lambda tools: self._set_live_tools(context, tools),
            'phase': lambda phase, detail='': self._set_live_phase(context, phase, detail),
            'plan': lambda plan: self._record_live_plan(context, plan),
            'progress': lambda progress: self.llm_node.update_planner_progress(context) if self.llm_node else None,
            'diagnostic': lambda attempt: self.llm_node.record_planning_attempt(context, attempt) if self.llm_node else None,
            'rejected': lambda reason: self.llm_node.reject_planner_decision(context, reason) if self.llm_node else None,
            'reused': lambda index, source, result: self.llm_node.mark_lookup_reused(context, index, source, result) if self.llm_node else None,
            'step_started': lambda index, step: self._mark_live_step_started(context, index, step),
            'step_completed': lambda index, result: self._mark_live_step_completed(context, index, result),
            'announce': lambda text: self._announce(text),
            'announce_grounded': lambda text: self._announce_grounded(text), }
        try:
            if switch_notice and self.llm_node:
                self.llm_node.publish_message(switch_notice, self.user_language, generation=context.stop_generation)
            status, response_en, steps, results = await self.agent.run(context, hooks)
            steps, results = [*previous_steps, *steps], [*previous_results, *results]
            self.all_results.extend(results)
            # The planner replies in the payload language; fault messages are localized templates.
            response = response_en or ''
            if status == ExecutionStatus.SUPERSEDED:
                context.replacement_request = user_input
                log.info('Replacing pending request: request_id=%s new_request=%s', context.request_id, user_input)
            if self.llm_node and status == ExecutionStatus.STOPPED and context.cancel_event.is_set():
                if self.llm_node.last_stop_ack_generation == self.llm_node.stop_generation:
                    response = ''  # The stop callback already acknowledged this cancellation.
                else:
                    response = runtime_message('cancelled', self.user_language)
                    self.response_generation = self.llm_node.stop_generation
            if status == ExecutionStatus.AWAITING_INPUT:
                context.clarification_deadline = time.monotonic() + self.clarification_timeout_s
                if self.llm_node and not self.llm_node.set_clarification_window(context, speaker,
                        self.clarification_timeout_s,
                        bool(getattr(config, 'clarification_allow_without_wake_word', False))):
                    status, response = ExecutionStatus.STOPPED, ''
                else:
                    self.pending_interaction = PendingInteraction(context, speaker, response, steps, results)
            if status != ExecutionStatus.AWAITING_INPUT:
                self._record_completed_task(context.user_request, context.english_request, steps, results,
                    response_text=response or response_en, status=status)
            self._finish_live_session(context, status, response_text=response or response_en)
            if response:
                self.conversation_history.append(ConversationMessage(role='assistant', content=response_en or response))
        except Exception as exc:
            self._finish_live_session(context, ExecutionStatus.FAILURE, error_text=str(exc))
            raise
        finally:
            if self.llm_node:
                self.llm_node.end_request(context)
            self.mcp.cancel_check = None
            self.executor.execution_context = None
            self._set_led_status('idle')
        if status == ExecutionStatus.SUPERSEDED:
            # Release the old execution/MCP ownership before starting the new
            # request. Reuse only the admitted input generation, never the old
            # task's tool results, selected tools or missing parameters.
            if self.llm_node:
                self.llm_node.publish_message(response, self.user_language, generation=context.stop_generation)
            fresh_input = json.dumps({'text': user_input, 'lang': self.user_language, 'new_request': True})
            fresh_response = await self.process(fresh_input, _expected_generation=context.stop_generation)
            return fresh_response if self.llm_node else ' '.join(filter(None, (response, fresh_response)))
        return response if self.llm_node else ' '.join(filter(None, (switch_notice, response)))

    def _reject_input(self, user_input, language, key):
        self.user_language = language or self.user_language
        response = runtime_message(key, self.user_language)
        if self.pending_interaction and self.llm_node:
            self.llm_node.record_clarification_rejection(self.pending_interaction.context, user_input, response)
        log.info('Input rejected: reason=%s text=%s', key, user_input)
        self._record_completed_task(user_input, user_input, [], [], response, ExecutionStatus.REFUSED)
        return response

    def _close_pending(self, status, replacement_request=''):
        pending = self.pending_interaction
        if pending is None:
            return
        self.pending_interaction = None
        pending.context.replacement_request = replacement_request
        key = {ExecutionStatus.TIMEOUT: 'clarification_expired', ExecutionStatus.SUPERSEDED: 'request_replaced'}.get(status, 'cancelled')
        message = runtime_message(key,
            getattr(pending.context, 'user_language', 'en'))
        self._record_completed_task(pending.context.user_request, pending.context.english_request,
            pending.steps, pending.results, message, status)
        self._finish_live_session(pending.context, status, response_text=message)
        if self.llm_node:
            self.llm_node.clear_clarification_window(pending.context)
        return message

    def get_context_summary(self) -> str:
        return self._recent_task_context(2)

    def _recent_task_context(self, max_tasks: int = 2) -> str:
        return '\n\n'.join(task.compact_summary() for task in self.task_history[-max_tasks:])

    def _detect_language(self, text: str) -> str:
        return request_language(text, self.user_language)

    async def _translate_to_user_language(self, text: str) -> str:
        if not text or not self.user_language or self.user_language == 'en':
            return text
        try:
            prompt = str(config.prompt_translation_to_user_language).format(target_lang=self.user_language)
            context = self.executor.execution_context
            if context is not None:
                translated = await self.agent.text_interruptibly(context, prompt, text, temperature=0.1)
                return '' if context.is_cancelled() else translated or text
            return self.llm.text(prompt, text, temperature=0.1) or text
        except Exception as exc:
            log.warning(f'Translation failed: {exc}')
            return text

    async def _respond(self, text_en: str) -> str:
        return await self._translate_to_user_language(text_en)

    async def _announce(self, text_en: str) -> str:
        translated = await self._respond(text_en)
        return await self._announce_grounded(translated)

    async def _announce_grounded(self, text: str) -> str:
        # Source excerpts and localized uncertainty templates must survive the
        # speech path verbatim, including for an intermediate knowledge goal.
        if self.llm_node and text:
            self.llm_node.publish_message(text, self.user_language or 'en')
            self.llm_node.flush_response_callbacks()
        return text

    def announce_action_start(self, call: ToolCall) -> None:
        if getattr(self.executor.execution_context,'request_source','')=='automatic_charging':
            return  # The deterministic workflow owns its bounded announcements.
        text = action_start_message(call.name, self.user_language)
        if text and self.llm_node:
            self.llm_node.publish_message(text, self.user_language, generation=self.response_generation)

    def _record_completed_task(self, original_request: str, english_request: str, steps: List[ToolCall], results: List[
        ToolResult], response_text: Optional[str] = None, status: ExecutionStatus = ExecutionStatus.SUCCESS) -> None:
        completed_steps = []
        for step, result in zip(steps, results):
            summary = summarize_result_for_context(result.result)
            completed_steps.append(CompletedStep(name=step.name, args=step.args or {}, status=result.status, result_summary=summary or None, error=result.error))
        self.task_history.append(CompletedTask(request=english_request, original_request=original_request, english_request=english_request, steps=completed_steps, response_text=response_text, status=status))
        self.task_history = self.task_history[-5:]

    def _start_live_session(self, context: ExecutionContext) -> None:
        if self.llm_node:
            self.llm_node.start_planner_session(context, user_language=self.user_language)

    def _set_live_tools(self, context: ExecutionContext, tools: List[ToolSpec]) -> None:
        if self.llm_node:
            raw = [{
                'name': tool.name,
                'description': tool.description,
                'parameters': tool.parameters,
                'returns': tool.returns,
                'meta': tool.meta} for tool in tools]
            self.llm_node.update_planner_tools(context, raw)

    def _set_live_phase(self, context: ExecutionContext, phase: str, detail: str = '') -> None:
        if self.llm_node:
            self.llm_node.update_planner_phase(context, phase, detail)

    def _record_live_plan(self, context: ExecutionContext, plan: Any) -> None:
        if self.llm_node:
            self.llm_node.record_plan(context, plan)

    def _mark_live_step_started(self, context: ExecutionContext, step_index: int, step: ToolCall) -> None:
        if self.llm_node:
            self.llm_node.mark_plan_step_started(context, step_index, step)

    def _mark_live_step_completed(self, context: ExecutionContext, step_index: int, result: ToolResult) -> None:
        if self.llm_node:
            self.llm_node.mark_plan_step_completed(context, step_index, result)

    def _finish_live_session(self, context: ExecutionContext, status: ExecutionStatus | str, response_text: str = '', error_text: str = '') -> None:
        if self.llm_node:
            self.llm_node.complete_planner_session(context, status.value if isinstance(status, ExecutionStatus) else status, response_text=response_text, error_text=error_text)

    def _set_led_status(self, status: str) -> None:
        if self.llm_node:
            self.llm_node.publish_led_status(status)

    async def _generate_runtime_message(self, payload: Dict[
        str, Any], instruction: str = '', fallback: str = '') -> str:
        system = str(config.prompt_base_conversation)
        if instruction:
            system += '\n' + instruction
        try:
            return self.llm.text(system, json.dumps(payload, ensure_ascii=False, default=str), temperature=0.1) or fallback
        except Exception:
            return fallback or json.dumps(payload, ensure_ascii=False, default=str)

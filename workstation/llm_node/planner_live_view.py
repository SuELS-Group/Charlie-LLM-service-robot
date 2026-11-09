from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except Exception:  # pragma: no cover - handled at runtime when dependency is missing
    websockets = None
    ConnectionClosed = Exception
from workstation_common import get_logger, load
from models import ExecutionContext, ExecutionPlan, ToolCall, ToolResult, summarize_result_for_context

config = load(Path(__file__).resolve().parent / 'config' / 'llm.yaml', profile_key='model_profiles')
log = get_logger('llm.planner.live')


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlannerLiveView:

    def __init__(self, host: str = '0.0.0.0', port: int = 8765, history_size: int = 15, enabled: bool = True, ) -> None:
        self.host = (host or '0.0.0.0').strip()
        self.port = int(port)
        self.history_size = max(1, int(history_size))
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._history: Deque[Dict[str, Any]] = deque(maxlen=self.history_size)
        self._current_session: Optional[Dict[str, Any]] = None
        self._agent_state: Dict[str, Any] = {
            'status': 'idle',
            'active_session_id': None,
            'updated_at': _utc_now_iso(), }
        self._updated_at = _utc_now_iso()
        self._client_count = 0
        self._clients: set[Any] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._ready = threading.Event()

    @classmethod
    def from_config(cls) -> 'PlannerLiveView':
        return cls(host=str(getattr(config, 'planner_live_host', '0.0.0.0') or '0.0.0.0'), port=int(getattr(config, 'planner_live_port', 8765) or 8765), history_size=int(getattr(config, 'planner_live_history_size', 15) or 15), enabled=bool(getattr(config, 'planner_live_enabled', True)), )

    def start(self) -> None:
        if not self.enabled:
            log.info('Planner live view disabled')
            return
        if websockets is None:
            self.enabled = False
            log.error('Planner live view disabled because the "websockets" package is not installed')
            return
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run_server, name='planner-live-view', daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=2.0):
            log.warning('Planner live view server did not report readiness within 2s')

    def stop(self) -> None:
        if not self._loop or self._loop.is_closed():
            return
        stop_event = self._stop_event
        if stop_event is not None:
            self._loop.call_soon_threadsafe(stop_event.set)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                log.warning('Planner live view server did not stop within 2s')
                return
        self._thread = None
        self._loop = None
        self._stop_event = None
        self._ready.clear()

    def start_session(self, context: ExecutionContext, user_language: str = '') -> None:
        if not self.enabled:
            return
        session_id = self._session_id(context)
        now = _utc_now_iso()
        with self._lock:
            if self._current_session is not None and self._current_session['id'] == session_id:
                for question in self._current_session.get('clarifications', []):
                    index = question['reply_index']
                    if question['state'] == 'waiting' and index < len(context.followups):
                        question.update({'state': 'answered', 'answer': context.followups[index]['answer'],
                            'answered_at': now})
                        self._update_question_plan_locked(self._current_session, question, 'answered')
                self._current_session.update({'status': 'active', 'phase': 'planning',
                    'phase_detail': 'Processing clarification reply', 'user_language': user_language,
                    'response_text': '', 'error_text': '', 'updated_at': now})
            else:
                self._current_session = {
                    'id': session_id,
                    'status': 'active',
                    'phase': 'planning',
                    'phase_detail': 'Analyzing request',
                    'user_request': context.user_request,
                    'english_request': context.english_request,
                    'user_language': user_language or '',
                    'started_at': now,
                    'completed_at': None,
                    'updated_at': now,
                    'selected_tools': [],
                    'plans': [],
                    'clarifications': [],
                    'task_progress': copy.deepcopy(context.task_progress),
                    'planning_error': None,
                    'planning_decisions': context.planning_decisions,
                    'response_text': '',
                    'error_text': '', }
            self._set_agent_state_locked('planning', session_id)
            self._touch_locked()
        self._broadcast_snapshot()

    def update_selected_tools(self, context: ExecutionContext, tools: List[Dict[str, Any]]) -> None:
        if not self.enabled:
            return
        tool_names = [tool.get('name', 'unknown') for tool in tools if isinstance(tool, dict)]
        with self._lock:
            session = self._require_current_session_locked(self._session_id(context))
            if session is None:
                return
            session['selected_tools'] = tool_names
            session['phase'] = 'planning'
            session['phase_detail'] = 'Creating execution plan'
            session['updated_at'] = _utc_now_iso()
            self._set_agent_state_locked('planning', session['id'])
            self._touch_locked()
        self._broadcast_snapshot()

    def update_phase(self, context: ExecutionContext, phase: str, detail: str = '') -> None:
        if not self.enabled:
            return
        with self._lock:
            session = self._require_current_session_locked(self._session_id(context))
            if session is None:
                return
            session['phase'] = phase
            session['phase_detail'] = detail
            session['updated_at'] = _utc_now_iso()
            self._set_agent_state_locked(phase, session['id'])
            self._touch_locked()
        self._broadcast_snapshot()

    def record_plan(self, context: ExecutionContext, plan: ExecutionPlan) -> None:
        if not self.enabled:
            return
        now = _utc_now_iso()
        with self._lock:
            session = self._require_current_session_locked(self._session_id(context))
            if session is None:
                return
            decision = self._serialize_decision(plan.prompt_debug)
            action = str(decision.get('action', '') or '')
            session['phase'] = 'executing' if plan.steps else 'planning'
            session['phase_detail'] = self._phase_detail_for_decision(action, plan)
            attempt = 1 + sum(item.get('iteration') == context.iteration for item in session['plans'])
            session['planning_decisions'] = context.planning_decisions
            session['plans'].append({
                'id': f'{session["id"]}:plan:{context.iteration + 1}:{attempt}',
                'iteration': int(context.iteration),
                'attempt': attempt,
                'label': f'Decision {context.iteration + 1}' + (f' · attempt {attempt}' if attempt > 1 else ''),
                'type': 'agent_decision',
                'action': action,
                'decision': decision,
                'reasoning': plan.reasoning,
                'needs_more_planning': bool(plan.needs_more_planning),
                'context_window': self._serialize_context_window(plan.metrics),
                'prompt_debug': self._serialize_prompt_debug({**(plan.prompt_debug or {}),
                    'iteration': context.iteration,
                    'max_context_tokens': getattr(plan.metrics, 'max_context_tokens', 0)}),
                'created_at': now,
                'status': {'ask_user': 'awaiting_input', 'replace_request': 'superseded'}.get(
                    action, 'running' if plan.steps else 'empty'),
                'steps': [self._serialize_step(index, step) for index, step in enumerate(plan.steps)], })
            session['updated_at'] = now
            self._set_agent_state_locked(session['phase'], session['id'])
            self._touch_locked()
        self._broadcast_snapshot()

    def update_task_progress(self, context: ExecutionContext) -> None:
        if not self.enabled:
            return
        with self._lock:
            session = self._require_current_session_locked(self._session_id(context))
            if session is None:
                return
            session['task_progress'] = copy.deepcopy(context.task_progress)
            session['planning_decisions'] = context.planning_decisions
            session['updated_at'] = _utc_now_iso()
            self._touch_locked()
        self._broadcast_snapshot()

    def record_planning_attempt(self, context: ExecutionContext, attempt: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        with self._lock:
            session = self._require_current_session_locked(self._session_id(context))
            if session is None:
                return
            number = attempt['attempt']
            now = _utc_now_iso()
            session['plans'].append({'id': f'{session["id"]}:attempt:{context.iteration + 1}:{number}',
                'iteration': context.iteration, 'attempt': number,
                'label': f'Decision {context.iteration + 1} · attempt {number}',
                'type': 'planning_attempt', 'action': '', 'decision': {},
                'reasoning': attempt['message'], 'error_code': attempt['code'],
                'retry_scheduled': attempt['retry_scheduled'], 'needs_more_planning': attempt['retry_scheduled'],
                'context_window': self._serialize_context_window(attempt['metrics']),
                'prompt_debug': self._serialize_prompt_debug({'messages': attempt['messages'],
                    'raw_response': attempt['raw_response'],
                    'model': getattr(attempt['metrics'], 'model', ''),
                    'max_context_tokens': getattr(attempt['metrics'], 'max_context_tokens', 0),
                    'iteration': context.iteration}),
                'created_at': now, 'status': 'timeout' if attempt['code'] == 'planning_timeout' else 'failure', 'steps': []})
            session['updated_at'] = now
            self._touch_locked()
        self._broadcast_snapshot()

    def reject_decision(self, context: ExecutionContext, reason: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            session = self._require_current_session_locked(self._session_id(context))
            plan = self._find_plan_locked(session, context.iteration) if session else None
            if plan is None:
                return
            plan.update({'status': 'failure', 'error_code': 'blocked_before_execution', 'rejection_reason': reason})
            for step in plan['steps']:
                if step['status'] == 'pending':
                    step.update({'status': 'blocked', 'error': reason})
            session['updated_at'] = _utc_now_iso()
            self._touch_locked()
        self._broadcast_snapshot()

    def mark_lookup_reused(self, context: ExecutionContext, step_index: int, source_index: int, result: ToolResult) -> None:
        if not self.enabled:
            return
        with self._lock:
            session = self._require_current_session_locked(self._session_id(context))
            plan = self._find_plan_locked(session, context.iteration) if session else None
            step = self._find_step_locked(plan, step_index) if plan else None
            if step is None:
                return
            now = _utc_now_iso()
            plan['status'] = 'reused'
            step.update({'status': 'reused', 'reused_result': True, 'source_result_index': source_index,
                'result_summary': summarize_result_for_context(result.result), 'completed_at': now,
                'should_announce': False})
            session.update({'phase': 'planning', 'phase_detail': f'Reusing the previous {result.tool_name} result',
                'updated_at': now})
            self._set_agent_state_locked('planning', session['id'])
            self._touch_locked()
        self._broadcast_snapshot()

    def mark_step_started(self, context: ExecutionContext, step_index: int, step: ToolCall) -> None:
        if not self.enabled:
            return
        with self._lock:
            session = self._require_current_session_locked(self._session_id(context))
            if session is None:
                return
            plan = self._find_plan_locked(session, context.iteration)
            step_state = self._find_step_locked(plan, step_index) if plan else None
            if plan is None or step_state is None:
                return
            now = _utc_now_iso()
            plan['status'] = 'running'
            step_state['status'] = 'running'
            step_state['started_at'] = now
            step_state['should_announce'] = step.should_announce
            session['phase'] = 'executing'
            session['phase_detail'] = f'Running {step.name}'
            session['updated_at'] = now
            self._set_agent_state_locked('executing', session['id'])
            self._touch_locked()
        self._broadcast_snapshot()

    def mark_step_completed(self, context: ExecutionContext, step_index: int, result: ToolResult) -> None:
        if not self.enabled:
            return
        with self._lock:
            session = self._require_current_session_locked(self._session_id(context))
            if session is None:
                return
            plan = self._find_plan_locked(session, context.iteration)
            step_state = self._find_step_locked(plan, step_index) if plan else None
            if plan is None or step_state is None:
                return
            now = _utc_now_iso()
            step_state['status'] = result.status.value
            step_state['completed_at'] = now
            step_state['result_summary'] = summarize_result_for_context(result.result)
            step_state['error'] = result.error or ''
            step_state['error_details'] = copy.deepcopy(result.error_details)
            step_state['has_return_data'] = bool(result.has_return_data)
            if result.announcement:
                step_state['announcement'] = result.announcement
            session['phase'] = 'planning' if result.success and step_state.get('has_return_data') else 'executing'
            session['phase_detail'] = self._phase_detail_for_result(result)
            session['updated_at'] = now
            plan['status'] = self._plan_status_locked(plan)
            self._set_agent_state_locked(session['phase'], session['id'])
            self._touch_locked()
        self._broadcast_snapshot()

    def complete_session(self, context: ExecutionContext, status: str, response_text: str = '', error_text: str = '', ) -> None:
        if not self.enabled:
            return
        session_id = self._session_id(context)
        with self._lock:
            session = self._require_current_session_locked(session_id)
            if session is None:
                return
            now = _utc_now_iso()
            awaiting = status == 'awaiting_input'
            questions = session['clarifications']
            if awaiting:
                remaining = max(0.0, context.clarification_deadline - time.monotonic())
                question = {'question': response_text or context.clarification_question,
                    'state': 'waiting', 'asked_at': now,
                    'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=remaining)).isoformat(),
                    'reply_index': len(context.followups), 'answer': '', 'answered_at': None,
                    'plan_id': next((plan['id'] for plan in reversed(session['plans'])
                        if plan['action'] == 'ask_user'), None),
                    'rejected_replies': []}
                if not questions or questions[-1]['state'] != 'waiting':
                    questions.append(question)
            else:
                for question in questions:
                    if question['state'] == 'waiting' or (status == 'superseded' and question is questions[-1]):
                        question['state'] = {'timeout': 'expired', 'stopped': 'cancelled',
                            'superseded': 'superseded'}.get(status, 'closed')
                        if status == 'superseded':
                            question['replacement_request'] = context.replacement_request
                        question['closed_at'] = now
                        self._update_question_plan_locked(session, question, status)
            session['status'] = status
            session['phase'] = 'awaiting_input' if awaiting else 'idle'
            session['phase_detail'] = 'Waiting for your reply — say Charlie, then your answer' if awaiting else ''
            session['response_text'] = response_text or ''
            session['knowledge_outcome'] = context.knowledge_outcome
            session['planning_error'] = copy.deepcopy(context.planning_error)
            session['task_progress'] = copy.deepcopy(context.task_progress)
            session['planning_decisions'] = context.planning_decisions
            last_failure = (context.previous_results[-1] if context.previous_results and
                status in {'failure', 'timeout'} and not context.previous_results[-1].success else None)
            session['execution_error'] = copy.deepcopy(last_failure.error_details) if last_failure else None
            session['error_text'] = (error_text or (context.planning_error or {}).get('message', '') or
                (last_failure.error if last_failure else '') or '')
            session['completed_at'] = None if awaiting else now
            session['updated_at'] = now
            if awaiting:
                self._set_agent_state_locked('awaiting_input', session_id)
            else:
                self._history.appendleft(copy.deepcopy(session))
                self._current_session = None
                self._set_agent_state_locked('idle', None)
            self._touch_locked()
        self._broadcast_snapshot()

    def record_clarification_rejection(self, context: ExecutionContext, text: str, reason: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            session = self._require_current_session_locked(self._session_id(context))
            if session is None or not session.get('clarifications'):
                return
            question = session['clarifications'][-1]
            if question['state'] != 'waiting':
                return
            now = _utc_now_iso()
            replies = question['rejected_replies']
            replies.append({'text': text, 'reason': reason, 'received_at': now})
            del replies[:-5]
            session['updated_at'] = now
            self._touch_locked()
        self._broadcast_snapshot()

    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'type': 'planner_state',
                'server': {'host': self.host, 'port': self.port, 'client_count': self._client_count, },
                'agent': copy.deepcopy(self._agent_state),
                'current_session': copy.deepcopy(self._current_session),
                'history': [copy.deepcopy(item) for item in self._history],
                'updated_at': self._updated_at, }

    def _run_server(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as exc:
            log.error(f'Planner live view server failed: {exc}')
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    async def _serve(self) -> None:
        self._stop_event = asyncio.Event()
        async with websockets.serve(self._handle_client, self.host, self.port, ping_interval=20, ping_timeout=20):
            log.info(f'Planner live view websocket listening on ws://{self.host}:{self.port}/ws/planner')
            self._ready.set()
            await self._stop_event.wait()

    async def _handle_client(self, websocket: Any, path: Optional[str] = None) -> None:
        request = getattr(websocket, 'request', None)
        request_path = path or getattr(request, 'path', None) or getattr(websocket, 'path', '/')
        if request_path != '/ws/planner':
            log.warning(f'Rejecting planner live view client on unexpected path: {request_path}')
            await websocket.close(code=1008, reason='Unsupported path')
            return
        self._register_client(1)
        self._clients.add(websocket)
        try:
            await websocket.send(json.dumps(self.get_snapshot(), ensure_ascii=False))
            async for _ in websocket:
                continue
        except ConnectionClosed:
            pass
        except Exception as exc:
            log.warning(f'Planner live view client error: {exc}')
        finally:
            self._clients.discard(websocket)
            self._register_client(-1)

    def _broadcast_snapshot(self) -> None:
        if not self.enabled or self._loop is None or self._loop.is_closed():
            return
        snapshot = self.get_snapshot()
        asyncio.run_coroutine_threadsafe(self._send_snapshot(snapshot), self._loop)

    async def _send_snapshot(self, snapshot: Dict[str, Any]) -> None:
        if not self._clients:
            return
        payload = json.dumps(snapshot, ensure_ascii=False)
        disconnected = []
        for websocket in list(self._clients):
            try:
                await websocket.send(payload)
            except Exception:
                disconnected.append(websocket)
        for websocket in disconnected:
            self._clients.discard(websocket)
        if disconnected:
            self._register_client(0)

    def _register_client(self, delta: int) -> None:
        with self._lock:
            if delta:
                self._client_count = max(0, self._client_count + delta)
            else:
                self._client_count = len(self._clients)
            self._touch_locked()

    def _require_current_session_locked(self, session_id: str) -> Optional[Dict[str, Any]]:
        if self._current_session and self._current_session.get('id') == session_id:
            return self._current_session
        return None

    def _find_plan_locked(self, session: Dict[str, Any], iteration: int) -> Optional[Dict[str, Any]]:
        for plan in reversed(session.get('plans', [])):
            if int(plan.get('iteration', -1)) == int(iteration) and plan.get('type') != 'planning_attempt':
                return plan
        return None

    @staticmethod
    def _update_question_plan_locked(session: Dict[str, Any], question: Dict[str, Any], status: str) -> None:
        for plan in session['plans']:
            if plan['id'] == question.get('plan_id'):
                plan['status'] = status
                break

    def _find_step_locked(self, plan: Optional[Dict[str, Any]], step_index: int) -> Optional[Dict[str, Any]]:
        if not plan:
            return None
        for step in plan.get('steps', []):
            if int(step.get('index', -1)) == int(step_index):
                return step
        return None

    def _serialize_step(self, index: int, step: ToolCall) -> Dict[str, Any]:
        return {
            'index': index,
            'name': step.name,
            'title': step.name or f'Step {index + 1}',
            'args': step.args or {},
            'reason': step.reason,
            'should_announce': bool(step.should_announce),
            'returns_data': bool(step.returns_data),
            'task_id': step.task_id,
            'status': 'pending',
            'started_at': None,
            'completed_at': None,
            'result_summary': '',
            'error': '',
            'announcement': '',
            'has_return_data': bool(step.returns_data), }

    def _serialize_context_window(self, metrics: Any) -> Optional[Dict[str, Any]]:
        if metrics is None:
            return None
        max_tokens = max(0, int(getattr(metrics, 'max_context_tokens', 0) or 0))
        used_tokens = getattr(metrics, 'prompt_tokens', None)
        used_tokens = int(used_tokens) if used_tokens is not None else None
        response_tokens = getattr(metrics, 'response_tokens', None)
        response_tokens = int(response_tokens) if response_tokens is not None else None
        usage_ratio = None
        remaining_tokens = None
        if used_tokens is not None and max_tokens > 0:
            usage_ratio = used_tokens / max_tokens
            remaining_tokens = max(0, max_tokens - used_tokens)
        return {
            'model': str(getattr(metrics, 'model', '') or ''),
            'max_tokens': max_tokens,
            'used_tokens': used_tokens,
            'remaining_tokens': remaining_tokens,
            'usage_ratio': usage_ratio,
            'response_tokens': response_tokens,
            'output_token_limit': getattr(metrics, 'output_token_limit', None),
            'finish_reason': getattr(metrics, 'finish_reason', None),
            'elapsed_s': getattr(metrics, 'elapsed_s', None),
            'system_prompt_chars': int(getattr(metrics, 'system_prompt_chars', 0) or 0),
            'user_prompt_chars': int(getattr(metrics, 'user_prompt_chars', 0) or 0),
            'prompt_chars': int(getattr(metrics, 'prompt_chars', 0) or 0), }

    def _serialize_prompt_debug(self, prompt_debug: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(prompt_debug, dict):
            return None
        messages = []
        for message in prompt_debug.get('messages', []):
            if not isinstance(message, dict):
                continue
            messages.append({
                'role': str(message.get('role', '') or ''),
                'content': str(message.get('content', '') or ''), })
        return {
            'model': str(prompt_debug.get('model', '') or ''),
            'max_context_tokens': int(prompt_debug.get('max_context_tokens', 0) or 0),
            'iteration': int(prompt_debug.get('iteration', 0) or 0),
            'is_continuation': bool(prompt_debug.get('is_continuation', False)),
            'user_request': str(prompt_debug.get('user_request', '') or ''),
            'english_request': str(prompt_debug.get('english_request', '') or ''),
            'context_summary': str(prompt_debug.get('context_summary', '') or ''),
            'base_system_prompt': str(prompt_debug.get('base_system_prompt', '') or ''),
            'full_system_prompt': str(prompt_debug.get('full_system_prompt') or '\n'.join(
                message['content'] for message in messages if message['role'] == 'system')),
            'user_prompt': str(prompt_debug.get('user_prompt') or '\n'.join(
                message['content'] for message in messages if message['role'] != 'system')),
            'execution_history': str(prompt_debug.get('execution_history', '') or ''),
            'task_history': str(prompt_debug.get('task_history', '') or ''),
            'available_tools': str(prompt_debug.get('available_tools', '') or ''),
            'raw_response': str(prompt_debug.get('raw_response', '') or ''),
            'raw_parsed_plan': copy.deepcopy(prompt_debug.get('raw_parsed_plan')),
            'compiled_plan': copy.deepcopy(prompt_debug.get('compiled_plan')),
            'decision': copy.deepcopy(prompt_debug.get('decision')),
            'messages': messages, }

    @staticmethod
    def _serialize_decision(prompt_debug: Any) -> Dict[str, Any]:
        if not isinstance(prompt_debug, dict):
            return {}
        decision = prompt_debug.get('decision')
        return copy.deepcopy(decision) if isinstance(decision, dict) else {}

    @staticmethod
    def _phase_detail_for_decision(action: str, plan: ExecutionPlan) -> str:
        if action == 'call_tool' and plan.steps:
            return f'Calling {plan.steps[0].name}'
        if action == 'select_tools':
            return 'Selecting more tools'
        if action == 'answer':
            return 'Answering user'
        if action == 'finish':
            return 'Finishing request'
        if action == 'ask_user':
            return 'Asking for clarification'
        if action == 'replace_request':
            return 'Switching to a new request'
        return 'Evaluating next action'

    def _plan_status_locked(self, plan: Dict[str, Any]) -> str:
        statuses = [step.get('status') for step in plan.get('steps', [])]
        if not statuses:
            return plan.get('status', 'empty')
        if any(status == 'failure' for status in statuses):
            return 'failure'
        if any(status == 'stopped' for status in statuses):
            return 'stopped'
        if any(status == 'timeout' for status in statuses):
            return 'timeout'
        if any(status in ('pending', 'running') for status in statuses):
            return 'running'
        return 'success'

    def _phase_detail_for_result(self, result: ToolResult) -> str:
        if result.success:
            return f'Completed {result.tool_name}'
        if result.error:
            return f'{result.tool_name} failed'
        return f'Finished {result.tool_name}'

    def _set_agent_state_locked(self, status: str, session_id: Optional[str]) -> None:
        self._agent_state = {'status': status, 'active_session_id': session_id, 'updated_at': _utc_now_iso(), }

    def _touch_locked(self) -> None:
        self._updated_at = _utc_now_iso()

    def _session_id(self, context: ExecutionContext) -> str:
        return context.planning_session_id or context.request_id

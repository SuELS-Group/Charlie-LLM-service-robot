from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from workstation_common import get_logger, load

from models import ExecutionContext, ExecutionPlan

config = load(Path(__file__).resolve().parent / 'config' / 'llm.yaml', profile_key='model_profiles')
log = get_logger('llm.planner.telemetry')


class PlannerTelemetryClient:

    def __init__(self, endpoint: str = '', api_key: str = '', timeout_s: float = 2.0, enabled: bool = False, source: str = 'llm_node', ):
        self.endpoint = (endpoint or '').strip()
        self.api_key = (api_key or '').strip()
        self.timeout_s = float(timeout_s)
        self.enabled = bool(enabled and self.endpoint)
        self.source = source.strip() or 'llm_node'
        if self.enabled:
            log.info(f'Planner telemetry enabled: endpoint={self.endpoint} '
                     f'timeout={self.timeout_s:.1f}s source={self.source}')
        else:
            log.info('Planner telemetry disabled')

    @classmethod
    def from_config(cls) -> 'PlannerTelemetryClient':
        return cls(endpoint=str(getattr(config, 'planner_logging_endpoint', '') or ''), api_key=os.environ.get('CHARLIE_PLANNER_LOGGING_API_KEY', ''), timeout_s=float(getattr(config, 'planner_logging_timeout_s', 2.0) or 2.0), enabled=bool(getattr(config, 'planner_logging_enabled', False)), source=str(getattr(config, 'planner_logging_source', 'llm_node') or 'llm_node'), )

    def log_planner_event(self, *, context: ExecutionContext, system_prompt: str, available_tools: List[
        Dict[str, Any]], model: str, status: str, raw_response: str = '', parsed_plan: Optional[
        ExecutionPlan] = None, error_message: str = '', ) -> None:
        if not self.enabled:
            return
        payload = {
            'event_type': 'planner_trace',
            'source': self.source,
            'created_at_utc': datetime.now(timezone.utc).isoformat(),
            'request_id': context.request_id or '',
            'iteration': int(context.iteration),
            'planner_model': model,
            'status': status,
            'tool_names': [tool.get('name', 'unknown') for tool in available_tools if isinstance(tool, dict)],
            'input': {
                'system_prompt': system_prompt,
                'user_prompt': context.english_request or context.user_request,
                'original_user_prompt': context.user_request, },
            'output': {'raw_response': raw_response or '', 'parsed_plan': self._serialize_plan(parsed_plan), },
            'runtime': {
                'was_cancelled': bool(context.was_cancelled),
                'previous_results': [self._serialize_result(result) for result in context.previous_results],
                'task_history': [task.compact_summary() for task in context.task_history[-3:]], },
            'error_message': error_message or '', }
        thread = threading.Thread(target=self._post_json, args=(payload,), name='planner-telemetry', daemon=True, )
        thread.start()

    def _serialize_plan(self, plan: Optional[ExecutionPlan]) -> Optional[Dict[str, Any]]:
        if plan is None:
            return None
        return {
            'reasoning': plan.reasoning,
            'needs_more_planning': plan.needs_more_planning,
            'metrics': None if plan.metrics is None else {
                'model': plan.metrics.model,
                'max_context_tokens': plan.metrics.max_context_tokens,
                'prompt_tokens': plan.metrics.prompt_tokens,
                'response_tokens': plan.metrics.response_tokens,
                'system_prompt_chars': plan.metrics.system_prompt_chars,
                'user_prompt_chars': plan.metrics.user_prompt_chars,
                'prompt_chars': plan.metrics.prompt_chars, },
            'steps': [{
                'name': step.name,
                'args': step.args,
                'reason': step.reason,
                'should_announce': step.should_announce,
                'returns_data': step.returns_data, } for step in plan.steps], }

    @staticmethod
    def _serialize_result(result: Any) -> Dict[str, Any]:
        return {
            'tool_name': getattr(result, 'tool_name', ''),
            'status': getattr(getattr(result, 'status', ''), 'value', getattr(result, 'status', '')),
            'args': getattr(result, 'args', {}) or {},
            'result': getattr(result, 'result', None),
            'error': getattr(result, 'error', None),
            'error_details': getattr(result, 'error_details', None),
            'has_return_data': bool(getattr(result, 'has_return_data', False)),
            'announcement': getattr(result, 'announcement', None), }

    def _post_json(self, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')
        headers = {'Content-Type': 'application/json; charset=utf-8', 'Accept': 'application/json', }
        if self.api_key:
            headers['X-Api-Key'] = self.api_key
        request = Request(self.endpoint, data=body, headers=headers, method='POST')
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                status_code = getattr(response, 'status', None) or response.getcode()
                if status_code >= 400:
                    log.warning(f'Planner telemetry rejected with HTTP {status_code}')
        except HTTPError as exc:
            log.warning(f'Planner telemetry HTTP error: {exc.code} {exc.reason}')
        except URLError as exc:
            log.warning(f'Planner telemetry network error: {exc}')
        except Exception as exc:
            log.warning(f'Planner telemetry post failed: {exc}')

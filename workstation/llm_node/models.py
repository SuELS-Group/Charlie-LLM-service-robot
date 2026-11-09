from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Event
from typing import Any, Dict, List, Optional, Tuple, Type


class ExecutionStatus(str, Enum):
    SUCCESS = 'success'
    FAILURE = 'failure'
    STOPPED = 'stopped'
    TIMEOUT = 'timeout'
    PENDING = 'pending'
    AWAITING_INPUT = 'awaiting_input'
    REFUSED = 'refused'
    SUPERSEDED = 'superseded'


@dataclass
class ConversationMessage:
    role: str
    content: str


@dataclass
class PlannerPromptMetrics:
    model: str
    max_context_tokens: int
    prompt_tokens: Optional[int] = None
    response_tokens: Optional[int] = None
    system_prompt_chars: int = 0
    user_prompt_chars: int = 0
    output_token_limit: Optional[int] = None
    finish_reason: Optional[str] = None
    elapsed_s: Optional[float] = None

    @property
    def prompt_chars(self) -> int:
        return self.system_prompt_chars + self.user_prompt_chars


@dataclass
class ToolCall:
    name: str
    args: Dict[str, Any] = field(default_factory=dict)
    reason: str = ''
    should_announce: bool = False
    returns_data: bool = False
    task_id: str = ''


@dataclass
class ToolResult:
    tool_name: str
    status: ExecutionStatus
    result: Optional[Any] = None
    error: Optional[str] = None
    error_details: Optional[Dict[str, Any]] = None
    has_return_data: bool = False
    args: Dict[str, Any] = field(default_factory=dict)
    announcement: Optional[str] = None
    announcement_submitted: bool = False
    announcement_pending: bool = False
    task_id: str = ''
    observed_at: float = field(default_factory=time.monotonic, repr=False)
    knowledge_reply_count: int = 0

    @property
    def success(self) -> bool:
        return self.status == ExecutionStatus.SUCCESS

    @property
    def stopped(self) -> bool:
        return self.status == ExecutionStatus.STOPPED


@dataclass
class ExecutionPlan:
    reasoning: str
    steps: List[ToolCall] = field(default_factory=list)
    needs_more_planning: bool = False
    metrics: Optional[PlannerPromptMetrics] = None
    prompt_debug: Optional[Dict[str, Any]] = None


@dataclass
class CompletedStep:
    name: str
    args: Dict[str, Any] = field(default_factory=dict)
    status: ExecutionStatus = ExecutionStatus.SUCCESS
    result_summary: Optional[str] = None
    error: Optional[str] = None

    def summary(self) -> str:
        args_text = ', '.join(f'{key}={value}' for key, value in self.args.items()) if self.args else ''
        call = f'{self.name}({args_text})' if args_text else f'{self.name}()'
        if self.result_summary:
            return f'{call}: {self.status.value} -> {self.result_summary}'
        if self.error:
            return f'{call}: {self.status.value} -> {self.error}'
        return f'{call}: {self.status.value}'


@dataclass
class CompletedTask:
    request: str
    original_request: Optional[str] = None
    english_request: Optional[str] = None
    steps: List[CompletedStep] = field(default_factory=list)
    response_text: Optional[str] = None
    status: ExecutionStatus = ExecutionStatus.SUCCESS

    def summary(self) -> str:
        lines = [f'Request: {self.english_request or self.request}', f'Status: {self.status.value}']
        if self.steps:
            lines.extend(step.summary() for step in self.steps[:8])
        if self.response_text:
            lines.append(f'Response: {self.response_text}')
        return '\n'.join(lines)

    def compact_summary(self) -> str:
        lines = [f'Request: {self.english_request or self.request}', f'Status: {self.status.value}']
        calls = []
        for step in self.steps[:8]:
            args_text = ', '.join(f'{key}={value}' for key, value in step.args.items()) if step.args else ''
            calls.append(f'{step.name}({args_text})' if args_text else f'{step.name}()')
        if calls:
            lines.append(f'Calls: {", ".join(calls)}')
        if self.response_text:
            lines.append(f'Response: {self.response_text}')
        return '\n'.join(lines)


@dataclass
class ExecutionContext:
    user_request: str
    english_request: str = ''
    request_id: str = ''
    issued_at: float = field(default_factory=time.time)
    previous_results: List[ToolResult] = field(default_factory=list)
    iteration: int = 0
    was_cancelled: bool = False
    cancel_event: Event = field(default_factory=Event, repr=False)
    stop_generation: int = 0
    planning_session_id: Optional[str] = None
    task_history: List[CompletedTask] = field(default_factory=list)
    clarification_question: str = ''
    clarification_reason: str = ''
    clarification_deadline: float = 0.0
    followups: List[Dict[str, str]] = field(default_factory=list)
    replacement_request: str = ''
    selected_tool_names: List[str] = field(default_factory=list)
    task_progress: List[Dict[str, Any]] = field(default_factory=list)
    planning_decisions: int = 0
    planning_error: Optional[Dict[str, Any]] = None
    planning_attempts: List[Dict[str, Any]] = field(default_factory=list)
    last_planning_messages: List[Dict[str, str]] = field(default_factory=list, repr=False)
    reused_lookup_index: Optional[int] = None
    knowledge_outcome: str = ''

    def add_result(self, result: ToolResult) -> None:
        self.previous_results.append(result)

    def is_cancelled(self) -> bool:
        return self.was_cancelled or self.cancel_event.is_set()

    def get_summary(self) -> str:
        parts = []
        if self.previous_results:
            parts.append('Current task results:')
            parts.extend(
                f'- {result.tool_name}: {result.status.value} {summarize_result_for_context(result.result) if result.result else result.error or ""}'.strip()
                for result in self.previous_results[-8:])
        if self.task_history:
            parts.append('Recent completed tasks:')
            parts.extend(task.compact_summary() for task in self.task_history[-3:])
        return '\n'.join(parts) if parts else 'No prior context.'

    def summarize_result_for_context(self, result: Any) -> str:
        return summarize_result_for_context(result)


@dataclass
class ToolSpec:
    name: str
    description: str = ''
    parameters: List[Dict[str, Any]] = field(default_factory=list)
    returns: str = 'Any'
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def category(self) -> str:
        return str(self.meta.get('category') or 'uncategorized')

    @property
    def returns_data(self) -> bool:
        return bool(self.meta.get('returns_data'))


@dataclass
class AgentDecision:
    action: str
    reasoning: str = ''
    tool: str = ''
    args: Dict[str, Any] = field(default_factory=dict)
    answer: str = ''
    query: str = ''
    categories: List[str] = field(default_factory=list)
    should_announce: bool = False
    task_plan: List[Dict[str, str]] = field(default_factory=list)
    task_id: str = ''
    evidence_ids: List[str] = field(default_factory=list)
    knowledge_outcome: str = ''


@dataclass
class AgentTurn:
    decision: AgentDecision
    raw_response: str
    messages: List[Dict[str, str]]
    metrics: PlannerPromptMetrics
    available_tools: List[str]


def extract_json_value(text: str, expected_types: Tuple[Type[Any], ...] = (dict, list)) -> Any:
    text = (text or '').strip()
    if not text:
        raise ValueError('Empty JSON input')
    try:
        value = json.loads(text)
        if isinstance(value, expected_types):
            return value
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in '{[':
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, expected_types):
            return value
    raise ValueError('No JSON value found in model response')


def summarize_result_for_context(result: Any, limit: int = 700) -> str:
    if result is None:
        return ''
    value = result.get('result', result) if isinstance(result, dict) else result
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            if key in {'tool', 'status', 'error', 'meta'}:
                continue
            if isinstance(item, list):
                parts.append(f'{key}={_compact_list(item)}')
            elif isinstance(item, dict):
                parts.append(f'{key}={_compact_dict(item)}')
            else:
                parts.append(f'{key}={item}')
        text = ', '.join(parts) if parts else str(value)
    elif isinstance(value, list):
        text = _compact_list(value)
    else:
        text = str(value)
    return text[:limit]


def _compact_list(values: List[Any], limit: int = 6) -> str:
    rendered = []
    for item in values[:limit]:
        rendered.append(_compact_dict(item) if isinstance(item, dict) else str(item))
    suffix = f' ... +{len(values) - limit}' if len(values) > limit else ''
    return '[' + '; '.join(rendered) + suffix + ']'


def _compact_dict(value: Dict[str, Any], limit: int = 8) -> str:
    parts = []
    for index, (key, item) in enumerate(value.items()):
        if index >= limit:
            parts.append('...')
            break
        parts.append(f'{key}:{item}')
    return '{' + ', '.join(parts) + '}'

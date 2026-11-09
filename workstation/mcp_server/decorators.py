from __future__ import annotations

import inspect
import re
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union, get_origin

from workstation_common import get_logger
from mcp_server.call_state import capture_call_faults, operation_budget, tool_context, request_tool
from mcp_server.study_policy import check_tool
from workstation_common.action_validation import physical_argument_error
from mcp_server.call_state import request_id as current_request_id
from mcp_server.k2_events import record as record_k2_event

log = get_logger('mcp.decorators')


class ToolStatus(str, Enum):
    SUCCESS = 'success'
    FAILURE = 'failure'
    STOPPED = 'stopped'
    TIMEOUT = 'timeout'
    PENDING = 'pending'


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _type_to_str(t: Any) -> str:
    try:
        # Generic aliases expose short names such as Optional/List, which lose
        # their element types. The planner needs the complete annotation.
        if get_origin(t) is not None:
            return str(t).replace('typing.', '')
        name = getattr(t, '__name__', None)
        if name:
            return name
        return str(t).replace('typing.', '')
    except Exception:
        return 'Any'


def _build_tool_metadata(func: Callable[..., Any], *, name: str, description: str, meta: Optional[
    Dict[str, Any]] = None, ) -> Dict[str, Any]:
    sig = inspect.signature(func)
    params: List[Dict[str, Any]] = []
    for pname, p in sig.parameters.items():
        if pname == 'ctx':
            continue
        annotation = 'Any' if p.annotation is inspect._empty else _type_to_str(p.annotation)
        default = None if p.default is inspect._empty else p.default
        required = (p.default is inspect._empty) and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                                                inspect.Parameter.KEYWORD_ONLY,)
        params.append({
            'name': pname,
            'type': annotation,
            'default': default,
            'required': required,
            'kind': p.kind.name, })
    return_type = 'Any' if sig.return_annotation is inspect._empty else _type_to_str(sig.return_annotation)
    try:
        raw = description or ''
        cleaned = inspect.cleandoc(raw)
        lines = cleaned.splitlines()
        normalized_lines: List[str] = []
        prev_blank = False
        for ln in lines:
            if ln.strip() == '':
                if not prev_blank:
                    normalized_lines.append('')
                    prev_blank = True
            else:
                normalized_lines.append(ln.rstrip())
                prev_blank = False
        clean_description = '\n'.join(normalized_lines).strip()
    except Exception:
        clean_description = (description or '').strip()
    return {
        'name': name,
        'description': clean_description,
        'parameters': params,
        'returns': return_type,
        'meta': meta or {}, }


def _normalize_hint_list(values: Optional[List[str]]) -> List[str]:
    if not values:
        return []
    normalized: List[str] = []
    seen = set()
    for value in values:
        item = (value or '').strip()
        if not item or item in seen:
            continue
        normalized.append(item)
        seen.add(item)
    return normalized


def _normalize_state_items(values: Optional[List[Union[str, Dict[str, Any]]]]) -> List[Dict[str, Any]]:
    if not values:
        return []
    normalized: List[Dict[str, Any]] = []
    seen = set()
    for value in values:
        item = {'state': value} if isinstance(value, str) else dict(value or {})
        state = str(item.get('state') or '').strip()
        if not state:
            continue
        item['state'] = state
        key = tuple(sorted((k, str(v)) for k, v in item.items()))
        if key in seen:
            continue
        normalized.append(item)
        seen.add(key)
    return normalized


def _resolve_path(obj: Any, path: str) -> Any:
    parts = path.split('.') if path else []
    cur = obj
    for p in parts:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            try:
                cur = getattr(cur, p)
            except Exception:
                return None
    return cur


def _render_template(template: str, result: Any = None, args: Optional[Dict[str, Any]] = None) -> str:
    if not template:
        return template

    def replacer(match):
        key = match.group(1)
        if key.startswith('result.'):
            path = key[len('result.'):]
            val = _resolve_path(result, path) if result is not None else None
            if val is None:
                return ''
            if isinstance(val, (list, tuple)):
                return ', '.join(str(x) for x in val)
            return str(val)
        if key == 'result':
            return str(result) if result is not None else ''
        if key.startswith('args.'):
            path = key[len('args.'):]
            return str(_resolve_path(args, path) or '') if args else ''
        if args and key in args:
            return str(args[key])
        return ''

    try:
        return re.sub(r'{([^}]+)}', replacer, template)
    except Exception:
        return template


def tool_result(status: Union[ToolStatus, str], result: Any = None, error: Optional[Dict[str, Any]] = None, meta:
Optional[Dict[str, Any]] = None, ) -> Dict[str, Any]:
    status_str = status.value if isinstance(status, ToolStatus) else status
    tool_name: Optional[str] = None
    try:
        stack = inspect.stack()
        for frame_info in stack[1:8]:
            fn_name = frame_info.function
            if fn_name in TOOL_REGISTRY:
                tool_name = fn_name
                break
    except Exception:
        tool_name = 'unknown'
    out = {'tool': tool_name, 'status': status_str, 'result': result, 'error': error, 'meta': meta or {}, }
    return out


def tool_with_meta(*, wait_duration: Optional[float] = None, announcement: Optional[
    str] = None, is_async: bool = False, provides: Optional[List[Union[str, Dict[str, Any]]]] = None, requires:
Optional[List[Union[str, Dict[str, Any]]]] = None, workflow_before: Optional[List[str]] = None, invalidates: Optional[
    List[str]] = None, returns_data: Optional[bool] = None, ):
    def decorator(fn: Callable[..., Any]):
        tool_name = fn.__name__
        final_desc = (fn.__doc__ or '').strip()
        module_name = getattr(fn, '__module__', '')
        derived_category = 'uncategorized'
        try:
            parts = module_name.split('.')
            if parts:
                if 'tools' in parts:
                    idx = parts.index('tools')
                    if idx + 1 < len(parts):
                        derived_category = parts[idx + 1]
                    else:
                        derived_category = parts[-1]
                else:
                    derived_category = parts[-1]
        except Exception:
            derived_category = 'uncategorized'
        provide_items = _normalize_state_items(provides)
        require_items = _normalize_state_items(requires)
        store_meta: Dict[str, Any] = {'category': derived_category, 'is_async': bool(is_async)}
        if wait_duration is not None:
            store_meta['wait_duration'] = wait_duration
        if announcement is not None:
            store_meta['announcement'] = announcement
        has_result_data = any('path' in item for item in provide_items)
        store_meta['returns_data'] = has_result_data if returns_data is None else bool(returns_data)
        if provide_items:
            store_meta['provides'] = provide_items
        if require_items:
            store_meta['requires'] = require_items
        workflow = _normalize_hint_list(workflow_before)
        if workflow:
            store_meta['workflow_before'] = workflow
        invalidated_states = _normalize_hint_list(invalidates)
        if invalidated_states:
            store_meta['invalidates'] = invalidated_states
        TOOL_REGISTRY[tool_name] = _build_tool_metadata(fn, name=tool_name, description=final_desc, meta=store_meta, )

        def wrapper(*args, **kwargs):
            try:
                if not args and isinstance(kwargs, dict) and 'args' in kwargs and 'kwargs' in kwargs:
                    incoming_args = kwargs.pop('args') or []
                    incoming_kwargs = kwargs.pop('kwargs') or {}
                    args = tuple(incoming_args)
                    kwargs = incoming_kwargs
            except Exception:
                pass
            issued_at = kwargs.pop('_request_time', None)
            language = kwargs.pop('_request_language', None)
            request_id = kwargs.pop('_request_id', None)
            bound_args = {}
            sig = inspect.signature(fn)
            try:
                bound = sig.bind_partial(*args, **kwargs)
                bound_args = dict(bound.arguments)
            except Exception:
                bound_args = {}
            try:
                sig.bind(*args, **kwargs)
            except TypeError as exc:
                params = TOOL_REGISTRY.get(tool_name, {}).get('parameters', [])
                expected = []
                required = []
                for p in params:
                    name = p.get('name', 'arg')
                    if p.get('required', False):
                        required.append(name)
                        expected.append(name)
                    else:
                        default = p.get('default')
                        expected.append(f'{name}={default!r}' if default is not None else f'{name}=...')
                provided = sorted(kwargs.keys()) if isinstance(kwargs, dict) else []
                return tool_result(ToolStatus.FAILURE, error={
                    'code': 'INVALID_ARGUMENTS',
                    'message': str(exc),
                    'retryable': False, 'execution_started': False,
                    'execution_uncertain': False, 'completion_confirmed': True,
                    'details': {
                        'tool': tool_name,
                        'provided': provided,
                        'required': required,
                        'expected_signature': f'{tool_name}({", ".join(expected)})', }, }, )
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            validation_error = physical_argument_error(tool_name, bound.arguments)
            if validation_error:
                return tool_result(ToolStatus.FAILURE, error={'code': 'INVALID_ARGUMENTS',
                    'message': validation_error, 'retryable': False, 'execution_started': False,
                    'execution_uncertain': False, 'completion_confirmed': True})
            try:
                with capture_call_faults(issued_at, language, request_id) as faults, operation_budget(tool_name), tool_context(tool_name):
                    denied = check_tool(tool_name, bound.arguments, request_tool())
                    if denied:
                        return tool_result(ToolStatus.FAILURE, error={'code': denied[0], 'message': denied[1],
                            'retryable': False, 'execution_uncertain': False})
                    ret = fn(*args, **kwargs)
                    if faults and isinstance(ret, dict):
                        fault = next((fault for fault in faults if fault['code'].endswith('TIMEOUT')),
                                     next((fault for fault in faults if fault.get('execution_uncertain')), faults[-1]))
                        error = dict(ret.get('error') or {})
                        error.update(fault)
                        ret['error'] = error
                        if ret.get('status') not in {'timeout', 'stopped'}:
                            ret['status'] = ToolStatus.TIMEOUT.value if fault['code'].endswith('TIMEOUT') else ToolStatus.FAILURE.value
            except Exception:
                raise
            try:
                announcement_template = store_meta.get('announcement') or None
                if isinstance(ret, dict):
                    if not announcement_template:
                        announcement_template = ret.get('meta', {}).get('announcement')
                    if announcement_template:
                        rendered = _render_template(announcement_template, result=ret.get('result'), args=bound_args)
                        ret.setdefault('meta', {})['announcement'] = rendered
            except Exception as exc:
                log.warning(f'Failed to render announcement template for {tool_name}: {exc}')
            return ret

        def audited_wrapper(*args, **kwargs):
            started = time.monotonic()
            root = request_tool() or tool_name
            envelope = kwargs.get('kwargs') if isinstance(kwargs.get('kwargs'), dict) else kwargs
            identifier = envelope.get('_request_id') or current_request_id()
            result, exception = None, True
            try:
                result = wrapper(*args, **kwargs)
                exception = False
                return result
            finally:
                record_k2_event(tool_name, root, identifier, started, result, exception)

        return audited_wrapper

    return decorator

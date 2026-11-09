"""Per-request lookup reuse. Dynamic sensing and physical effects stay fresh."""
from __future__ import annotations

from dataclasses import dataclass
import json
from time import monotonic
from workstation_common.tool_safety import needs_robot_state


@dataclass(frozen=True)
class LookupRule:
    max_age_s: float
    resources: frozenset[str]


# Only these reads can be memoized. Every other tool remains a fresh execution.
# A new explicit goal whose completion_tool is this read always permits a new read.
LOOKUP_RULES = {
    'get_waypoints': LookupRule(120, frozenset({'waypoints'})),
    'query_knowledge_base': LookupRule(120, frozenset({'knowledge'})),
    'recall_memory': LookupRule(120, frozenset({'memory'})),
    'wiki_summary': LookupRule(60, frozenset({'wiki'})),
}

RESOURCE_CHANGES = {
    'save_waypoint': {'waypoints'}, 'save_pointed_waypoint': {'waypoints'},
    'delete_waypoint': {'waypoints'}, 'switch_to_mapping_mode': {'waypoints'},
    'switch_to_localization_mode': {'waypoints'}, 'store_memory': {'memory', 'knowledge'},
}


def call_key(name, args, specs):
    values = dict(args or {})
    tool = specs.get(name)
    for param in tool.parameters if tool else []:
        if not param.get('required') and 'default' in param:
            values.setdefault(param['name'], param['default'])
    # JSON integer and integral float representations mean the same argument.
    def normalize(value):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value
    return name + ':' + json.dumps(normalize(values), sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def reusable_result(context, name, args, specs):
    rule = LOOKUP_RULES.get(name)
    if rule is None or needs_robot_state(name):
        return None
    key = call_key(name, args, specs)
    now = monotonic()
    for index in range(len(context.previous_results) - 1, -1, -1):
        result = context.previous_results[index]
        spec = specs.get(result.tool_name)
        invalidates = set(spec.meta.get('invalidates', []) or []) if spec else set()
        invalidates.update(RESOURCE_CHANGES.get(result.tool_name, ()))
        # Failed mutations can have partial effects too. Never reuse through them.
        if invalidates & rule.resources:
            return None
        if call_key(result.tool_name, result.args, specs) != key:
            continue
        if name in {'query_knowledge_base', 'wiki_summary'} and (
                getattr(result, 'knowledge_reply_count', 0) != len(context.followups)):
            return None
        age = now - result.observed_at
        if result.success and result.result is not None and 0 <= age <= rule.max_age_s:
            return index, result
        return None
    return None


def hidden_cached_helpers(context, tools, specs):
    """No-argument reads can be excluded from the generation schema completely."""
    remaining = [goal for goal in context.task_progress if goal['status'] not in {'completed', 'ready'}]
    completion = remaining[0]['completion_tool'] if remaining else ''
    return {tool.name for tool in tools if tool.name in LOOKUP_RULES and not tool.parameters
        and tool.name != completion and reusable_result(context, tool.name, {}, specs)}


def retained_lookup_results(context, specs):
    """Re-expose valid lookup evidence even when it fell out of the last ten results."""
    retained = []
    seen = set()
    for result in reversed(context.previous_results):
        if result.tool_name not in LOOKUP_RULES:
            continue
        key = call_key(result.tool_name, result.args, specs)
        if key in seen:
            continue
        seen.add(key)
        reusable = reusable_result(context, result.tool_name, result.args, specs)
        if reusable:
            retained.append(reusable)
    # Keep prompt growth bounded, always retaining the sole argument-free lookup
    # and the last reused result even when it is older than recent tool history.
    priority = [entry for entry in retained if entry[0] == context.reused_lookup_index
        or not (specs.get(entry[1].tool_name) and specs[entry[1].tool_name].parameters)]
    others = [entry for entry in retained if entry not in priority]
    return [*priority, *others[:max(0, 8 - len(priority))]]

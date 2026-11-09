"""Ordered user goals; only execution results and delivered replies advance them."""
from __future__ import annotations

from decision_contract import validation_error
from workstation_common.knowledge_policy import local_knowledge_mode

KNOWLEDGE_TOOLS = frozenset({'query_knowledge_base', 'wiki_summary'})

READ_HELPERS = frozenset({'battery_status', 'current_time', 'current_date', 'current_weather',
    'wiki_summary', 'query_knowledge_base', 'recall_memory', 'get_persons', 'get_person_details',
    'get_visible_objects', 'get_tracked_people', 'get_scene_description', 'get_identified_faces',
    'get_waypoints', 'get_slam_mode'})

# These tools already own their controller/state checks. Unrelated lookups
# cannot help finish these goals and used to distract the constrained decoder.
SELF_CONTAINED_GOALS = frozenset({'battery_status', 'turn_to_relative', 'turn_to',
    'move_relative', 'calibrate_arm', 'stow_arm', 'dock_robot', 'put_item_in_trash'})


def goal_tool_names(progress, specs):
    remaining = [task for task in progress if task['status'] not in {'completed', 'ready'}]
    if not remaining:
        return set()
    if remaining[0]['completion_tool'] in KNOWLEDGE_TOOLS:
        return {'query_knowledge_base'} if local_knowledge_mode() else set(KNOWLEDGE_TOOLS)
    allowed = set() if remaining[0]['completion_tool'] in SELF_CONTAINED_GOALS else set(READ_HELPERS)
    visited = set()
    def add(name):
        if name in visited or name not in specs:
            return
        visited.add(name)
        allowed.add(name)
        meta = specs[name].meta
        for provider in meta.get('workflow_before', []) or []:
            add(str(provider))
        for requirement in meta.get('requires', []) or []:
            if isinstance(requirement, dict) and requirement.get('provider'):
                add(str(requirement['provider']))
    add(remaining[0]['completion_tool'])
    return allowed


def task_plan_schema(tool_names):
    return {'type': 'array', 'minItems': 1, 'maxItems': 100, 'items': {
        'type': 'object', 'properties': {'id': {'type': 'string', 'minLength': 1},
            'label': {'type': 'string', 'minLength': 1},
            'completion_tool': {'enum': [*tool_names, 'response']}},
        'required': ['id', 'label', 'completion_tool'], 'additionalProperties': False}}


def accept_plan(context, decision, tool_names):
    if not decision.task_plan:
        return ''
    if context.task_progress:
        original = [{key: task[key] for key in ('id', 'label', 'completion_tool')} for task in context.task_progress]
        return '' if original == decision.task_plan else 'The task plan is already fixed. Continue its remaining goals.'
    error = validation_error(decision.task_plan, task_plan_schema(tool_names))
    if error:
        return error
    ids = [task.get('id') for task in decision.task_plan]
    if len(ids) != len(set(ids)):
        return 'Task IDs must be unique.'
    context.task_progress = [dict(task, status='pending', evidence=[]) for task in decision.task_plan]
    return ''


def remaining_tasks(context):
    return [task for task in context.task_progress if task['status'] not in {'completed', 'ready'}]


def progress_count(context):
    return sum(task['status'] in {'completed', 'ready'} for task in context.task_progress)


def validate_task_call(context, decision, specs):
    if not context.task_progress:
        return ''  # Compatibility with in-process callers that do not use the model contract.
    remaining = remaining_tasks(context)
    if not remaining:
        return 'All tool goals are verified. Finish the request without repeating them.'
    task = remaining[0]
    if decision.task_id != task['id']:
        return (f'task_id must be {task["id"]!r} for the first unfinished goal: {task["label"]}; '
            f'received {decision.task_id!r}. This call was not executed.')
    if decision.tool not in goal_tool_names(context.task_progress, specs):
        return (f'Goal {task["id"]} requires {task["completion_tool"]}. {decision.tool} cannot substitute for it.'
            ' Only read-only helpers and documented prerequisites are allowed. Use select_tools if the required tool is missing.')
    return ''


def record_result(context, call, result, fact_tools):
    result.task_id = call.task_id
    if call.name in KNOWLEDGE_TOOLS:
        result.knowledge_reply_count = len(context.followups)
    task = next((task for task in context.task_progress if task['id'] == call.task_id), None)
    if task is None:
        return
    task['evidence'].append({'tool': call.name, 'args': call.args, 'status': result.status.value,
        'result_index': len(context.previous_results) - 1})
    if result.success and task['completion_tool'] in KNOWLEDGE_TOOLS and call.name in KNOWLEDGE_TOOLS:
        task['status'] = 'needs_answer'
        data = result.result.get('result', {}) if isinstance(result.result, dict) else {}
        task['evidence_status'] = data.get('evidence_status', 'retrieved') if isinstance(data, dict) else 'retrieved'
    elif result.success and task['completion_tool'] == call.name:
        task['status'] = 'ready' if call.name in fact_tools else 'completed'
    else:
        task['status'] = ('in_progress' if result.success else 'uncertain'
            if result.status.value in {'timeout', 'pending'} or (result.error_details or {}).get('execution_uncertain')
            else 'blocked' if (result.error_details or {}).get('execution_started') is False
            else 'failed')


def mark_announced(context, results):
    ids = {result.task_id for result in results if result.announcement_submitted}
    for task in context.task_progress:
        if task['id'] in ids and task['status'] == 'ready':
            task['status'] = 'completed'


def finish_progress(context, answer):
    remaining = remaining_tasks(context)
    if remaining and not (answer.strip() and (all(task['completion_tool'] == 'response' for task in remaining) or
            len(remaining) == 1 and remaining[0]['status'] == 'needs_answer')):
        return 'The request still has unfinished goals: ' + ', '.join(task['label'] for task in remaining)
    if any(task['status'] == 'ready' for task in context.task_progress) and not answer.strip():
        return 'The requested information has not been spoken. Provide an answer or announce the pending result.'
    for task in context.task_progress:
        if task['status'] in {'ready', 'needs_answer'} or task['completion_tool'] == 'response':
            task['status'] = 'completed'
    return ''

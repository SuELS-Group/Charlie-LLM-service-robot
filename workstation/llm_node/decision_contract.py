"""Shared generation and execution contracts. This module never calls a tool."""
from __future__ import annotations

import ast
import math

from jsonschema import Draft202012Validator, validators


def _number(_checker, value):
    return isinstance(value, int) and not isinstance(value, bool) or isinstance(value, float) and math.isfinite(value)


Validator = validators.extend(Draft202012Validator, type_checker=Draft202012Validator.TYPE_CHECKER
    .redefine('number', _number).redefine('integer', lambda checker, value:
        _number(checker, value) and int(value) == value))


def annotation_schema(annotation):
    """Read registry annotations as syntax, never evaluate Python code."""
    if isinstance(annotation, list):
        return {'type': annotation}
    try:
        node = ast.parse(str(annotation or 'Any').replace('typing.', ''), mode='eval').body
    except (SyntaxError, ValueError):
        return {}

    def parse(node):
        if isinstance(node, ast.Constant) and node.value is None:
            return {'type': 'null'}
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return {'anyOf': [parse(node.left), parse(node.right)]}
        if isinstance(node, ast.Name):
            kind = {'str': 'string', 'int': 'integer', 'float': 'number', 'bool': 'boolean',
                'list': 'array', 'List': 'array', 'dict': 'object', 'Dict': 'object',
                'None': 'null', 'NoneType': 'null', 'string': 'string', 'integer': 'integer',
                'number': 'number', 'boolean': 'boolean', 'array': 'array', 'object': 'object'}
            return {'type': kind[node.id]} if node.id in kind else {}
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            name = node.value.id
            args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            if name == 'Optional':
                return {'anyOf': [parse(args[0]), {'type': 'null'}]}
            if name == 'Union':
                return {'anyOf': [parse(arg) for arg in args]}
            if name in {'List', 'list', 'Sequence'}:
                return {'type': 'array', 'items': parse(args[0])}
            if name in {'Dict', 'dict'}:
                return {'type': 'object', 'additionalProperties': parse(args[-1])}
            if name == 'Literal' and all(isinstance(arg, ast.Constant) for arg in args):
                return {'enum': [arg.value for arg in args]}
        return {}
    return parse(node)


def arguments_schema(tool):
    properties = {}
    required = []
    for param in tool.parameters:
        name = param.get('name')
        if not name:
            continue
        schema = dict(param['schema']) if 'schema' in param else annotation_schema(param.get('type'))
        if ('schema' not in param and 'default' in param and not param.get('required')
                and param['default'] is None and schema):
            schema = {'anyOf': [schema, {'type': 'null'}]}
        properties[name] = schema
        if param.get('required'):
            required.append(name)
    result = {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}
    # Both historical argument names are supported; an empty turn has no meaning.
    if tool.name == 'turn_to_relative' and {'angle', 'degrees'} <= properties.keys():
        result['anyOf'] = [{'required': [name], 'properties': {name: {'type': 'integer'}}}
            for name in ('angle', 'degrees')]
    return result


def decision_schema(tools, names=None, *, task_plan=None, task_active=False, current_task_id=None, knowledge_evidence=None):
    specs = {tool.name: tool for tool in tools}
    names = list(names if names is not None else specs)
    reasoning = {'reasoning': {'type': 'string'}}

    def branch(action, fields=None, required=()):
        return {'type': 'object', 'properties': {'action': {'const': action}, **reasoning, **(fields or {})},
            'required': ['action', *required], 'additionalProperties': False}

    branches = [branch(action, {'answer': {'type': 'string'}}, ['answer'])
        for action in ('answer', 'finish', 'ask_user', 'refuse')]
    for variant in branches[:2]:
        variant['properties'].update(evidence_ids={'type': 'array', 'maxItems': 3,
            'items': {'type': 'string'}}, knowledge_outcome={'enum': ['supported', 'no_evidence', 'conflict']})
    if knowledge_evidence is not None:
        # Require an explicit disposition and constrain IDs during generation,
        # not only afterwards. Optional fields were routinely omitted by the
        # local model even when the prompt explicitly requested them.
        for variant in branches[:2]:
            variant['properties'].update(answer={'const': ''},
                knowledge_outcome={'enum': ['supported', 'no_evidence', 'conflict'] if knowledge_evidence else ['no_evidence']},
                evidence_ids={'type': 'array', 'maxItems': 3 if knowledge_evidence else 0,
                    'items': {'enum': list(knowledge_evidence)} if knowledge_evidence else {'type':'string'}})
            variant['required'].extend(['knowledge_outcome', 'evidence_ids'])
    branches += [branch('replace_request'), branch('select_tools', {
        'query': {'type': 'string'}, 'categories': {'type': 'array', 'items': {'type': 'string'}}}, ['query'])]
    for name in names:
        args = arguments_schema(specs[name]) if name in specs else {'type': 'object'}
        branches.append(branch('call_tool', {'tool': {'const': name}, 'args': args,
            'should_announce': {'type': 'boolean'}}, ['tool', 'args']))
    if task_plan is not None:
        for variant in branches:
            action = variant['properties']['action']['const']
            if action != 'replace_request' and not task_active:
                variant['properties']['task_plan'] = task_plan
            if action == 'call_tool':
                # Once a plan exists, the scheduler already knows the next
                # goal. Do not let the model invent/reuse a different ID.
                variant['properties']['task_id'] = ({'const': current_task_id} if current_task_id is not None
                    else {'type': 'string', 'minLength': 1})
                variant['required'].append('task_id')
                if not task_active:
                    variant['required'].append('task_plan')
    return {'oneOf': branches}


def validation_error(value, schema):
    # Return a short actionable leaf error rather than echoing the full schema.
    if 'oneOf' in schema and isinstance(value, dict) and value.get('action') == 'call_tool':
        allowed = {variant['properties']['tool']['const'] for variant in schema['oneOf']
            if 'tool' in variant['properties']}
        if not value.get('tool'):
            return 'tool: a call_tool decision requires a tool name and args.'
        if not isinstance(value.get('tool'), str) or value['tool'] not in allowed:
            return f'tool: {value.get("tool")!r} is unavailable for this goal. Select missing tools or continue the first unfinished goal.'
    errors = list(Validator(schema).iter_errors(value))
    if not errors:
        return ''
    error = errors[0]
    if error.context and 'oneOf' in schema:
        action = value.get('action') if isinstance(value, dict) else None
        matching = [child for child in error.context if child.schema_path and
            schema.get('oneOf', [])[child.schema_path[0]]['properties']['action'].get('const') == action]
        if action == 'call_tool':
            matching = [child for child in matching if schema['oneOf'][child.schema_path[0]]['properties']
                .get('tool', {}).get('const') == value.get('tool')] or matching
        error = matching[0] if matching else error
    path = '.'.join(str(item) for item in error.absolute_path) or 'decision'
    return f'{path}: {error.message}'[:500]


class PlanningDecisionError(RuntimeError):
    code = 'invalid_decision'

    def __init__(self, message, *, raw_response='', metrics=None, messages=None):
        super().__init__(message)
        self.raw_response = raw_response
        self.metrics = metrics
        self.messages = messages or []


class PlanningOutputLimitError(PlanningDecisionError):
    code = 'output_limit'


class PlanningContextCapacityError(PlanningDecisionError):
    code = 'context_capacity'

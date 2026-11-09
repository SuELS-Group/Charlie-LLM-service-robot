from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Sequence

from workstation_common import get_logger

from models import ToolCall, ToolSpec
from decision_contract import arguments_schema, validation_error
from knowledge_grounding import information_question

log = get_logger('llm.tools')
TOOL_SEARCH_WORDS = {
    'query_knowledge_base': {'wissen', 'information', 'informationen', 'versicherung', 'krankenversicherung',
        'insurance', 'university', 'universität', 'office', 'büro', 'opening', 'öffnungszeiten', 'verfahren'},
    'turn_to_relative': {'drehe', 'drehen', 'dreh', 'dreht', 'turn', 'rotate'},
    'get_waypoints': {'wegpunkt', 'wegpunkte', 'testpunkt', 'testpunkte', 'waypoint', 'waypoints'},
    'move_to_waypoint': {'wegpunkt', 'wegpunkte', 'testpunkt', 'testpunkte', 'waypoint', 'waypoints'},
    'calibrate_arm': {'kalibriere', 'kalibrieren', 'calibrate'},
    'stow_arm': {'verstaue', 'verstauen', 'stow'},
    'release_item': {'loslassen', 'release'},
    'put_item_in_trash': {'müll', 'muell', 'mülleimer', 'muelleimer', 'abfall', 'abfalleimer',
        'entsorgen', 'entsorge', 'wegwerfen', 'trash', 'trashcan', 'garbage', 'rubbish', 'bin', 'dispose'},
    'battery_status': {'akkustand', 'akku', 'battery', 'batteriestatus', 'ladestatus', 'ladestand', 'ladezustand', 'charging'},
    'dock_robot': {'dock', 'docke', 'andocken', 'aufladen', 'ladestation', 'dockingstation', 'lade'},
}
SEARCH_STOP_WORDS = frozenset('charlie please bitte the a an and und der die das den dem des ein eine einen einer '
    'ist sind is are was what wie how kann can you du mir me my your dein deine es it to zu of von mit with '
    'ich ichs im in on at für for this that tell sag sagen'.split())


class ToolCatalog:

    def __init__(self, tools: Sequence[Dict[str, Any]]):
        self.tools = [self._spec(tool) for tool in tools if isinstance(tool, dict) and tool.get('name')]
        self.by_name = {tool.name: tool for tool in self.tools}
        self.by_category: Dict[str, List[ToolSpec]] = {}
        for tool in self.tools:
            self.by_category.setdefault(tool.category, []).append(tool)
        self._search_text = {tool.name: self._text_for(tool) for tool in self.tools}
        log.info(f'Tool catalog loaded: tools={len(self.tools)} categories={len(self.by_category)}')

    @staticmethod
    def _spec(tool: Dict[str, Any]) -> ToolSpec:
        params = tool.get('parameters') or []
        if isinstance(params, dict):
            required = set(params.get('required') or [])
            params = [{
                'name': name,
                'type': info.get('type', 'Any'),
                'required': name in required,
                'default': info.get('default'),
                'schema': dict(info),
                'description': info.get('description', '')} for name, info in (params.get('properties') or {}).items()]
        return ToolSpec(name=str(tool.get('name')), description=str(tool.get('description') or '').strip(), parameters=list(params) if isinstance(params, list) else [], returns=str(tool.get('returns') or 'Any'), meta=dict(tool.get('meta') or {}))

    def initial_tools(self, request: str, limit: int = 10) -> List[ToolSpec]:
        selected = self.search(request, limit=limit)
        if information_question(request) and 'query_knowledge_base' in self.by_name:
            selected = [self.by_name['query_knowledge_base'], *[t for t in selected if t.name != 'query_knowledge_base']][:limit]
        if selected:
            return self.expand(selected)
        return []

    def category_summary(self) -> str:
        lines = []
        for category in sorted(self.by_category):
            names = ', '.join(tool.name for tool in self.by_category[category])
            lines.append(f'- {category}: {names}')
        return '\n'.join(lines)

    def search(self, query: str, categories: Sequence[str] | None = None, limit: int = 12) -> List[ToolSpec]:
        words = self._tokens(query)
        category_set = {str(c) for c in categories or [] if str(c)}
        candidates = [tool for tool in self.tools if not category_set or tool.category in category_set]
        scored = []
        for tool in candidates:
            text = self._search_text[tool.name]
            score = 12 if tool.name in words else 0
            if tool.name == 'release_item' and re.search(r'\b(?:öffne\w*|open)\b.{0,80}\b(?:greifer|gripper)\b', query, re.I):
                score += 12
            if words & TOOL_SEARCH_WORDS.get(tool.name, set()):
                score += 6
            for word in words:
                if word in self._tokens(text.replace('_', ' ')):
                    score += 3 if word in tool.name.lower().split('_') else 1
            if category_set and tool.category in category_set:
                score += 4
            if score:
                scored.append((score, tool.name, tool))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [tool for _, _, tool in scored[:limit]]

    def expand(self, tools: Iterable[ToolSpec]) -> List[ToolSpec]:
        selected: Dict[str, ToolSpec] = {}

        def add(name: str) -> None:
            if not name or name in selected or name not in self.by_name:
                return
            tool = self.by_name[name]
            for provider in tool.meta.get('workflow_before', []) or []:
                add(str(provider))
            for requirement in tool.meta.get('requires', []) or []:
                provider = requirement.get('provider') if isinstance(requirement, dict) else None
                if provider:
                    add(str(provider))
            selected[name] = tool

        for tool in tools:
            add(tool.name)
        return list(selected.values())

    def select_more(self, query: str = '', categories: Sequence[str] | None = None, current: Sequence[
                                                                                                 str] | None = None, limit: int = 12) -> \
            List[ToolSpec]:
        current_set = set(current or [])
        matches = self.search(query, categories, limit=limit)
        if not matches and categories:
            matches = [tool for category in categories or [] for tool in self.by_category.get(str(category), [])]
        expanded = self.expand(matches)
        return [tool for tool in expanded if tool.name not in current_set]

    def tool_cards(self, tools: Sequence[ToolSpec]) -> str:
        return '\n'.join(self.tool_card(tool) for tool in tools)

    def tool_card(self, tool: ToolSpec) -> str:
        params = []
        for param in tool.parameters:
            name = param.get('name')
            if not name:
                continue
            ptype = param.get('type') or 'Any'
            required = bool(param.get('required'))
            default = param.get('default')
            suffix = '' if required else ' optional'
            if default is not None:
                suffix += f' default={default}'
            params.append(f'{name}:{ptype}{suffix}')
        hints = []
        if tool.meta.get('workflow_before'):
            hints.append(f'before={tool.meta.get("workflow_before")}')
        if tool.meta.get('requires'):
            hints.append(f'requires={tool.meta.get("requires")}')
        if tool.meta.get('provides'):
            hints.append(f'provides={tool.meta.get("provides")}')
        if tool.meta.get('invalidates'):
            hints.append(f'invalidates={tool.meta.get("invalidates")}')
        data = ' data' if tool.returns_data else ''
        hint_text = f' | {"; ".join(hints)}' if hints else ''
        return f'- {tool.name}({", ".join(params)}) [{tool.category}{data}] {tool.description.replace(chr(10), " ")}{hint_text}'

    def validate_call(self, call: ToolCall) -> tuple[bool, str, ToolCall]:
        tool = self.by_name.get(call.name)
        if tool is None:
            return False, f'unknown tool {call.name}', call
        error = validation_error(call.args, arguments_schema(tool))
        if error:
            return False, f'Invalid arguments for {call.name}: {error}', call
        return True, '', ToolCall(name=call.name, args=dict(call.args), reason=call.reason,
            should_announce=call.should_announce, returns_data=tool.returns_data, task_id=call.task_id)

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {token for token in re.findall(r'[\w_]+', (text or '').lower())
                if len(token) > 1 and token not in SEARCH_STOP_WORDS}

    @staticmethod
    def _text_for(tool: ToolSpec) -> str:
        params = ' '.join(
            str(param.get('name') or '') + ' ' + str(param.get('description') or '') for param in tool.parameters)
        meta = ' '.join(str(value) for value in tool.meta.values())
        return f'{tool.name} {tool.category} {tool.description} {params} {meta}'.lower()

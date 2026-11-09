from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

from tool_catalog import ToolCatalog
from workstation_common import get_logger

if TYPE_CHECKING:
    from mcp_client import MCPClient
log = get_logger('llm.router')


class ToolRouter:

    def __init__(self, client: Any = None, mcp_client: Optional['MCPClient'] = None):
        self.client = client
        self.mcp = None
        self.catalog = ToolCatalog([])
        if mcp_client is not None:
            self.set_mcp_client(mcp_client)

    def set_mcp_client(self, mcp_client: 'MCPClient') -> None:
        self.mcp = mcp_client
        self.catalog = ToolCatalog(getattr(mcp_client, 'llm_callable_tools', []) or [])

    def get_tools_for_request(self, request: str, context_summary: str = '') -> List[Dict]:
        del context_summary
        names = [tool.name for tool in self.catalog.initial_tools(request)]
        return [self._raw(name) for name in names if self._raw(name)]

    def get_tools_for_categories(self, categories: List[str]) -> List[Dict]:
        selected = self.catalog.select_more(categories=categories)
        return [self._raw(tool.name) for tool in selected if self._raw(tool.name)]

    def _raw(self, name: str) -> Dict[str, Any] | None:
        for tool in getattr(self.mcp, 'llm_callable_tools', []) or []:
            if tool.get('name') == name:
                return tool
        return None

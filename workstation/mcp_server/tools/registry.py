from mcp_server.decorators import TOOL_REGISTRY, ToolStatus, tool_result, tool_with_meta, utc_now_iso
from mcp_server.study_policy import registry_view


@tool_with_meta()
def get_tools_registry() -> dict:
    """
    Return the full tool registry with metadata.
    Useful for debugging or inspecting what the MCP server currently exposes.
    """
    tools_info = registry_view(TOOL_REGISTRY)
    server_meta = {'name': 'llm-tools', 'generated_at': utc_now_iso(), 'count': len(tools_info)}
    return {'server': server_meta, 'tools': tools_info}


@tool_with_meta()
def list_categories() -> dict:
    """
    List available tool categories.
    Useful for browsing the tool inventory by domain.
    """
    categories = sorted({meta.get('meta', {}).get('category', 'uncategorized') for meta in registry_view(TOOL_REGISTRY)})
    return {'categories': categories, 'count': len(categories)}


@tool_with_meta()
def list_tools_by_category(category: str, include_meta: bool = True) -> dict:
    """
    List tools in `category`.
    Set `include_meta=False` for names only.
    """
    matching = [t for t in registry_view(TOOL_REGISTRY) if t.get('meta', {}).get('category', 'uncategorized') == category]
    if include_meta:
        return {'category': category, 'count': len(matching), 'tools': matching}
    return {'category': category, 'count': len(matching), 'tools': [t['name'] for t in matching]}

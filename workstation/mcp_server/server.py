from __future__ import annotations

import importlib
import logging
import os
from pathlib import Path
from types import ModuleType

import rclpy
from mcp.server.fastmcp import FastMCP
from workstation_common import configure_logging, get_logger

configure_logging(stream='stderr')
from mcp_server.decorators import TOOL_REGISTRY
from mcp_server.ros_utils import get_node
from mcp_server.study_policy import mode, query_policy

log = get_logger('mcp.server')
for logger_name in ('mcp', 'mcp.server', 'mcp.server.fastmcp', 'fastmcp', 'mcp.server.server'):
    try:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    except Exception:
        pass
domain_id = os.environ.get('ROS_DOMAIN_ID', '(default)')
log.info(f'Starting MCP server with ROS_DOMAIN_ID={domain_id}')
if not rclpy.ok():
    log.info('Initializing ROS2')
    rclpy.init()
_ros_node = get_node()
log.info('Shared ROS node ready')
mcp = FastMCP(name='llm-tools', instructions=('Robot control server with tools for navigation, manipulation, '
                                              'perception, face recognition, time/date and communication. Use get_tools_registry to discover tools.'), )
excluded_files = {'__init__.py'}
tool_modules: dict[str, ModuleType] = {}


def discover_and_import_tools() -> None:
    allowed = None
    if mode() == 'study':
        summary, error = query_policy()
        if error:
            raise RuntimeError(error[1])
        allowed = set(summary.get('allowed_tools') or [])
    tools_dir = Path(__file__).parent / 'tools'
    for py_file in sorted(tools_dir.glob('*.py')):
        if py_file.name in excluded_files:
            continue
        module_name = py_file.stem
        full_module = f'mcp_server.tools.{module_name}'
        try:
            module = importlib.import_module(full_module)
            tool_modules[module_name] = module
            log.info(f'Loaded tool module {module_name}')
            for tool_name, tool_meta in list(TOOL_REGISTRY.items()):
                if allowed is not None and tool_name not in allowed:
                    continue
                if not hasattr(module, tool_name):
                    continue
                wrapped_fn = getattr(module, tool_name)
                if not callable(wrapped_fn):
                    continue
                try:
                    mcp.tool(name=tool_name, description=tool_meta.get('description', ''))(wrapped_fn)
                except Exception:
                    pass
        except Exception as exc:
            log.exception(f'Failed to load tool module {module_name}: {exc}')
    log.info(f'Registered {len(TOOL_REGISTRY)} tools')


discover_and_import_tools()
if __name__ == '__main__':
    log.info('MCP server ready')
    mcp.run()

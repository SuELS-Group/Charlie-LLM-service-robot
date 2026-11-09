import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from workstation_common import get_logger
from workstation_common.config import load

config = load(Path(__file__).resolve().parent / 'config' / 'llm.yaml', profile_key='model_profiles')
log = get_logger('llm.mcp')


class MCPClient:

    def __init__(self, server_module: str | None = None, health_check_interval: float | None = None, ):
        self.server_module = server_module or config.mcp_server_module
        self.health_check_interval = (
            float(health_check_interval) if health_check_interval is not None else float(config.mcp_health_check_interval_s))
        self.client: Client | None = None
        self.registry: dict[str, Any] | None = None
        self.categories: list[str] = []
        self.llm_callable_categories: list[str] = []
        self.llm_callable_tools: list[dict[str, Any]] = []
        self._connected = False
        self._health_task: asyncio.Task | None = None
        self._reconnecting = False
        self._operation_in_progress = False
        self._stopping = False
        self._call_lock = asyncio.Lock()
        self._health_pause_depth = 0
        self._health_resume_at = 0.0
        self.cancel_check = None
        self.request_language = 'en'
        self.request_id = ''
        self.request_issued_at = None
        log.info(f'MCP client configured for module {self.server_module} '
                 f'(health_check_interval={self.health_check_interval:.1f}s)')

    @staticmethod
    def unwrap_call_tool_result(result):
        if isinstance(result, (dict, list, str, int, float, bool)) or result is None:
            return result
        if hasattr(result, 'model_dump'):
            dumped = result.model_dump()
            content = dumped.get('content', [])
            if content and isinstance(content[0], dict) and 'text' in content[0]:
                text = content[0]['text']
                try:
                    return json.loads(text)
                except Exception:
                    return text
            return dumped
        if hasattr(result, 'content'):
            content = result.content
            if content and hasattr(content[0], 'text'):
                text = content[0].text
                try:
                    return json.loads(text)
                except Exception:
                    return text
        return str(result)

    async def start(self) -> tuple[dict[str, Any], list[str], list[str], list[dict[str, Any]]]:
        self._stopping = False
        workspace_root = Path(__file__).resolve().parents[1]
        project_root = workspace_root.parent
        ros2_setup = '/opt/ros/humble/setup.bash'
        ament_ws_setup = workspace_root / 'ament_ws' / 'install' / 'setup.bash'
        robot_ws_setup = project_root / 'robot' / 'ros_ws' / 'install' / 'setup.bash'
        source_cmd = f'source {ros2_setup}'
        if ament_ws_setup.exists():
            source_cmd += f' && source {ament_ws_setup}'
        if robot_ws_setup.exists():
            source_cmd += f' && source {robot_ws_setup}'
        command = f'{source_cmd} && {sys.executable} -m {self.server_module}'
        ros_domain_id = os.environ.get('ROS_DOMAIN_ID', 'default')
        log.info(f'Starting MCP server {self.server_module} with ROS_DOMAIN_ID={ros_domain_id}')
        transport = StdioTransport(command='bash', args=['-lc',
                                                         command], cwd=str(workspace_root), env=os.environ.copy(), )
        self.client = Client(transport=transport)
        await self.client.__aenter__()
        self._connected = True
        self.registry = await self.call_tool('get_tools_registry', {})
        tools = self.registry.get('tools', [])
        self.categories = sorted({tool.get('meta', {}).get('category', 'uncategorized') for tool in tools})
        excluded_categories = {'system', 'health', 'registry'}
        self.llm_callable_categories = [category for category in self.categories if category not in excluded_categories]
        self.llm_callable_tools = [tool for tool in tools if
                                   tool.get('meta', {}).get('category', 'uncategorized') not in excluded_categories]
        log.info(f'Loaded {len(tools)} MCP tools in {len(self.categories)} categories; '
                 f'LLM-callable tools={len(self.llm_callable_tools)} categories={self.llm_callable_categories}')
        for tool in self.llm_callable_tools:
            tool_name = tool['name']
            category = tool['meta'].get('category', 'uncategorized')
            log.debug(f'Callable tool: {tool_name} category={category}')
        if self.health_check_interval > 0:
            self._health_task = asyncio.create_task(self._health_check_loop())
        return self.registry, self.categories, self.llm_callable_categories, self.llm_callable_tools

    async def stop(self):
        self._stopping = True
        self._connected = False
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None
        if not self.client:
            return
        try:
            await self.client.__aexit__(None, None, None)
        except Exception as exc:
            log.warning(f'Failed to shutdown MCP client cleanly: {exc}')
        finally:
            self.client = None
            self._connected = False
            log.info('MCP client stopped')

    async def _health_check_loop(self):
        consecutive_failures = 0
        while self._connected and not self._stopping:
            try:
                await asyncio.sleep(self.health_check_interval)
                if not self._connected or self._stopping:
                    break
                if self._health_pause_depth > 0:
                    consecutive_failures = 0
                    continue
                if time.monotonic() < self._health_resume_at:
                    consecutive_failures = 0
                    continue
                if self._operation_in_progress or self._reconnecting or self._call_lock.locked():
                    consecutive_failures = 0
                    continue
                await asyncio.wait_for(self.call_tool('ping', {}), timeout=float(config.mcp_health_check_timeout_s), )
                consecutive_failures = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_failures += 1
                if not self._operation_in_progress and not self._reconnecting and not self._stopping:
                    log.warning(f'MCP health check failed ({consecutive_failures}): {exc}')
                if (
                        consecutive_failures >= int(config.mcp_reconnect_attempts) and not self._reconnecting and not self._operation_in_progress and not self._stopping):
                    log.warning('Multiple MCP health checks failed, attempting reconnect')
                    await self._reconnect()

    async def _reconnect(self):
        if self._reconnecting or self._stopping:
            return
        self._reconnecting = True
        log.info('Starting MCP reconnection')
        max_attempts = int(config.mcp_reconnect_attempts)
        backoff_base = float(config.mcp_reconnect_backoff_base_s)
        for attempt in range(max_attempts):
            try:
                old_client = self.client
                self.client = None
                self._connected = False
                if old_client:
                    try:
                        await old_client.__aexit__(None, None, None)
                    except Exception:
                        pass
                await asyncio.sleep(backoff_base * (attempt + 1))
                log.info(f'MCP reconnect attempt {attempt + 1}/{max_attempts}')
                await self.start()
                log.info('MCP reconnection successful')
                self._reconnecting = False
                return
            except Exception as exc:
                log.warning(f'MCP reconnect attempt {attempt + 1} failed: {exc}')
        log.error('All MCP reconnection attempts failed')
        self._reconnecting = False

    def pause_health_checks(self) -> None:
        self._health_pause_depth += 1
        if self._health_pause_depth == 1:
            log.info('Paused MCP health checks during active request')

    def resume_health_checks(self, grace_seconds: float | None = None) -> None:
        if self._health_pause_depth <= 0:
            self._health_pause_depth = 0
        else:
            self._health_pause_depth -= 1
        if self._health_pause_depth == 0:
            pause = self.health_check_interval if grace_seconds is None else max(0.0, float(grace_seconds))
            self._health_resume_at = time.monotonic() + pause
            log.info(f'Resumed MCP health checks with {pause:.1f}s grace period')

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        if not self.client or not self._connected:
            raise RuntimeError('MCPClient not started or disconnected. Call await start() first.')
        async with self._call_lock:
            if self.cancel_check is not None and self.cancel_check() and name not in {'get_tools_registry', 'ping'}:
                return {'status': 'stopped', 'error': {'code': 'REQUEST_CANCELLED', 'message': 'Request cancelled before dispatch'}}
            if not self.client or not self._connected:
                raise RuntimeError('MCPClient not started or disconnected. Call await start() first.')
            self._operation_in_progress = True
            try:
                payload = args or {}
                wrapper_payload = payload if isinstance(payload, dict) and (
                        'args' in payload or 'kwargs' in payload) else {'args': [], 'kwargs': payload}
                wrapper_payload = dict(wrapper_payload)
                wrapper_payload['kwargs'] = {**wrapper_payload.get('kwargs', {}),
                    '_request_time': self.request_issued_at or time.time(),
                    '_request_language': self.request_language, '_request_id': self.request_id}
                log.info(f'MCP call start: {name} args={payload}')
                result = await self.client.call_tool(name, wrapper_payload)
                unwrapped = self.unwrap_call_tool_result(result)
                if isinstance(unwrapped, dict):
                    log.info(f'MCP call done: {name} status={unwrapped.get("status", "ok")} result={unwrapped.get("result")} error={unwrapped.get("error")}')
                else:
                    log.info(f'MCP call done: {name} result={unwrapped}')
                return unwrapped
            except Exception as exc:
                error_text = str(exc).lower()
                if (
                        'not connected' in error_text or 'context manager' in error_text or 'brokenresourceerror' in error_text or 'closed resource' in error_text):
                    self._connected = False
                log.warning(f'MCP call failed: {name} error={exc}')
                raise
            finally:
                self._operation_in_progress = False

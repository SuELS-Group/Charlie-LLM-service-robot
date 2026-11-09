from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from models import MachineType, ServiceStatus
from pydantic import BaseModel
from ssh_manager import ssh_manager
from web import state

logger = logging.getLogger(__name__)
router = APIRouter()
GREETING_MODE_PATH = state.CONFIG_DIR / 'greeting_mode.json'
GREETING_NODE_ID = 'greeting_mode'


class GreetingModeUpdate(BaseModel):
    enabled: bool


def _default_state() -> dict[str, Any]:
    return {'enabled': False, 'updated_at': None}


def _read_state() -> dict[str, Any]:
    if not GREETING_MODE_PATH.exists():
        return _default_state()
    try:
        data = json.loads(GREETING_MODE_PATH.read_text(encoding='utf-8'))
    except Exception as exc:
        logger.warning(f'Failed to read greeting mode state: {exc}')
        return _default_state()
    return {'enabled': bool(data.get('enabled', False)), 'updated_at': data.get('updated_at'), }


def _write_state(enabled: bool) -> dict[str, Any]:
    GREETING_MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {'enabled': bool(enabled), 'updated_at': datetime.now(timezone.utc).isoformat(), }
    tmp_path = GREETING_MODE_PATH.with_suffix('.json.tmp')
    tmp_path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    tmp_path.replace(GREETING_MODE_PATH)
    return data


def _service_status_value(raw_status: Any) -> str:
    return getattr(raw_status, 'value', str(raw_status))


def _find_greeting_node() -> tuple[str | None, Any | None]:
    for machine_id, machine in state.machines_config.items():
        if machine.type != MachineType.WORKSTATION:
            continue
        node_group = state.nodes_config.get(machine.type.value, [])
        nodes = node_group.get('nodes', node_group) if isinstance(node_group, dict) else node_group
        node = next((item for item in nodes if item.id == GREETING_NODE_ID), None)
        if node:
            return machine_id, node
    return None, None


async def _get_greeting_node_status() -> dict[str, Any]:
    machine_id, node = _find_greeting_node()
    if not machine_id or not node:
        return {'node_running': False, 'node_status': 'missing', 'machine_id': machine_id, 'service_name': None, }
    if not ssh_manager.is_connected(machine_id):
        return {
            'node_running': False,
            'node_status': 'disconnected',
            'machine_id': machine_id,
            'service_name': node.service_name, }
    try:
        status_data = await ssh_manager.get_service_status(machine_id, node.service_name)
    except Exception as exc:
        logger.warning(f'Failed to read greeting node status: {exc}')
        return {
            'node_running': False,
            'node_status': 'unknown',
            'machine_id': machine_id,
            'service_name': node.service_name,
            'error': str(exc), }
    service_status = status_data.get('status')
    service_status_value = _service_status_value(service_status)
    return {
        'node_running': service_status == ServiceStatus.RUNNING or service_status_value == ServiceStatus.RUNNING.value,
        'node_status': service_status_value,
        'machine_id': machine_id,
        'service_name': node.service_name,
        'active_state': status_data.get('active_state'),
        'sub_state': status_data.get('sub_state'),
        'error': status_data.get('error'), }


@router.get('/api/greeting-mode')
async def get_greeting_mode():
    data = _read_state()
    data.update(await _get_greeting_node_status())
    return data


@router.post('/api/greeting-mode')
async def set_greeting_mode(update: GreetingModeUpdate):
    node_status = await _get_greeting_node_status()
    if update.enabled and not node_status.get('node_running'):
        raise HTTPException(status_code=409, detail='Greeting node is not running')
    try:
        data = _write_state(update.enabled)
    except Exception as exc:
        logger.error(f'Failed to write greeting mode state: {exc}')
        raise HTTPException(status_code=500, detail=str(exc))
    data.update(node_status)
    return data

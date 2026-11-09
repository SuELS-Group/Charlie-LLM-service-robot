from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Literal
from urllib.request import Request, urlopen

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel
from models import MachineType
from web import state

logger = logging.getLogger(__name__)
router = APIRouter()


class BatteryAnnouncementRequest(BaseModel):
    mode: Literal['reduced', 'full', 'off']


def _battery_source(machine_id):
    machine = state.machines_config.get(machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail='Machine not found')
    sources = [(source_id, config) for source_id, config in state.machines_config.items()
               if config.type == MachineType.WORKSTATION and config.ros_domain_id == machine.ros_domain_id]
    return sources[0] if len(sources) == 1 else None


def _read_battery(host: str) -> dict:
    authority = f'[{host}]' if ':' in host else host
    with urlopen(f'http://{authority}:{state.WEB_BRIDGE_HTTP_PORT}/api/battery', timeout=2) as response:
        body = response.read(65537)
    if len(body) > 65536:
        raise ValueError('Battery response is too large')
    payload = json.loads(body)
    if not isinstance(payload, dict) or payload.get('type') != 'battery':
        raise ValueError('Invalid battery response')
    mode = payload.get('announcement_mode')
    settings = {'announcement_mode': mode} if isinstance(mode, str) and mode in {'reduced', 'full', 'off'} else {}
    payload.pop('announcement_mode', None)
    payload.update(settings)
    # Apply source freshness again at the proxy boundary. A saved response or
    # delayed connection must never turn stale measurements into live telemetry.
    stamp = payload.get('sampled_at')
    fresh = (isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
             and math.isfinite(stamp) and -.5 <= time.time() - stamp <= 3)
    if payload.get('available') is not True or not fresh:
        return {'type': 'battery', 'available': False, 'reason': 'stale_or_missing_data', **settings}
    percentage = payload.get('percentage')
    if percentage is not None and (not isinstance(percentage, (int, float))
                                   or isinstance(percentage, bool) or not math.isfinite(percentage)
                                   or not 0 <= percentage <= 100):
        raise ValueError('Invalid battery percentage')
    # Reject NaN/Infinity anywhere, including history, before FastAPI encodes it.
    json.dumps(payload, allow_nan=False)
    return payload


@router.get('/api/machines/{machine_id}/battery')
async def get_battery(machine_id: str, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    source = _battery_source(machine_id)
    if source is None:
        return {'type': 'battery', 'available': False, 'reason': 'battery_source_not_unique'}
    source_id, source = source
    try:
        payload = await asyncio.to_thread(_read_battery, source.host)
    except Exception as exc:
        logger.debug('Battery bridge unavailable on %s: %s', source_id, exc)
        payload = {'type': 'battery', 'available': False, 'reason': 'bridge_unavailable'}
    return {**payload, 'machine_id': machine_id, 'source_machine_id': source_id}


def _write_announcement_mode(host: str, mode: str) -> dict:
    authority = f'[{host}]' if ':' in host else host
    request = Request(f'http://{authority}:{state.WEB_BRIDGE_HTTP_PORT}/api/battery/announcements',
        data=json.dumps({'mode': mode}).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    with urlopen(request, timeout=3) as response:
        body = response.read(65537)
    if len(body) > 65536:
        raise ValueError('Battery settings response is too large')
    payload = json.loads(body)
    if not isinstance(payload, dict) or payload.get('mode') != mode:
        raise ValueError('Battery announcement setting was not confirmed')
    return {'mode': mode}


@router.post('/api/machines/{machine_id}/battery/announcements')
async def set_battery_announcements(machine_id: str, request: BatteryAnnouncementRequest, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    source = _battery_source(machine_id)
    if source is None:
        raise HTTPException(status_code=409, detail='Battery source is not unique')
    source_id, config = source
    try:
        result = await asyncio.to_thread(_write_announcement_mode, config.host, request.mode)
    except Exception as exc:
        logger.debug('Cannot save battery announcement mode on %s: %s', source_id, exc)
        raise HTTPException(status_code=502, detail='Battery announcement mode could not be saved') from exc
    return {**result, 'source_machine_id': source_id}

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from models import MachineType
from web import state

logger = logging.getLogger(__name__)
router = APIRouter()


def _workstation_sources() -> list[tuple[str, str]]:
    return [(machine_id, config.host) for machine_id, config in state.machines_config.items() if
        config.type == MachineType.WORKSTATION]


async def _forward_source(machine_id: str, host: str, output: asyncio.Queue[dict[str, Any]]) -> None:
    uri = f"ws://{host}:{state.ROBOT_CONTROL_WS_PORT}{state.VOICE_ACTIVITY_WS_PATH}"
    while True:
        try:
            async with websockets.connect(uri, open_timeout=3, ping_interval=20, ping_timeout=20) as upstream:
                async for raw_message in upstream:
                    payload = json.loads(raw_message)
                    if payload.get("type") != "voice_activity":
                        continue
                    payload["machine_id"] = machine_id
                    await output.put(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Voice activity bridge unavailable on %s: %s", machine_id, exc)
            await output.put({
                "type": "voice_activity",
                "machine_id": machine_id,
                "available": False,
                "active": False, })
            await asyncio.sleep(2)


@router.websocket("/ws/voice-activity")
async def voice_activity_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    output: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
    tasks = [asyncio.create_task(_forward_source(machine_id, host, output)) for machine_id, host in
        _workstation_sources()]
    if not tasks:
        await websocket.send_json({
            "type": "voice_activity",
            "machine_id": "workstation",
            "available": False,
            "active": False, })
    try:
        while True:
            try:
                payload = await asyncio.wait_for(output.get(), timeout=10)
            except asyncio.TimeoutError:
                payload = {"type": "heartbeat"}
            await websocket.send_json(payload)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

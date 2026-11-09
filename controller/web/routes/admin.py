from __future__ import annotations

import asyncio
import logging
from typing import Dict

import diagrams.ros_diagram_creator as ros_diagram_creator
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from generate_services import ensure_loaded_generator_is_current, generate_all_services
from key_manager import get_key_path, is_local_machine, keys_exist, setup_keys_from_api, verify_key_connectivity
from machine_private_config import hide_machine_in_private_config
from models import DiscoveryServerStatus
from pydantic import BaseModel
from ssh_manager import ssh_manager
from web import state
from web.config import detect_private_discovery_ip, load_machines_config, reload_configs
from web.lifecycle import connect_all_machines

logger = logging.getLogger(__name__)
router = APIRouter()


def require_current_service_generator() -> None:
    try:
        ensure_loaded_generator_is_current()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/config/reload")
async def reload_config():
    try:
        reload_configs()
        return {"status": "success", "message": "Configuration reloaded"}
    except Exception as e:
        logger.error(f"Error reloading config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/discovery/server", response_model=DiscoveryServerStatus)
async def get_discovery_server():
    return state.discovery_manager.status()


@router.post("/api/discovery/server/restart", response_model=DiscoveryServerStatus)
async def restart_discovery_server():
    return state.discovery_manager.restart()


@router.get("/api/ssh/status")
async def ssh_key_status():
    has_keys = keys_exist()
    machines_info = []
    for mid, cfg in state.machines_config.items():
        machines_info.append({
            "id": mid,
            "name": cfg.name,
            "host": cfg.host,
            "user": cfg.user,
            "is_local": is_local_machine(cfg.host), })
    connectivity = {}
    if has_keys:
        connectivity = await verify_key_connectivity(state.machines_config)
    return {
        "keys_exist": has_keys,
        "key_path": str(get_key_path()) if has_keys else None,
        "machines": machines_info,
        "connectivity": connectivity, }


class SSHSetupRequest(BaseModel):
    credentials: Dict[str, str]
    users: Dict[str, str] = {}


@router.post("/api/ssh/setup")
async def ssh_setup(request: SSHSetupRequest):
    try:
        results = await setup_keys_from_api(state.machines_config, request.credentials, request.users)
        state.machines_config = load_machines_config()
        await ssh_manager.disconnect_all()
        asyncio.create_task(connect_all_machines())
        return {"status": "success", "results": {mid: {"ok": ok, "error": err} for mid, (ok, err) in results.items()}, }
    except Exception as e:
        logger.error(f"SSH setup error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/machines/{machine_id}/hide")
async def hide_machine(machine_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not hide_machine_in_private_config(state.CONFIG_DIR, machine_id):
        raise HTTPException(status_code=500, detail=f"Failed to hide machine {machine_id}")
    await ssh_manager.disconnect(machine_id, suppress_reconnect=True)
    reload_configs()
    return {"status": "success", "message": f"Machine {machine_id} hidden"}


@router.get("/api/services/generate")
@router.post("/api/services/generate")
async def api_generate_services():
    require_current_service_generator()
    try:
        result = generate_all_services(state.CONFIG_DIR, state.GENERATED_DIR, advertise_host_override=detect_private_discovery_ip(), )
        summary = {mid: [s["service_name"] for s in services] for mid, services in result.items()}
        total = sum(len(v) for v in result.values())
        return {"status": "success", "generated": summary, "total": total, }
    except Exception as e:
        logger.error(f"Service generation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/services/deploy/{machine_id}")
async def api_deploy_services(machine_id: str):
    require_current_service_generator()
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    machine_dir = state.GENERATED_DIR / machine_id
    if not machine_dir.exists():
        raise HTTPException(status_code=404, detail=f"No generated services for {machine_id}. Generate first.", )
    services = []
    for service_file in sorted(machine_dir.glob("*.service")):
        services.append({
            "filename": service_file.name,
            "service_name": service_file.stem,
            "content": service_file.read_text(), })
    if not services:
        raise HTTPException(status_code=404, detail=f"No .service files found for {machine_id}", )
    success, message = await ssh_manager.deploy_service_files(machine_id, services)
    return {
        "status": "success" if success else "error",
        "message": message,
        "machine_id": machine_id,
        "services_deployed": [s["service_name"] for s in services] if success else [], }


@router.post("/api/services/generate-and-deploy")
async def api_generate_and_deploy():
    require_current_service_generator()
    try:
        result = generate_all_services(state.CONFIG_DIR, state.GENERATED_DIR, advertise_host_override=detect_private_discovery_ip(), )
    except Exception as e:
        logger.error(f"Service generation error: {e}")
        raise HTTPException(status_code=500, detail=f"Generation failed: {e}")
    deploy_results = {}
    for machine_id, services in result.items():
        if not ssh_manager.is_connected(machine_id):
            deploy_results[machine_id] = {
                "status": "skipped",
                "message": "Not connected",
                "services": [s["service_name"] for s in services], }
            continue
        success, message = await ssh_manager.deploy_service_files(machine_id, services)
        deploy_results[machine_id] = {
            "status": "success" if success else "error",
            "message": message,
            "services": [s["service_name"] for s in services], }
    return {"status": "success", "results": deploy_results, }


@router.get("/api/services/status")
async def api_services_status():
    generated = {}
    if state.GENERATED_DIR.exists():
        for machine_dir in state.GENERATED_DIR.iterdir():
            if machine_dir.is_dir() and machine_dir.name in state.machines_config:
                service_files = sorted(machine_dir.glob("*.service"))
                generated[machine_dir.name] = [f.stem for f in service_files]
    return {
        "machines": {mid: {
            "name": cfg.name,
            "type": cfg.type.value,
            "connected": ssh_manager.is_connected(mid),
            "generated_services": generated.get(mid, []), } for mid, cfg in state.machines_config.items()}, }


@router.post("/api/generate_ros_graph")
async def generate_ros_graph():
    state.DIAGRAMS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        loop = asyncio.get_event_loop()
        exit_code = await loop.run_in_executor(None, ros_diagram_creator.execute)
        if exit_code != 0:
            raise HTTPException(status_code=500, detail="ROS graph generation failed")
        return {"status": "success", "message": "ROS graph generated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating ROS graph: {e}")
        raise HTTPException(status_code=500, detail=f"ROS graph generation error: {str(e)}")


@router.get("/api/ros_graph/pdf")
async def get_ros_graph_pdf():
    if not state.PDF_PATH.exists():
        raise HTTPException(status_code=404, detail="No generated PDF found")
    return FileResponse(path=str(state.PDF_PATH), media_type="application/pdf", filename="graph.pdf", )


@router.get("/", response_class=HTMLResponse)
async def serve_frontend():
    index_file = state.STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse(content="""
    <!DOCTYPE html>
    <html>
    <head>
        <title>ROS2 Robot Controller</title>
        <meta charset="utf-8">
        <meta http-equiv="refresh" content="0; url=/static/index.html">
    </head>
    <body>
        <p>Redirecting to <a href="/static/index.html">Controller UI</a>...</p>
    </body>
    </html>
    """)

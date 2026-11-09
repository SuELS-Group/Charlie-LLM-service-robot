from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from typing import Dict, List, Optional

from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile, WebSocket
from models import (CommandResult, LlmInputConnectionInfo, MachineConfig, MachineStatus, NodeConfig, NodeStatus, RobotControlConnectionInfo, ServiceStatus, SystemInfo, )
from pydantic import BaseModel
from ssh_manager import ssh_manager
from web import state
from web.config import get_log_lines_default

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/machines", response_model=List[MachineStatus])
async def list_machines():
    result = []
    for machine_id, config in state.machines_config.items():
        connected = ssh_manager.is_connected(machine_id)
        git_branch = None
        git_error = None
        if connected:
            git_branch, git_error = await _get_machine_git_branch(machine_id, config)
        result.append(MachineStatus(id=machine_id, name=config.name, host=config.host, connected=connected, type=config.type, ros_domain_id=config.ros_domain_id, git_branch=git_branch, git_error=git_error, ))
    return result


@router.post("/api/machines/{machine_id}/connect")
async def connect_machine(machine_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    config = state.machines_config[machine_id]
    if not config.user:
        raise HTTPException(status_code=400, detail=f"Machine {machine_id} has no SSH user configured")
    success = await ssh_manager.connect(machine_id, config)
    if success:
        return {"status": "connected", "message": f"Connected to {machine_id}"}
    else:
        raise HTTPException(status_code=500, detail=f"Failed to connect to {machine_id}")


@router.post("/api/machines/{machine_id}/disconnect")
async def disconnect_machine(machine_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    await ssh_manager.disconnect(machine_id, suppress_reconnect=True)
    return {"status": "disconnected", "message": f"Disconnected from {machine_id}"}


@router.get("/api/machines/{machine_id}/status")
async def get_machine_status(machine_id: str) -> MachineStatus:
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    config = state.machines_config[machine_id]
    connected = ssh_manager.is_connected(machine_id)
    error = None
    if not connected:
        error = "Not connected"
    git_branch = None
    git_error = None
    if connected:
        git_branch, git_error = await _get_machine_git_branch(machine_id, config)
    return MachineStatus(id=machine_id, name=config.name, host=config.host, connected=connected, error=error, type=config.type, ros_domain_id=config.ros_domain_id, git_branch=git_branch, git_error=git_error, )


@router.get("/api/machines/{machine_id}/nodes", response_model=List[NodeConfig])
async def list_machine_nodes(machine_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    machine_type = state.machines_config[machine_id].type.value
    return state.nodes_config.get(machine_type, [])


@router.get("/api/machines/{machine_id}/nodes/{node_id}/status", response_model=NodeStatus)
async def get_node_status(machine_id: str, node_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    machine_type = state.machines_config[machine_id].type.value
    nodes = state.nodes_config.get(machine_type, [])
    node = next((n for n in nodes if n.id == node_id), None)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    try:
        status_data = await ssh_manager.get_service_status(machine_id, node.service_name)
        return _node_status_from_data(node, status_data)
    except Exception as e:
        logger.error(f"Error getting status for {node_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/machines/{machine_id}/nodes/status", response_model=List[NodeStatus])
async def get_all_nodes_status(machine_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    machine_type = state.machines_config[machine_id].type.value
    nodes = state.nodes_config.get(machine_type, [])
    results = []
    for node in nodes:
        try:
            status_data = await ssh_manager.get_service_status(machine_id, node.service_name)
            results.append(_node_status_from_data(node, status_data))
        except Exception as e:
            logger.error(f"Error getting status for {node.id}: {e}")
            results.append(NodeStatus(id=node.id, name=node.name, description=node.description, service_name=node.service_name, status=ServiceStatus.UNKNOWN, active_state="error", sub_state="", error=str(e), ))
    return results


def _node_status_from_data(node: NodeConfig, status_data: Dict[str, object]) -> NodeStatus:
    return NodeStatus(id=node.id, name=node.name, description=node.description, service_name=node.service_name, status=
    status_data["status"], active_state=status_data["active_state"], sub_state=status_data[
        "sub_state"], exit_code=status_data.get("exit_code"), pid=status_data.get("pid"), memory_current=status_data.get("memory_current"), started_at=status_data.get("started_at"), error=status_data.get("error"), )


async def _wait_for_service_running(machine_id: str, service_name: str, timeout_seconds: float = 120.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        try:
            status_data = await ssh_manager.get_service_status(machine_id, service_name)
            if status_data.get("status") == ServiceStatus.RUNNING:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


async def _start_node_and_dependencies(machine_id: str, node: NodeConfig, node_map: Dict[
    str, NodeConfig], started: set, visiting: set, ):
    if node.id in started:
        return
    if node.id in visiting:
        raise RuntimeError(f"Dependency cycle detected at {node.id}")
    visiting.add(node.id)
    for dep_id in node.dependencies:
        dep_node = node_map.get(dep_id)
        if not dep_node:
            raise RuntimeError(f"Dependency {dep_id} not found for {node.id}")
        await _start_node_and_dependencies(machine_id, dep_node, node_map, started, visiting)
    visiting.remove(node.id)
    status_data = await ssh_manager.get_service_status(machine_id, node.service_name)
    if status_data.get("status") == ServiceStatus.RUNNING:
        started.add(node.id)
        return
    success, message = await ssh_manager.start_service(machine_id, node.service_name)
    if not success:
        raise RuntimeError(message)
    if node.startup_delay > 0:
        await asyncio.sleep(node.startup_delay)
    if not await _wait_for_service_running(machine_id, node.service_name):
        raise RuntimeError(f"Service {node.service_name} did not reach running state")
    started.add(node.id)


@router.post("/api/machines/{machine_id}/nodes/{node_id}/start", response_model=CommandResult)
async def start_node(machine_id: str, node_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    machine_type = state.machines_config[machine_id].type.value
    nodes = state.nodes_config.get(machine_type, [])
    node = next((n for n in nodes if n.id == node_id), None)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    node_map = {n.id: n for n in nodes}
    try:
        await _start_node_and_dependencies(machine_id, node, node_map, set(), set())
        return CommandResult(success=True, message=f"Service {node.service_name} started")
    except Exception as e:
        logger.error(f"Error starting {node_id}: {e}")
        return CommandResult(success=False, message=str(e))


@router.post("/api/machines/{machine_id}/nodes/{node_id}/stop", response_model=CommandResult)
async def stop_node(machine_id: str, node_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    machine_type = state.machines_config[machine_id].type.value
    nodes = state.nodes_config.get(machine_type, [])
    node = next((n for n in nodes if n.id == node_id), None)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    try:
        success, message = await ssh_manager.stop_service(machine_id, node.service_name)
        return CommandResult(success=success, message=message)
    except Exception as e:
        logger.error(f"Error stopping {node_id}: {e}")
        return CommandResult(success=False, message=str(e))


@router.post("/api/machines/{machine_id}/nodes/{node_id}/restart", response_model=CommandResult)
async def restart_node(machine_id: str, node_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    machine_type = state.machines_config[machine_id].type.value
    nodes = state.nodes_config.get(machine_type, [])
    node = next((n for n in nodes if n.id == node_id), None)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    try:
        success, message = await ssh_manager.restart_service(machine_id, node.service_name)
        return CommandResult(success=success, message=message)
    except Exception as e:
        logger.error(f"Error restarting {node_id}: {e}")
        return CommandResult(success=False, message=str(e))


@router.get("/api/machines/{machine_id}/nodes/{node_id}/logs")
async def get_node_logs(machine_id: str, node_id: str, lines: Optional[
    int] = Query(default=None, ge=1, le=10000), since: Optional[str] = None):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    machine_type = state.machines_config[machine_id].type.value
    nodes = state.nodes_config.get(machine_type, [])
    node = next((n for n in nodes if n.id == node_id), None)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    resolved_lines = lines if lines is not None else get_log_lines_default()
    try:
        logs = await ssh_manager.get_service_logs(machine_id, node.service_name, resolved_lines, since)
        return {"logs": logs, "service_name": node.service_name}
    except Exception as e:
        logger.error(f"Error getting logs for {node_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/machines/{machine_id}/nodes/start-all", response_model=List[CommandResult])
async def start_all_nodes(machine_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    machine_type = state.machines_config[machine_id].type.value
    nodes = state.nodes_config.get(machine_type, [])
    results = []
    for node in nodes:
        try:
            success, message = await ssh_manager.start_service(machine_id, node.service_name)
            results.append(CommandResult(success=success, message=f"{node.name}: {message}"))
            if node.startup_delay > 0:
                await asyncio.sleep(node.startup_delay)
        except Exception as e:
            results.append(CommandResult(success=False, message=f"{node.name}: {str(e)}"))
    return results


@router.post("/api/machines/{machine_id}/nodes/stop-all", response_model=List[CommandResult])
async def stop_all_nodes(machine_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    machine_type = state.machines_config[machine_id].type.value
    nodes = state.nodes_config.get(machine_type, [])
    kill_cmd = ("units=$(systemctl --user list-units --no-legend 'ros2-*.service' 2>/dev/null"
                " | awk '{print $1}');"
                " [ -n \"$units\" ] && {"
                "   systemctl --user stop $units 2>/dev/null || true;"
                "   systemctl --user reset-failed $units 2>/dev/null || true;"
                " } || true")
    try:
        await ssh_manager.execute(machine_id, kill_cmd, timeout=15)
    except Exception as e:
        logger.error(f"Error in stop-all for {machine_id}: {e}")
    return [CommandResult(success=True, message=f"{n.name}: stopped") for n in nodes]


@router.get("/api/machines/{machine_id}/system-info", response_model=SystemInfo)
async def get_system_info(machine_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    try:
        _, stdout, _ = await ssh_manager.execute(machine_id, _system_info_command(), timeout=5)
        return SystemInfo(**_parse_system_info(stdout))
    except TimeoutError as exc:
        logger.warning("System info query timed out for %s; keeping the SSH connection", machine_id)
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except Exception as e:
        logger.error(f"Error getting system info for {machine_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _system_info_command() -> str:
    cpu_command = _cpu_usage_command()
    return ("printf 'hostname=%s\\n' \"$(hostname 2>/dev/null)\"; "
            "printf 'kernel=%s\\n' \"$(uname -r 2>/dev/null)\"; "
            "printf 'uptime=%s\\n' \"$(uptime -p 2>/dev/null)\"; "
            f"cpu_usage=$({cpu_command}); printf 'cpu_usage=%s\\n' \"$cpu_usage\"; "
            "free -h --si 2>/dev/null | awk 'NR == 2 {"
            "print \"memory_total=\" $2; print \"memory_used=\" $3; print \"memory_available=\" $7}'; "
            "df -h / --output=size,used,avail,pcent 2>/dev/null | awk 'NR == 2 {"
            "print \"disk_total=\" $1; print \"disk_used=\" $2; "
            "print \"disk_available=\" $3; print \"disk_use_percent=\" $4}'; "
            f"{{ {_gpu_usage_command()}; }} | awk -F, 'NR == 1 && NF >= 3 {{"
            "gsub(/^[[:space:]]+|[[:space:]]+$/, \"\", $1); "
            "gsub(/^[[:space:]]+|[[:space:]]+$/, \"\", $2); "
            "gsub(/^[[:space:]]+|[[:space:]]+$/, \"\", $3); "
            "print \"gpu_usage=\" $1 \"%\"; print \"gpu_memory_used=\" $2 \" MiB\"; "
            "print \"gpu_memory_total=\" $3 \" MiB\"}'")


def _parse_system_info(output: str) -> dict[str, str]:
    info: dict[str, str] = {}
    known_fields = set(SystemInfo.model_fields)
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        value = value.strip()
        if separator and key in known_fields and value:
            info[key] = value
    if "uptime" in info:
        info["uptime"] = info["uptime"].removeprefix("up ")
    return info


def _cpu_usage_command() -> str:
    return ("read _ user nice system idle iowait irq softirq steal _ < /proc/stat; "
            "idle1=$((idle+iowait)); "
            "total1=$((user+nice+system+idle+iowait+irq+softirq+steal)); "
            "sleep 0.2; "
            "read _ user nice system idle iowait irq softirq steal _ < /proc/stat; "
            "idle2=$((idle+iowait)); "
            "total2=$((user+nice+system+idle+iowait+irq+softirq+steal)); "
            "awk -v idle_delta=$((idle2-idle1)) -v total_delta=$((total2-total1)) "
            "'BEGIN { if (total_delta > 0) printf \"%.0f%%\", "
            "100 * (1 - idle_delta / total_delta) }'")


def _gpu_usage_command() -> str:
    return ("command -v nvidia-smi >/dev/null 2>&1 && "
            "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total "
            "--format=csv,noheader,nounits 2>/dev/null | head -1 || true")


@router.websocket("/ws/logs/{machine_id}/{node_id}")
async def websocket_logs(websocket: WebSocket, machine_id: str, node_id: str):
    current_task = asyncio.current_task()
    if current_task:
        state.background_tasks.add(current_task)
    try:
        await websocket.accept()
        if machine_id not in state.machines_config:
            await websocket.send_json({"error": f"Machine {machine_id} not found"})
            await websocket.close()
            return
        if not ssh_manager.is_connected(machine_id):
            await websocket.send_json({"error": f"Not connected to {machine_id}"})
            await websocket.close()
            return
        machine_type = state.machines_config[machine_id].type.value
        nodes = state.nodes_config.get(machine_type, [])
        node = next((n for n in nodes if n.id == node_id), None)
        if not node:
            await websocket.send_json({"error": f"Node {node_id} not found"})
            await websocket.close()
            return
        logger.info(f"Starting log stream for {machine_id}/{node_id}")
        try:
            recent_logs = await ssh_manager.get_service_logs(machine_id, node.service_name, get_log_lines_default(), )
            if recent_logs:
                await websocket.send_json({"type": "history", "logs": recent_logs})
            async for log_line in ssh_manager.stream_service_logs(machine_id, node.service_name):
                await websocket.send_json({"type": "log", "message": log_line})
        except asyncio.CancelledError:
            logger.debug(f"Log stream cancelled for {machine_id}/{node_id}")
        except Exception as e:
            logger.error(f"Error in log stream for {machine_id}/{node_id}: {e}")
            try:
                await websocket.send_json({"error": str(e)})
            except Exception:
                pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass
    finally:
        if current_task:
            state.background_tasks.discard(current_task)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _machine_project_path(config: MachineConfig) -> str:
    project_path = config.project_path or "~/pg-llm-service-robot"
    if project_path.startswith("~") and config.user:
        return project_path.replace("~", f"/home/{config.user}", 1)
    return project_path


async def _get_machine_git_branch(machine_id: str, config: MachineConfig) -> tuple[Optional[str], Optional[str]]:
    if not ssh_manager.is_connected(machine_id):
        return None, None
    project_path = _machine_project_path(config)
    cmd = (f"cd {_shell_quote(project_path)} && "
           "git rev-parse --is-inside-work-tree >/dev/null 2>&1 && "
           "git rev-parse --abbrev-ref HEAD")
    try:
        exit_code, stdout, stderr = await ssh_manager.execute(machine_id, cmd, timeout=15)
    except Exception as e:
        return None, str(e)
    if exit_code != 0:
        detail = (stderr or stdout or "git repository not available").strip()
        return None, detail
    branch = stdout.strip().splitlines()[-1].strip() if stdout.strip() else ""
    if not branch:
        return None, "Unable to determine git branch"
    return branch, None


@router.post("/api/machines/{machine_id}/git/pull")
async def pull_machine_git(machine_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    config = state.machines_config[machine_id]
    project_path = _machine_project_path(config)
    cmd = f"cd {_shell_quote(project_path)} && git pull --ff-only"
    try:
        exit_code, stdout, stderr = await ssh_manager.execute(machine_id, cmd, timeout=120)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if exit_code != 0:
        detail = (stderr or stdout or "git pull failed").strip()
        manual = (f"Git pull failed. Resolve manually on {machine_id}: "
                  f"ssh to the machine, cd {project_path}, inspect local changes/conflicts, then run git pull.")
        raise HTTPException(status_code=409, detail=f"{manual} Details: {detail}")
    return {
        "status": "success",
        "message": f"Git pull completed on {machine_id}",
        "output": stdout.strip() if stdout else "", }


def _validate_volume_level(level):
    try:
        val = int(level)
        return 0 <= val <= 100
    except Exception:
        return False


def _resolve_workstation_for_robot(robot_id: str) -> tuple[str, MachineConfig]:
    robot_cfg = state.machines_config.get(robot_id)
    if robot_cfg is None:
        raise HTTPException(status_code=404, detail=f"Machine {robot_id} not found")
    if str(robot_cfg.type) != 'MachineType.ROBOT' and str(robot_cfg.type).lower() != 'robot':
        raise HTTPException(status_code=400, detail=f"Machine {robot_id} is not a robot")
    matches: list[tuple[str, MachineConfig]] = []
    for machine_id, machine_cfg in state.machines_config.items():
        is_matching_workstation = (
                str(machine_cfg.type).lower().endswith('workstation') and machine_cfg.ros_domain_id == robot_cfg.ros_domain_id)
        if is_matching_workstation:
            matches.append((machine_id, machine_cfg))
    connected = [(mid, cfg) for mid, cfg in matches if ssh_manager.is_connected(mid)]
    if not connected:
        raise HTTPException(status_code=503, detail=f"No connected workstation found for robot {robot_id} on ROS domain {robot_cfg.ros_domain_id}", )
    for preferred in ('workstation', 'laptop'):
        for item in connected:
            if item[0] == preferred:
                return item
    return connected[0]


class PasswordRequest(BaseModel):
    password: str


@router.post("/api/machines/{machine_id}/restart", response_model=CommandResult)
async def restart_machine(machine_id: str, req: PasswordRequest):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    password = req.password
    if not password:
        raise HTTPException(status_code=400, detail="Password required")
    try:
        cmd = f"echo {shlex_quote(password)} | sudo -S systemctl reboot"
        exit_code, stdout, stderr = await ssh_manager.execute(machine_id, cmd)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if exit_code != 0:
        detail = (stderr or stdout or "Reboot failed").strip()
        manual = (f"Reboot failed. Resolve manually on {machine_id}: ")
        return CommandResult(success=False, message=f"{manual} Details: {detail}", exit_code=exit_code, stdout=stdout, stderr=stderr, )
    return CommandResult(success=True, message=f"Restart completed on {machine_id}", exit_code=exit_code, stdout=stdout, stderr=stderr, )


@router.post("/api/machines/{machine_id}/shutdown", response_model=CommandResult)
async def shutdown_machine(machine_id: str, req: PasswordRequest):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    password = req.password
    if not password:
        raise HTTPException(status_code=400, detail="Password required")
    try:
        cmd = f"echo {shlex_quote(password)} | sudo -S systemctl poweroff"
        exit_code, stdout, stderr = await ssh_manager.execute(machine_id, cmd)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if exit_code != 0:
        detail = (stderr or stdout or "Shutdown failed").strip()
        manual = (f"Shutdown failed. Resolve manually on {machine_id}: ")
        return CommandResult(success=False, message=f"{manual} Details: {detail}", exit_code=exit_code, stdout=stdout, stderr=stderr, )
    return CommandResult(success=True, message=f"Shutdown completed on {machine_id}", exit_code=exit_code, stdout=stdout, stderr=stderr, )


def shlex_quote(s):
    return shlex.quote(s)


@router.get("/api/machines/{machine_id}/robot-control/connection", response_model=RobotControlConnectionInfo)
async def robot_control_connection(machine_id: str):
    workstation_id, workstation_cfg = _resolve_workstation_for_robot(machine_id)
    return RobotControlConnectionInfo(workstation_id=workstation_id, host=workstation_cfg.host, port=state.ROBOT_CONTROL_WS_PORT, path=state.ROBOT_CONTROL_WS_PATH, robot_id=machine_id, ros_domain_id=
    state.machines_config[machine_id].ros_domain_id, )


@router.get("/api/llm-input/connection", response_model=LlmInputConnectionInfo)
async def llm_input_connection():
    workstations = [(machine_id, cfg) for machine_id, cfg in state.machines_config.items() if
                    str(cfg.type).lower().endswith('workstation')]
    if not workstations:
        raise HTTPException(status_code=404, detail="No workstation configured")
    connected = [(machine_id, cfg) for machine_id, cfg in workstations if ssh_manager.is_connected(machine_id)]
    machine_id, cfg = (connected or workstations)[0]
    return LlmInputConnectionInfo(workstation_id=machine_id, host=cfg.host, port=state.WEB_BRIDGE_HTTP_PORT, path=state.LLM_INPUT_HTTP_PATH, ros_domain_id=cfg.ros_domain_id, )


@router.post("/api/machines/{machine_id}/volume", response_model=CommandResult)
async def set_volume(machine_id: str, volume: int = Body(..., embed=True, description="Volume level (0-100)")):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    if not _validate_volume_level(volume):
        raise HTTPException(status_code=400, detail="Volume must be between 0 and 100")
    try:
        cmd = f"amixer sset 'Master' {volume}%"
        exit_code, stdout, stderr = await ssh_manager.execute(machine_id, cmd)
        return CommandResult(success=exit_code == 0, message=f"Volume set to {volume}%", exit_code=exit_code, stdout=stdout, stderr=stderr, )
    except Exception as e:
        return CommandResult(success=False, message=str(e))


class SetModelRequest(BaseModel):
    model: str


@router.get("/api/machines/{machine_id}/llm/models")
async def list_ollama_models(machine_id: str):
    import subprocess
    import re

    try:
        output = subprocess.check_output(["ollama", "list"]).decode("utf-8")
        models = []
        for line in output.strip().splitlines()[1:]:
            parts = line.split()
            if parts:
                models.append({"name": parts[0]})
        current_model = None
        import os

        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        yaml_path = os.path.join(base_dir, "workstation", "llm_node", "config", "llm.yaml")
        try:
            with open(yaml_path, 'r') as f:
                lines = f.readlines()
            in_40_block = False
            for line in lines:
                if line.startswith('  40:'):
                    in_40_block = True
                elif line.startswith('  ') and not line.startswith('    ') and line.strip() and not line.strip().startswith('#') and in_40_block:
                    in_40_block = False
                elif not line.startswith(' ') and line.strip() and in_40_block:
                    in_40_block = False
                if in_40_block and 'llm_model_fast:' in line:
                    match = re.search(r"llm_model_fast:\s*['\"]([^'\"]+)['\"]", line)
                    if match:
                        current_model = match.group(1)
                        break
        except Exception:
            pass
        return {"models": models, "current_model": current_model}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


TTS_CONFIG_REL_PATH = "robot/ros_ws/src/tts_playback_node/config/tts_playback_node.yaml"
TTS_SOUNDS_SRC_REL = "robot/ros_ws/src/tts_playback_node"
TTS_SOUNDS_INSTALL_REL = "robot/ros_ws/install/tts_playback_node/share/tts_playback_node"
_TTS_SOUND_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$')
_KEYWORD_RE = re.compile(r'^[a-zA-ZäöüÄÖÜß0-9][a-zA-ZäöüÄÖÜß0-9 _-]{0,62}$')
_MP3_MAGIC = ((b'ID3',), (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2', b'\xff\xfa'),)


@router.get("/api/machines/{machine_id}/tts/config")
async def get_tts_config(machine_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    config = state.machines_config[machine_id]
    project_path = _machine_project_path(config)
    config_path = f"{project_path}/{TTS_CONFIG_REL_PATH}"
    cmd = f"cat {_shell_quote(config_path)}"
    try:
        exit_code, stdout, stderr = await ssh_manager.execute(machine_id, cmd)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if exit_code != 0:
        raise HTTPException(status_code=500, detail=stderr or "Failed to read TTS config")
    enable_feedback_sounds = False
    thinking_sound = "thinking-sound"
    sounds_volume = 70
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("enable_feedback_sounds:"):
            value = stripped.split(":", 1)[1].strip().lower()
            enable_feedback_sounds = value == "true"
        elif stripped.startswith("thinking_sound:"):
            thinking_sound = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("sounds_volume:"):
            try:
                sounds_volume = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                pass
    return {
        "enable_feedback_sounds": enable_feedback_sounds,
        "thinking_sound": thinking_sound,
        "sounds_volume": sounds_volume}


class TtsConfigRequest(BaseModel):
    enable_feedback_sounds: bool
    thinking_sound: str = "thinking-sound"
    sounds_volume: int = 70


@router.post("/api/machines/{machine_id}/tts/config", response_model=CommandResult)
async def set_tts_config(machine_id: str, req: TtsConfigRequest):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    if not _TTS_SOUND_NAME_RE.match(req.thinking_sound):
        raise HTTPException(status_code=400, detail="Invalid thinking_sound value")
    if not (0 <= req.sounds_volume <= 100):
        raise HTTPException(status_code=400, detail="sounds_volume must be 0–100")
    config = state.machines_config[machine_id]
    project_path = _machine_project_path(config)
    config_path = f"{project_path}/{TTS_CONFIG_REL_PATH}"
    feedback_val = "true" if req.enable_feedback_sounds else "false"
    cmd = (f"sed -i "
           f"'s/^enable_feedback_sounds: .*/enable_feedback_sounds: {feedback_val}/; "
           f"s/^thinking_sound: .*/thinking_sound: {req.thinking_sound}/; "
           f"s/^sounds_volume: .*/sounds_volume: {req.sounds_volume}/' "
           f"{_shell_quote(config_path)}")
    try:
        exit_code, stdout, stderr = await ssh_manager.execute(machine_id, cmd)
    except Exception as e:
        return CommandResult(success=False, message=str(e))
    if exit_code != 0:
        return CommandResult(success=False, message=stderr or "Failed to update TTS config")
    return CommandResult(success=True, message=f"TTS config updated")


@router.get("/api/machines/{machine_id}/tts/sounds")
async def list_tts_sounds(machine_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    config = state.machines_config[machine_id]
    project_path = _machine_project_path(config)
    src_dir = f"{project_path}/{TTS_SOUNDS_SRC_REL}"
    cmd = f"ls {_shell_quote(src_dir)}/*.mp3 2>/dev/null"
    try:
        exit_code, stdout, _ = await ssh_manager.execute(machine_id, cmd)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    stems = []
    for path in stdout.splitlines():
        fname = path.strip().split('/')[-1]
        if fname.endswith('.mp3') and fname != 'feedback-sound.mp3':
            stems.append(fname[:-4])
    return {"sounds": stems}


@router.post("/api/machines/{machine_id}/tts/sounds", response_model=CommandResult)
async def upload_tts_sound(machine_id: str, file: UploadFile = File(...)):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    fname = file.filename or ""
    if not fname.lower().endswith('.mp3'):
        raise HTTPException(status_code=400, detail="Only .mp3 files are allowed")
    stem = fname[:-4]
    if not _TTS_SOUND_NAME_RE.match(stem):
        raise HTTPException(status_code=400, detail="Invalid filename")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 10 MB)")
    if not (content[:3] == b'ID3' or (
            len(content) >= 2 and content[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2', b'\xff\xfa'))):
        raise HTTPException(status_code=400, detail="File does not appear to be a valid MP3")
    config = state.machines_config[machine_id]
    project_path = _machine_project_path(config)
    src_path = f"{project_path}/{TTS_SOUNDS_SRC_REL}/{fname}"
    install_path = f"{project_path}/{TTS_SOUNDS_INSTALL_REL}/{fname}"
    try:
        await ssh_manager.write_file(machine_id, content, src_path)
        _, _, _ = await ssh_manager.execute(machine_id, f"cp {_shell_quote(src_path)} {_shell_quote(install_path)} 2>/dev/null || true")
    except Exception as e:
        return CommandResult(success=False, message=str(e))
    return CommandResult(success=True, message=f"Uploaded {fname}")


LED_CONFIG_REL_PATH = "robot/ros_ws/src/led_control_node/config/led_control_node.yaml"


class LedConfigRequest(BaseModel):
    think_brightness: int = 10
    trace_brightness: int = 20
    spin_brightness: int = 20
    max_color_brightness: int = 20
    idle_r: float = 0.0
    idle_g: float = 0.0
    idle_b: float = 1.0
    idle_a: float = 0.1
    action_r: float = 1.0
    action_g: float = 0.0
    action_b: float = 0.0
    action_a: float = 1.0


@router.get("/api/machines/{machine_id}/led/config")
async def get_led_config(machine_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    config = state.machines_config[machine_id]
    project_path = _machine_project_path(config)
    config_path = f"{project_path}/{LED_CONFIG_REL_PATH}"
    script = ("import yaml,json,sys;"
              "d=yaml.safe_load(open(sys.argv[1]));"
              "hw=d.get('hardware',{});"
              "st=d.get('status',{});"
              "ic=st.get('idle',{}).get('color',{});"
              "ac=st.get('action',{}).get('color',{});"
              "print(json.dumps({'think_brightness':hw.get('think_brightness',10),'trace_brightness':hw.get('trace_brightness',20),'spin_brightness':hw.get('spin_brightness',20),'max_color_brightness':hw.get('max_color_brightness',20),'idle_r':ic.get('r',0.0),'idle_g':ic.get('g',0.0),'idle_b':ic.get('b',1.0),'idle_a':ic.get('a',0.1),'action_r':ac.get('r',1.0),'action_g':ac.get('g',0.0),'action_b':ac.get('b',0.0),'action_a':ac.get('a',1.0)}))")
    cmd = f"python3 -c {_shell_quote(script)} {_shell_quote(config_path)}"
    try:
        exit_code, stdout, stderr = await ssh_manager.execute(machine_id, cmd)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if exit_code != 0:
        raise HTTPException(status_code=500, detail=stderr or "Failed to read LED config")
    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse LED config: {e}")


@router.post("/api/machines/{machine_id}/led/config", response_model=CommandResult)
async def set_led_config(machine_id: str, req: LedConfigRequest):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    for val in (req.think_brightness, req.trace_brightness, req.spin_brightness, req.max_color_brightness):
        if not (0 <= val <= 255):
            raise HTTPException(status_code=400, detail="Brightness values must be 0–255")
    for val in (req.idle_r, req.idle_g, req.idle_b, req.idle_a, req.action_r, req.action_g, req.action_b, req.action_a):
        if not (0.0 <= val <= 1.0):
            raise HTTPException(status_code=400, detail="Color values must be 0.0–1.0")
    config = state.machines_config[machine_id]
    project_path = _machine_project_path(config)
    config_path = f"{project_path}/{LED_CONFIG_REL_PATH}"
    script = ("import yaml,sys;"
              "d=yaml.safe_load(open(sys.argv[1]));"
              "d['hardware']['think_brightness']=int(sys.argv[2]);"
              "d['hardware']['trace_brightness']=int(sys.argv[3]);"
              "d['hardware']['spin_brightness']=int(sys.argv[4]);"
              "d['hardware']['max_color_brightness']=int(sys.argv[5]);"
              "d['status']['idle']['color']['r']=float(sys.argv[6]);"
              "d['status']['idle']['color']['g']=float(sys.argv[7]);"
              "d['status']['idle']['color']['b']=float(sys.argv[8]);"
              "d['status']['idle']['color']['a']=float(sys.argv[9]);"
              "d['status']['action']['color']['r']=float(sys.argv[10]);"
              "d['status']['action']['color']['g']=float(sys.argv[11]);"
              "d['status']['action']['color']['b']=float(sys.argv[12]);"
              "d['status']['action']['color']['a']=float(sys.argv[13]);"
              "open(sys.argv[1],'w').write(yaml.dump(d,default_flow_style=False,allow_unicode=True));"
              "print('ok')")
    args = " ".join([_shell_quote(config_path),
                     str(req.think_brightness),
                     str(req.trace_brightness),
                     str(req.spin_brightness),
                     str(req.max_color_brightness),
                     str(round(req.idle_r, 6)),
                     str(round(req.idle_g, 6)),
                     str(round(req.idle_b, 6)),
                     str(round(req.idle_a, 6)),
                     str(round(req.action_r, 6)),
                     str(round(req.action_g, 6)),
                     str(round(req.action_b, 6)),
                     str(round(req.action_a, 6)), ])
    cmd = f"python3 -c {_shell_quote(script)} {args}"
    try:
        exit_code, stdout, stderr = await ssh_manager.execute(machine_id, cmd)
    except Exception as e:
        return CommandResult(success=False, message=str(e))
    if exit_code != 0:
        return CommandResult(success=False, message=stderr or "Failed to update LED config")
    return CommandResult(success=True, message="LED config updated")


VAD_CONFIG_REL_PATH = "workstation/ament_ws/src/voice_detection_node/config/voice_detection_node.yaml"


class VadConfigRequest(BaseModel):
    threshold: float = 0.5
    min_silence_duration_ms: int = 1300
    speech_pad_ms: int = 200
    activity_enabled: bool = True


@router.get("/api/machines/{machine_id}/vad/config")
async def get_vad_config(machine_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    config = state.machines_config[machine_id]
    project_path = _machine_project_path(config)
    config_path = f"{project_path}/{VAD_CONFIG_REL_PATH}"
    script = ("import yaml,json,sys;"
              "d=yaml.safe_load(open(sys.argv[1]));"
              "v=d.get('vad',{});"
              "a=d.get('activity',{});"
              "print(json.dumps({'threshold':v.get('threshold',0.5),'min_silence_duration_ms':v.get('min_silence_duration_ms',1300),'speech_pad_ms':v.get('speech_pad_ms',200),'activity_enabled':a.get('enabled',True)}))")
    cmd = f"python3 -c {_shell_quote(script)} {_shell_quote(config_path)}"
    try:
        exit_code, stdout, stderr = await ssh_manager.execute(machine_id, cmd)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if exit_code != 0:
        raise HTTPException(status_code=500, detail=stderr or "Failed to read VAD config")
    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse VAD config: {e}")


@router.post("/api/machines/{machine_id}/vad/config", response_model=CommandResult)
async def set_vad_config(machine_id: str, req: VadConfigRequest):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    if not (0.0 <= req.threshold <= 1.0):
        raise HTTPException(status_code=400, detail="threshold must be 0.0–1.0")
    if not (0 <= req.min_silence_duration_ms <= 10000):
        raise HTTPException(status_code=400, detail="min_silence_duration_ms must be 0–10000")
    if not (0 <= req.speech_pad_ms <= 5000):
        raise HTTPException(status_code=400, detail="speech_pad_ms must be 0–5000")
    config = state.machines_config[machine_id]
    project_path = _machine_project_path(config)
    config_path = f"{project_path}/{VAD_CONFIG_REL_PATH}"
    script = ("import yaml,sys;"
              "d=yaml.safe_load(open(sys.argv[1]));"
              "d['vad']['threshold']=float(sys.argv[2]);"
              "d['vad']['min_silence_duration_ms']=int(sys.argv[3]);"
              "d['vad']['speech_pad_ms']=int(sys.argv[4]);"
              "d.setdefault('activity',{})['enabled']=sys.argv[5].lower()=='true';"
              "open(sys.argv[1],'w').write(yaml.dump(d,default_flow_style=False,allow_unicode=True));"
              "print('ok')")
    args = " ".join([_shell_quote(config_path),
                     str(round(req.threshold, 4)),
                     str(req.min_silence_duration_ms),
                     str(req.speech_pad_ms),
                     str(req.activity_enabled).lower(), ])
    cmd = f"python3 -c {_shell_quote(script)} {args}"
    try:
        exit_code, stdout, stderr = await ssh_manager.execute(machine_id, cmd)
    except Exception as e:
        return CommandResult(success=False, message=str(e))
    if exit_code != 0:
        return CommandResult(success=False, message=stderr or "Failed to update VAD config")
    return CommandResult(success=True, message="VAD config updated")


TRANSCRIBER_CONFIG_REL_PATH = "workstation/ament_ws/src/transcriber_node/config/transcriber_node.yaml"


class TranscriberConfigRequest(BaseModel):
    beam_size: int = 5
    wake_words: list[str] = ["charlie"]
    stop_keywords: list[str] = ["stop", "stopp"]


@router.get("/api/machines/{machine_id}/transcriber/config")
async def get_transcriber_config(machine_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    config = state.machines_config[machine_id]
    project_path = _machine_project_path(config)
    config_path = f"{project_path}/{TRANSCRIBER_CONFIG_REL_PATH}"
    script = ("import yaml,json,sys;"
              "d=yaml.safe_load(open(sys.argv[1]));"
              "m=d.get('model',{});"
              "l=d.get('language',{});"
              "print(json.dumps({'beam_size':m.get('beam_size',5),'wake_words':l.get('wake_words',[]),'stop_keywords':l.get('stop_keywords',[])}))")
    cmd = f"python3 -c {_shell_quote(script)} {_shell_quote(config_path)}"
    try:
        exit_code, stdout, stderr = await ssh_manager.execute(machine_id, cmd)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if exit_code != 0:
        raise HTTPException(status_code=500, detail=stderr or "Failed to read transcriber config")
    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse transcriber config: {e}")


@router.post("/api/machines/{machine_id}/transcriber/config", response_model=CommandResult)
async def set_transcriber_config(machine_id: str, req: TranscriberConfigRequest):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    if not (1 <= req.beam_size <= 10):
        raise HTTPException(status_code=400, detail="beam_size must be 1–10")
    if not req.wake_words:
        raise HTTPException(status_code=400, detail="At least one wake word is required")
    for w in req.wake_words:
        if not _KEYWORD_RE.match(w):
            raise HTTPException(status_code=400, detail=f"Invalid wake word: {w!r}")
    for w in req.stop_keywords:
        if not _KEYWORD_RE.match(w):
            raise HTTPException(status_code=400, detail=f"Invalid stop keyword: {w!r}")
    config = state.machines_config[machine_id]
    project_path = _machine_project_path(config)
    config_path = f"{project_path}/{TRANSCRIBER_CONFIG_REL_PATH}"
    vals_json = json.dumps({
        "beam_size": req.beam_size,
        "wake_words": req.wake_words,
        "stop_keywords": req.stop_keywords, })
    script = ("import yaml,json,sys;"
              "d=yaml.safe_load(open(sys.argv[1]));"
              "v=json.loads(sys.argv[2]);"
              "d['model']['beam_size']=v['beam_size'];"
              "d['language']['wake_words']=v['wake_words'];"
              "d['language']['stop_keywords']=v['stop_keywords'];"
              "open(sys.argv[1],'w').write(yaml.dump(d,default_flow_style=False,allow_unicode=True));"
              "print('ok')")
    cmd = f"python3 -c {_shell_quote(script)} {_shell_quote(config_path)} {_shell_quote(vals_json)}"
    try:
        exit_code, stdout, stderr = await ssh_manager.execute(machine_id, cmd)
    except Exception as e:
        return CommandResult(success=False, message=str(e))
    if exit_code != 0:
        return CommandResult(success=False, message=stderr or "Failed to update transcriber config")
    return CommandResult(success=True, message="Transcriber config updated")


VOICE_IDENTITY_CONFIG_REL_PATH = "workstation/ament_ws/src/voice_identity_node/config/voice_identity_node.yaml"


class VoiceIdentityConfigRequest(BaseModel):
    threshold: float = 0.45


@router.get("/api/machines/{machine_id}/voice_identity/config")
async def get_voice_identity_config(machine_id: str):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    config = state.machines_config[machine_id]
    project_path = _machine_project_path(config)
    config_path = f"{project_path}/{VOICE_IDENTITY_CONFIG_REL_PATH}"
    script = ("import yaml,json,sys;"
              "d=yaml.safe_load(open(sys.argv[1]));"
              "m=d.get('matching',{});"
              "print(json.dumps({'threshold':m.get('threshold',0.45)}))")
    cmd = f"python3 -c {_shell_quote(script)} {_shell_quote(config_path)}"
    try:
        exit_code, stdout, stderr = await ssh_manager.execute(machine_id, cmd)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if exit_code != 0:
        raise HTTPException(status_code=500, detail=stderr or "Failed to read voice identity config")
    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse voice identity config: {e}")


@router.post("/api/machines/{machine_id}/voice_identity/config", response_model=CommandResult)
async def set_voice_identity_config(machine_id: str, req: VoiceIdentityConfigRequest):
    if machine_id not in state.machines_config:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    if not ssh_manager.is_connected(machine_id):
        raise HTTPException(status_code=503, detail=f"Not connected to {machine_id}")
    if not (0.0 <= req.threshold <= 1.0):
        raise HTTPException(status_code=400, detail="threshold must be 0.0–1.0")
    config = state.machines_config[machine_id]
    project_path = _machine_project_path(config)
    config_path = f"{project_path}/{VOICE_IDENTITY_CONFIG_REL_PATH}"
    script = ("import yaml,sys;"
              "d=yaml.safe_load(open(sys.argv[1]));"
              "d['matching']['threshold']=float(sys.argv[2]);"
              "open(sys.argv[1],'w').write(yaml.dump(d,default_flow_style=False,allow_unicode=True));"
              "print('ok')")
    cmd = f"python3 -c {_shell_quote(script)} {_shell_quote(config_path)} {str(round(req.threshold, 4))}"
    try:
        exit_code, stdout, stderr = await ssh_manager.execute(machine_id, cmd)
    except Exception as e:
        return CommandResult(success=False, message=str(e))
    if exit_code != 0:
        return CommandResult(success=False, message=stderr or "Failed to update voice identity config")
    return CommandResult(success=True, message="Voice identity config updated")


@router.post("/api/machines/{machine_id}/llm/models/select", response_model=CommandResult)
async def select_ollama_model(machine_id: str, req: SetModelRequest):
    import re
    import os

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    yaml_path = os.path.join(base_dir, "workstation", "llm_node", "config", "llm.yaml")
    try:
        with open(yaml_path, 'r') as f:
            lines = f.readlines()
        in_40_block = False
        for i, line in enumerate(lines):
            if line.startswith('  40:'):
                in_40_block = True
            elif line.startswith('  ') and not line.startswith('    ') and line.strip() and not line.strip().startswith('#') and in_40_block:
                in_40_block = False
            elif not line.startswith(' ') and line.strip() and in_40_block:
                in_40_block = False
            if in_40_block:
                if 'llm_model_fast:' in line:
                    lines[i] = re.sub(r"(llm_model_fast:\s*['\"])[^'\"]+(['\"])", rf"\g<1>{req.model}\g<2>", line)
                elif 'llm_model_smart:' in line:
                    lines[i] = re.sub(r"(llm_model_smart:\s*['\"])[^'\"]+(['\"])", rf"\g<1>{req.model}\g<2>", line)
        with open(yaml_path, 'w') as f:
            f.writelines(lines)
        return CommandResult(success=True, message=f"Successfully updated model to {req.model}")
    except Exception as e:
        return CommandResult(success=False, message=f"Failed to update model config: {str(e)}")

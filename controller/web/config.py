from __future__ import annotations

import json
from typing import Dict, List, Optional

from machine_private_config import load_effective_machines_data
from models import MachineConfig, NodeConfig, Settings
from network_config import detect_private_discovery_ip as detect_discovery_ip
from ssh_manager import ssh_manager
from web import state


def load_machines_config() -> Dict[str, MachineConfig]:
    data = load_effective_machines_data(state.CONFIG_DIR)
    return {machine_id: MachineConfig(**machine_data) for machine_id, machine_data in data.get("machines", {}).items()}


def load_nodes_config() -> Dict[str, List[NodeConfig]]:
    with open(state.CONFIG_DIR / "nodes.json") as f:
        data = json.load(f)
    return {machine_type: [NodeConfig(**node) for node in data.get(machine_type, {}).get("nodes", [])] for machine_type
            in ["robot", "workstation"]}


def load_settings() -> Settings:
    with open(state.CONFIG_DIR / "machines.json") as f:
        data = json.load(f)
    return Settings(**data.get("settings", {}))


def apply_runtime_settings() -> None:
    ssh_manager.configure_timeouts(connection_timeout=state.settings.connection_timeout, command_timeout=state.settings.command_timeout, )


def reload_configs() -> None:
    state.machines_config = load_machines_config()
    state.nodes_config = load_nodes_config()
    state.settings = load_settings()
    apply_runtime_settings()
    reload_discovery_manager()


def get_log_lines_default() -> int:
    return max(1, min(state.settings.log_lines_default, 10000))


def detect_private_discovery_ip() -> Optional[str]:
    machines = load_effective_machines_data(state.CONFIG_DIR)
    preferred_hosts = [str(machine.get('host', '')) for machine in machines.get('machines', {}).values() if
        machine.get('type') == 'workstation']
    return detect_discovery_ip(preferred_hosts)


def reload_discovery_manager() -> Optional[str]:
    advertise_host = detect_private_discovery_ip()
    state.discovery_manager.reload(advertise_host_override=advertise_host)
    return advertise_host

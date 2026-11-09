from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Dict

DISCOVERY_CONFIG_FILE = "dds_discovery_servers.json"


def load_discovery_config(config_dir: Path) -> Dict[str, Any]:
    config_path = config_dir / DISCOVERY_CONFIG_FILE
    if not config_path.exists():
        raise FileNotFoundError(f"Missing discovery config: {config_path}")
    raw = json.loads(config_path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"Discovery config must be a JSON object: {config_path}")
    shared_file = raw.get("shared_config_file")
    if not isinstance(shared_file, str) or not shared_file:
        raise ValueError(f"Discovery config must define shared_config_file: {config_path}")
    shared_path = config_dir / shared_file
    if not shared_path.exists():
        raise FileNotFoundError(f"Missing discovery shared config file: {shared_path}")
    server_file = raw.get("server_config_file")
    if server_file is not None:
        if not isinstance(server_file, str) or not server_file:
            raise ValueError(f"Discovery server_config_file must be a nonempty string: {config_path}")
        server_path = config_dir / server_file
        if not server_path.exists():
            raise FileNotFoundError(f"Missing discovery server config file: {server_path}")
    port = raw.get("port")
    if not isinstance(port, int) or port <= 0:
        raise ValueError(f"Discovery config must define a positive integer port: {config_path}")
    return raw


def shared_config_path(config_dir: Path, config: Dict[str, Any]) -> Path:
    return config_dir / str(config["shared_config_file"])


def server_config_path(config_dir: Path, config: Dict[str, Any]) -> Path:
    return config_dir / str(config.get("server_config_file") or config["shared_config_file"])


def discovery_port(config: Dict[str, Any]) -> int:
    return int(config["port"])


def discovery_endpoint(config: Dict[str, Any]) -> str:
    host = str(config["advertise_host"])
    return f"{host}:{discovery_port(config)}"


def discovery_listen_arguments(config: Dict[str, Any], port: int) -> str:
    bind_host = str(config.get("bind_host", "0.0.0.0"))
    if bind_host in ("", "0.0.0.0", "::"):
        # The whitelisted UDP transport cannot bind the wildcard locator.
        # Listen on the selected robot LAN and localhost, rather than unrelated
        # interfaces, and avoid duplicate localhost arguments in local setups.
        hosts = [str(config["advertise_host"]), "127.0.0.1"]
    else:
        hosts = [bind_host]
    arguments = []
    for host in dict.fromkeys(hosts):
        arguments.extend(["-l", host, "-p", str(port)])
    return shlex.join(arguments)


def format_with_domain(value: str, *, domain_id: int, port: int, bind_host: str, endpoint: str, config_file: str, listen_args: str = "", ) -> str:
    return value.format(domain_id=domain_id, port=port, bind_host=bind_host, endpoint=endpoint, config_file=config_file, listen_args=listen_args, )


def build_service_env(config: Dict[str, Any], *, domain_id: int, project_path: str) -> Dict[str, str]:
    endpoint = discovery_endpoint(config)
    port = discovery_port(config)
    bind_host = str(config["bind_host"])
    config_file_name = str(config["shared_config_file"])
    config_file = f"{project_path}/controller/config/{config_file_name}"
    env: Dict[str, str] = {}
    for key, value in (config.get("service_env") or {}).items():
        if not isinstance(value, str):
            continue
        env[
            key] = format_with_domain(value, domain_id=domain_id, port=port, bind_host=bind_host, endpoint=endpoint, config_file=config_file, )
    return env

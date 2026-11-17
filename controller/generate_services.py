from __future__ import annotations

import hashlib
import json
import argparse
import getpass
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from string import Template
from typing import Any, Dict, List

from discovery_config import build_service_env, load_discovery_config
from machine_private_config import load_effective_machines_data
from network_config import detect_private_discovery_ip

_SOURCE_PATH = Path(__file__).resolve()
_IMPORTED_SOURCE_FINGERPRINT = hashlib.sha256(_SOURCE_PATH.read_bytes()).hexdigest()
SERVICE_TEMPLATE = Template("""[Unit]
Description=$description
After=network.target
$dependencies
$start_limit
[Service]
Type=simple
$extra_env
$working_dir_line
$exec_start_pre_lines
ExecStart=$run_script
$exec_stop_post_lines
Restart=$restart
RestartSec=$restart_sec
TimeoutStartSec=$start_timeout
TimeoutStopSec=3
KillMode=mixed
SuccessExitStatus=137
StandardOutput=journal
StandardError=journal
""")


def ensure_loaded_generator_is_current() -> None:
    current_fingerprint = hashlib.sha256(_SOURCE_PATH.read_bytes()).hexdigest()
    if current_fingerprint != _IMPORTED_SOURCE_FINGERPRINT:
        raise RuntimeError("Controller code changed after the server started. Restart the controller "
                           "before generating or deploying systemd services.")


def remove_stale_generated_services(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    for service_file in output_dir.glob("*.service"):
        service_file.unlink()


def load_config(config_dir: Path) -> tuple[Dict[str, Any], Dict[str, Any]]:
    machines_config = load_effective_machines_data(config_dir)
    with open(config_dir / "nodes.json") as f:
        nodes_config = json.load(f)
    return machines_config, nodes_config


def generate_dependency_string(dependencies: List[str], service_prefix: str = "ros2-") -> str:
    if not dependencies:
        return ""
    service_names = [f"{service_prefix}{dep.replace('_', '-')}.service" for dep in dependencies]
    wants = f"Wants={' '.join(service_names)}"
    after = f"After={' '.join(service_names)}"
    return f"{wants}\n{after}"


def expand_variables(value: str, project_path: str, venv_path: str = "") -> str:
    if not value:
        return value
    result = value.replace("$PROJECT_PATH", project_path)
    result = result.replace("$VIRTUAL_ENV", venv_path)
    return result


def generate_service_file(node: Dict[
    str, Any], machine_type: str, ros_domain_id: int, project_path: str, user: str, venv_path: str = "", discovery_env:
Dict[str, str] | None = None, fleet_id: str = "stretch", home_dir: str | None = None, ) -> str:
    home_dir = home_dir or f"/home/{user}"
    scripts_dir = f"{project_path}/scripts"
    expanded_venv = expand_variables(venv_path, project_path) if venv_path else ""
    description = node.get("description", f"ROS2 {node['name']} node")
    command = expand_variables(node.get("command", ""), project_path, expanded_venv)
    working_dir = expand_variables(node.get("working_dir", ""), project_path, expanded_venv)
    dependencies = node.get("dependencies", [])
    requires_build = node.get("requires_build", False)
    requires_venv = node.get("requires_venv", False)
    packages_to_build = node.get("packages_to_build", [])
    node_extra_env = node.get("extra_env", {})
    if machine_type == "robot":
        update_script = f"{scripts_dir}/update_robot_ws.sh"
    else:
        update_script = f"{scripts_dir}/update_workstation_ws.sh"
    dep_string = generate_dependency_string(dependencies)
    extra_env_lines = ['Environment="PYTHONUNBUFFERED=1"']
    if machine_type == "robot":
        extra_env_lines.append(f'Environment="HELLO_FLEET_PATH={home_dir}/stretch_user"')
        extra_env_lines.append(f'Environment="HELLO_FLEET_ID={fleet_id}"')
        extra_env_lines.append(f'Environment="LRS_LOG_LEVEL=None"')
    for env_key, env_val in node_extra_env.items():
        expanded_val = expand_variables(env_val, project_path, expanded_venv)
        extra_env_lines.append(f'Environment="{env_key}={expanded_val}"')
    if discovery_env:
        for env_key, env_val in discovery_env.items():
            extra_env_lines.append(f'Environment="{env_key}={env_val}"')
    extra_env = "\n".join(extra_env_lines)
    working_dir_line = f"WorkingDirectory={working_dir}" if working_dir else ""
    venv_flag = ""
    if requires_venv and expanded_venv:
        venv_flag = f" --venv {expanded_venv}"
    exec_start_pre_lines = []
    start_timeout = 60
    restart = str(node.get("restart", "no"))
    restart_sec = int(node.get("restart_sec", 3))
    start_limit = ""
    if restart != "no":
        start_limit_interval = int(node.get("start_limit_interval_sec", 60))
        start_limit_burst = int(node.get("start_limit_burst", 3))
        start_limit = (f"StartLimitIntervalSec={start_limit_interval}\n"
                       f"StartLimitBurst={start_limit_burst}")
    if requires_build and packages_to_build:
        pkg_flag = f" --packages {','.join(packages_to_build)}"
        exec_start_pre_lines.append(f"ExecStartPre={update_script} --build-only{venv_flag}{pkg_flag}")
        start_timeout = 300
    exec_start_pre_lines.extend(f"ExecStartPre={command_line}" for command_line in node.get("exec_start_pre", []))
    exec_stop_post_lines = "\n".join(f"ExecStopPost={command_line}" for command_line in node.get("exec_stop_post", []))
    working_dir_flag = ""
    if working_dir:
        working_dir_flag = f" --working-dir {working_dir}"
    run_script = (f"{update_script} --run"
                  f" --ros-domain-id {ros_domain_id}"
                  f"{venv_flag}"
                  f"{working_dir_flag}"
                  f' --command "{command}"')
    content = SERVICE_TEMPLATE.substitute(description=description, dependencies=dep_string, start_limit=start_limit, extra_env=extra_env, working_dir_line=working_dir_line, exec_start_pre_lines="\n".join(exec_start_pre_lines), run_script=run_script, exec_stop_post_lines=exec_stop_post_lines, restart=restart, restart_sec=restart_sec, start_timeout=start_timeout, )
    lines = content.split('\n')
    cleaned = []
    for line in lines:
        if line.strip() == '' and cleaned and cleaned[-1].strip() == '':
            continue
        cleaned.append(line)
    return '\n'.join(cleaned)


def generate_all_services(config_dir: Path, output_dir: Path, advertise_host_override: str | None = None, ) -> Dict[
    str, List[Dict[str, str]]]:
    machines_config, nodes_config = load_config(config_dir)
    discovery_config = load_discovery_config(config_dir)
    preferred_hosts = [str(machine.get('host', '')) for machine in machines_config.get('machines', {}).values() if
        machine.get('type') == 'workstation']
    if advertise_host_override is None:
        advertise_host_override = (
                detect_private_discovery_ip(preferred_hosts) or next((host for host in preferred_hosts if host), None))
    if advertise_host_override:
        discovery_config["advertise_host"] = advertise_host_override
    output_dir.mkdir(parents=True, exist_ok=True)
    result: Dict[str, List[Dict[str, str]]] = {}
    for machine_id, machine in machines_config.get("machines", {}).items():
        machine_type = machine["type"]
        ros_domain_id = machine.get("ros_domain_id", 0)
        project_path = machine.get("project_path", "~/Charlie-LLM-service-robot")
        project_path = project_path.replace("~", "/home/" + machine["user"])
        machine_nodes = nodes_config.get(machine_type, {}).get("nodes", [])
        machine_output_dir = output_dir / machine_id
        machine_output_dir.mkdir(parents=True, exist_ok=True)
        remove_stale_generated_services(machine_output_dir)
        result[machine_id] = []
        for node in machine_nodes:
            service_name = node["service_name"]
            discovery_env = build_service_env(discovery_config, domain_id=ros_domain_id, project_path=project_path, )
            service_content = generate_service_file(node=node, machine_type=machine_type, ros_domain_id=ros_domain_id, project_path=project_path, user=
            machine["user"], venv_path=node.get("venv_path", ""), discovery_env=discovery_env,
            fleet_id=machine_id if re.fullmatch(r"stretch-se3-[0-9]{4}", machine_id) else "stretch", )
            filename = f"{service_name}.service"
            service_file = machine_output_dir / filename
            service_file.write_text(service_content)
            result[machine_id].append({
                "filename": filename,
                "service_name": service_name,
                "path": str(service_file),
                "content": service_content, })
            print(f"Generated: {service_file}")
    install_script = output_dir / "install_services.sh"
    install_script.write_text("""#!/bin/bash
SERVICE_DIR="$HOME/.config/systemd/user"
mkdir -p "$SERVICE_DIR"
for service_file in *.service; do
    if [[ -f "$service_file" ]]; then
        cp "$service_file" "$SERVICE_DIR/"
        echo "Installed: $service_file"
    fi
done
systemctl --user daemon-reload
echo ""
echo "Services installed. You can now:"
echo "  systemctl --user start <service>"
echo "  systemctl --user stop <service>"
echo "  systemctl --user status <service>"
echo "  journalctl --user -u <service> -f"
echo ""
echo "NOTE: Services are NOT enabled to auto-start."
echo "They must be started manually via the webui or systemctl start."
""")
    install_script.chmod(0o755)
    print(f"Generated: {install_script}")
    return result


def local_service_definitions(project_root: Path, machine_type: str, *, ros_domain_id: int | None = None,
                              fleet_id: str | None = None, discovery_host: str | None = None) -> List[Dict[str, str]]:
    """Use this checkout directly, without requiring controller SSH credentials."""
    project_root = project_root.resolve()
    config_dir = project_root / "controller/config"
    machines = json.loads((config_dir / "machines.json").read_text())["machines"]
    if machine_type == "robot":
        if fleet_id is None:
            system_config = Path("/etc/hello-robot/hello-robot.conf")
            match = re.search(r"^\s*(?:export\s+)?HELLO_FLEET_ID=['\"]?(stretch-se3-[0-9]{4})['\"]?\s*$",
                              system_config.read_text(), re.MULTILINE) if system_config.exists() else None
            fleet_id = match.group(1) if match else os.environ.get("HELLO_FLEET_ID")
        if not fleet_id or not re.fullmatch(r"stretch-se3-[0-9]{4}", fleet_id):
            raise ValueError("Specify this robot's --fleet-id stretch-se3-XXXX")
        matching = machines.get(fleet_id, {})
    else:
        local_ip = detect_private_discovery_ip(m.get("host", "") for m in machines.values()
                                               if m["type"] == "workstation")
        matching = next((m for m in machines.values() if m["type"] == "workstation"
                         and m.get("host") == local_ip), {})
    domain = ros_domain_id if ros_domain_id is not None else matching.get("ros_domain_id", 42)
    discovery = load_discovery_config(config_dir)
    discovery_host = discovery_host or next((m["host"] for m in machines.values()
        if m["type"] == "workstation" and m.get("ros_domain_id", 42) == domain), None)
    if not discovery_host:
        raise ValueError("Specify --discovery-host with the controller's robot-network IP")
    discovery["advertise_host"] = discovery_host
    nodes = json.loads((config_dir / "nodes.json").read_text())[machine_type]["nodes"]
    services = []
    for node in nodes:
        name = node["service_name"]
        if not re.fullmatch(r"ros2-[A-Za-z0-9_.@-]+", name):
            raise ValueError(f"Invalid service name: {name}")
        content = generate_service_file(node, machine_type, domain, str(project_root), getpass.getuser(),
            node.get("venv_path", ""), build_service_env(discovery, domain_id=domain, project_path=str(project_root)),
            fleet_id=fleet_id or "stretch", home_dir=str(Path.home()))
        services.append({"filename": f"{name}.service", "service_name": name, "content": content})
    return services


def install_local_services(project_root: Path, machine_type: str, services: List[Dict[str, str]]) -> Path:
    """Replace project user units, stopping old processes; never start or enable nodes."""
    project_root = project_root.resolve()
    workspace = project_root / ("robot/ros_ws" if machine_type == "robot" else "workstation/ament_ws")
    venv = project_root / ("robot/.venv" if machine_type == "robot" else "workstation/.venv")
    if not (workspace / "install/local_setup.bash").is_file() or not (venv / "bin/python").exists():
        raise RuntimeError("Build this checkout's ROS workspace and Python environment before installing services")
    service_dir = Path.home() / ".config/systemd/user"
    installed = sorted(service_dir.glob("ros2-*.service"))
    desired = {service["filename"] for service in services}
    if not desired or len(desired) != len(services) or any(
            not re.fullmatch(r"ros2-[A-Za-z0-9_.@-]+\.service", name) for name in desired):
        raise ValueError("Refusing an empty or invalid service set")
    for path in installed:
        if (service_dir / f"{path.name}.d").exists():
            raise RuntimeError(f"Review service overrides before migrating: {path.name}.d")
    # Verify the entire replacement before stopping anything or replacing files.
    with tempfile.TemporaryDirectory(prefix="charlie-services-") as staging:
        paths = []
        for service in services:
            path = Path(staging) / service["filename"]
            path.write_text(service["content"])
            paths.append(str(path))
        subprocess.run(["systemd-analyze", "--user", "verify", *paths], check=True)
    backup_parent = Path.home() / ".local/state/Charlie-LLM-service-robot/service-backups"
    backup_parent.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="units-", dir=backup_parent))
    for path in installed:
        shutil.copy2(path, backup / path.name)
    if installed:
        subprocess.run(["systemctl", "--user", "stop", *(p.name for p in installed)], check=True)
    obsolete = [p for p in installed if p.name not in desired]
    if obsolete:
        subprocess.run(["systemctl", "--user", "disable", *(p.name for p in obsolete)], check=True)
        for path in obsolete:
            path.unlink()
    service_dir.mkdir(parents=True, exist_ok=True)
    for service in services:
        path = service_dir / service["filename"]
        temporary = path.with_suffix(".service.tmp")
        temporary.write_text(service["content"])
        temporary.chmod(0o644)
        temporary.replace(path)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print(f"Installed {len(services)} services from {project_root}. Services remain stopped. Backup: {backup}")
    return backup


def main():
    parser = argparse.ArgumentParser(description="Generate or install this checkout's ROS user services")
    parser.add_argument("--install-local", choices=("robot", "workstation"),
                        help="Replace local ROS services; stop existing nodes without starting new ones")
    parser.add_argument("--ros-domain-id", type=int)
    parser.add_argument("--fleet-id")
    parser.add_argument("--discovery-host")
    parser.add_argument("--dry-run", action="store_true", help="Print local unit definitions without installing")
    args = parser.parse_args()
    if args.install_local:
        services = local_service_definitions(_SOURCE_PATH.parent.parent, args.install_local,
            ros_domain_id=args.ros_domain_id, fleet_id=args.fleet_id, discovery_host=args.discovery_host)
        if args.dry_run:
            for service in services:
                print(f"# {service['filename']}\n{service['content']}")
        else:
            if os.geteuid() == 0:
                parser.error("Run as the normal ROS user, not root")
            install_local_services(_SOURCE_PATH.parent.parent, args.install_local, services)
        return
    script_dir = Path(__file__).parent
    config_dir = script_dir / "config"
    output_dir = script_dir / "generated_services"
    print("Generating systemd service files...")
    print(f"Config directory: {config_dir}")
    print(f"Output directory: {output_dir}")
    print()
    result = generate_all_services(config_dir, output_dir)
    total = sum(len(services) for services in result.values())
    print()
    print(f"Generated {total} service files for {len(result)} machines.")


if __name__ == "__main__":
    main()

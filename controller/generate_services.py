from __future__ import annotations

import hashlib
import json
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
Dict[str, str] | None = None, ) -> str:
    home_dir = f"/home/{user}"
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
        extra_env_lines.append(f'Environment="HELLO_FLEET_ID=stretch"')
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
        project_path = machine.get("project_path", "~/pg-llm-service-robot")
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
            machine["user"], venv_path=node.get("venv_path", ""), discovery_env=discovery_env, )
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


def main():
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

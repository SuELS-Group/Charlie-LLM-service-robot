from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_SSH_KEY_PATH = "ssh_keys/controller_ed25519"
PRIVATE_MACHINES_FILE = "machines.private.json"


def _read_json(path: Path) -> Dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _private_config_path(config_dir: Path) -> Path:
    return config_dir / PRIVATE_MACHINES_FILE


def _normalize_hidden_machines(raw_hidden: Any, known_machine_ids: List[str]) -> List[str]:
    if not isinstance(raw_hidden, list):
        return []
    known = set(known_machine_ids)
    seen = set()
    result: List[str] = []
    for machine_id in raw_hidden:
        if isinstance(machine_id, str) and machine_id in known and machine_id not in seen:
            seen.add(machine_id)
            result.append(machine_id)
    return result


def ensure_private_machine_config(config_dir: Path) -> Dict[str, Any]:
    machines_data = _read_json(config_dir / "machines.json")
    private_path = _private_config_path(config_dir)
    private_data: Dict[str, Any] = {}
    if private_path.exists():
        private_data = _read_json(private_path)
    existing_machines = private_data.get("machines", {}) if isinstance(private_data, dict) else {}
    machine_ids = list(machines_data.get("machines", {}).keys())
    hidden_machines = _normalize_hidden_machines(private_data.get("hidden_machines", []), machine_ids)
    merged: Dict[str, Dict[str, Any]] = {}
    changed = not private_path.exists()
    for machine_id, machine in machines_data.get("machines", {}).items():
        raw_override = existing_machines.get(machine_id, {})
        machine_override = raw_override if isinstance(raw_override, dict) else {}
        user = machine_override.get("user") if machine_override.get("user") else machine.get("user")
        ssh_key_path = machine_override.get("ssh_key_path") or machine.get("ssh_key_path") or DEFAULT_SSH_KEY_PATH
        merged[machine_id] = {"user": user, "ssh_key_path": ssh_key_path}
        existing_user = machine_override.get("user")
        existing_key = machine_override.get("ssh_key_path")
        if existing_user != user or existing_key != ssh_key_path:
            changed = True
    if set(existing_machines.keys()) != set(merged.keys()):
        changed = True
    if private_data.get("hidden_machines") != hidden_machines:
        changed = True
    normalized = {"machines": merged, "hidden_machines": hidden_machines}
    if changed:
        _write_json(private_path, normalized)
    return normalized


def load_effective_machines_data(config_dir: Path) -> Dict[str, Any]:
    machines_data = _read_json(config_dir / "machines.json")
    private_data = ensure_private_machine_config(config_dir)
    overrides = private_data.get("machines", {})
    hidden = set(private_data.get("hidden_machines", []))
    effective = {}
    for machine_id, machine in machines_data.get("machines", {}).items():
        if machine_id in hidden:
            continue
        raw_override = overrides.get(machine_id, {})
        machine_override = raw_override if isinstance(raw_override, dict) else {}
        effective_user = machine_override.get("user") if machine_override.get("user") else machine.get("user")
        if effective_user is not None:
            machine["user"] = effective_user
        machine["ssh_key_path"] = (
                machine_override.get("ssh_key_path") or machine.get("ssh_key_path") or DEFAULT_SSH_KEY_PATH)
        effective[machine_id] = machine
    machines_data["machines"] = effective
    return machines_data


def update_private_key_path(config_dir: Path, key_path: str) -> None:
    private_data = ensure_private_machine_config(config_dir)
    overrides = private_data.get("machines", {})
    changed = False
    for machine in overrides.values():
        if machine.get("ssh_key_path") != key_path:
            machine["ssh_key_path"] = key_path
            changed = True
    if changed:
        _write_json(_private_config_path(config_dir), {
            "machines": overrides,
            "hidden_machines": private_data.get("hidden_machines", []), })


def update_private_machine_user(config_dir: Path, machine_id: str, user: str) -> None:
    private_data = ensure_private_machine_config(config_dir)
    overrides = private_data.get("machines", {})
    if machine_id not in overrides:
        return
    if overrides[machine_id].get("user") == user:
        return
    overrides[machine_id]["user"] = user
    _write_json(_private_config_path(config_dir), {
        "machines": overrides,
        "hidden_machines": private_data.get("hidden_machines", []), })


def hide_machine_in_private_config(config_dir: Path, machine_id: str) -> bool:
    private_data = ensure_private_machine_config(config_dir)
    overrides = private_data.get("machines", {})
    hidden = list(private_data.get("hidden_machines", []))
    if machine_id not in overrides:
        return False
    if machine_id in hidden:
        return True
    hidden.append(machine_id)
    _write_json(_private_config_path(config_dir), {"machines": overrides, "hidden_machines": hidden, })
    return True

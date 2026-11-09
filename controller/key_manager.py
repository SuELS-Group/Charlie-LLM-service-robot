from __future__ import annotations

import asyncio
import getpass
import logging
import os
import socket
from contextlib import suppress
from pathlib import Path
from typing import Dict, Optional, Tuple

import asyncssh

from machine_private_config import (DEFAULT_SSH_KEY_PATH, hide_machine_in_private_config, update_private_key_path, update_private_machine_user, )
from models import MachineConfig

logger = logging.getLogger(__name__)
KEYS_DIR = Path(__file__).parent / "ssh_keys"
KEY_NAME = "controller_ed25519"
CONFIG_DIR = Path(__file__).parent / "config"


def _key_tag() -> str:
    username = os.environ.get("USER", getpass.getuser())
    hostname = socket.gethostname()
    return f"ros2-controller:{username}@{hostname}"


def get_key_path() -> Path:
    return KEYS_DIR / KEY_NAME


def get_pubkey_path() -> Path:
    return KEYS_DIR / f"{KEY_NAME}.pub"


def keys_exist() -> bool:
    return get_key_path().exists() and get_pubkey_path().exists()


def generate_keypair() -> Tuple[Path, Path]:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    key_path = get_key_path()
    pub_path = get_pubkey_path()
    if key_path.exists():
        key_path.unlink()
    if pub_path.exists():
        pub_path.unlink()
    tag = _key_tag()
    private_key = asyncssh.generate_private_key("ssh-ed25519", comment=tag)
    key_path.write_bytes(private_key.export_private_key())
    key_path.chmod(0o600)
    pub_key_text = private_key.export_public_key().decode().strip()
    if not pub_key_text.endswith(tag):
        pub_key_text = f"{pub_key_text} {tag}"
    pub_path.write_text(pub_key_text + "\n")
    pub_path.chmod(0o644)
    logger.info("Generated new SSH keypair at %s (tag: %s)", key_path, tag)
    return key_path, pub_path


def is_local_machine(host: str) -> bool:
    try:
        local_hostname = socket.gethostname()
        local_ips = set()
        for info in socket.getaddrinfo(local_hostname, None):
            local_ips.add(info[4][0])
        local_ips.add("127.0.0.1")
        local_ips.add("::1")
        with suppress(socket.gaierror):
            resolved = socket.getaddrinfo(host, None)
            for info in resolved:
                if info[4][0] in local_ips:
                    return True
        if host == local_hostname:
            return True
        if host in local_ips:
            return True
        if host == "localhost":
            return True
    except Exception as exc:
        logger.debug("Unable to determine whether %s is local: %s", host, exc)
    return False


def _remove_tagged_keys(authorized_keys_text: str, tag: str) -> str:
    lines = authorized_keys_text.splitlines()
    kept = [line for line in lines if tag not in line]
    return "\n".join(kept) + "\n" if kept else ""


async def deploy_key_to_host(host: str, port: int, username: str, password: str, public_key_text: str, ) -> Tuple[
    bool, str]:
    tag = _key_tag()
    try:
        conn = await asyncio.wait_for(asyncssh.connect(host=host, port=port, username=username, password=password, known_hosts=None, ), timeout=15, )
        async with conn:
            await conn.run("mkdir -p ~/.ssh && chmod 700 ~/.ssh", check=True)
            result = await conn.run("cat ~/.ssh/authorized_keys 2>/dev/null || true")
            existing = result.stdout or ""
            cleaned = _remove_tagged_keys(existing, tag)
            pub_key_line = public_key_text.strip()
            new_content = cleaned.rstrip("\n") + "\n" + pub_key_line + "\n" if cleaned.strip() else pub_key_line + "\n"
            escaped = new_content.replace("'", "'\\''")
            await conn.run(f"printf '%s' '{escaped}' > ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys", check=True, )
            logger.info("Deployed public key to %s@%s (replaced tag %s)", username, host, tag)
        return True, ""
    except asyncio.TimeoutError:
        msg = f"Timeout connecting to {username}@{host}:{port}"
        logger.error(msg)
        return False, msg
    except asyncssh.PermissionDenied:
        msg = f"Authentication failed for {username}@{host} (wrong password)"
        logger.error(msg)
        return False, msg
    except asyncssh.DisconnectError as e:
        msg = f"Connection rejected by {host}: {e}"
        logger.error(msg)
        return False, msg
    except Exception as e:
        msg = f"Failed to deploy key to {username}@{host}: {e}"
        logger.error(msg)
        return False, msg


def deploy_key_locally(public_key_text: str) -> bool:
    tag = _key_tag()
    try:
        ssh_dir = Path.home() / ".ssh"
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        auth_keys = ssh_dir / "authorized_keys"
        existing = auth_keys.read_text() if auth_keys.exists() else ""
        cleaned = _remove_tagged_keys(existing, tag)
        pub_key_line = public_key_text.strip()
        new_content = cleaned.rstrip("\n") + "\n" + pub_key_line + "\n" if cleaned.strip() else pub_key_line + "\n"
        auth_keys.write_text(new_content)
        auth_keys.chmod(0o600)
        logger.info("Deployed public key locally (replaced tag %s)", tag)
        return True
    except Exception as e:
        logger.error("Failed to deploy key locally: %s", e)
        return False


def update_machines_config_key_path():
    rel_key_path = DEFAULT_SSH_KEY_PATH
    update_private_key_path(CONFIG_DIR, rel_key_path)
    logger.info("Updated private machine config with managed key path")


def _resolve_machine_user(mid: str, cfg: MachineConfig) -> Optional[str]:
    if cfg.user:
        return cfg.user
    user = input(f"SSH username for {mid} ({cfg.host}) [Enter to hide/skip]: ").strip()
    if not user:
        hide_machine_in_private_config(CONFIG_DIR, mid)
        return None
    cfg.user = user
    update_private_machine_user(CONFIG_DIR, mid, user)
    return user


async def setup_ssh_keys_interactive(machines: Dict[str, MachineConfig]) -> bool:
    print()
    print("=" * 60)
    print("  SSH Key Setup")
    print("=" * 60)
    print()
    print("No SSH keys found. This will generate a new keypair and")
    print("deploy it to all configured machines.")
    print()
    hidden_machines = []
    for mid, cfg in machines.items():
        resolved_user = _resolve_machine_user(mid, cfg)
        if not resolved_user:
            hidden_machines.append(mid)
    remote_machines = {}
    local_machines = {}
    for mid, cfg in machines.items():
        if mid in hidden_machines:
            continue
        if is_local_machine(cfg.host):
            local_machines[mid] = cfg
        else:
            remote_machines[mid] = cfg
    if local_machines:
        print("Local (this machine):")
        for mid, cfg in local_machines.items():
            print(f"  - {mid}: {cfg.name} ({cfg.user}@{cfg.host})")
        print()
    if remote_machines:
        print("Remote machines requiring credentials:")
        for mid, cfg in remote_machines.items():
            print(f"  - {mid}: {cfg.name} ({cfg.user}@{cfg.host})")
        print()
    proceed = input("Generate keypair and configure SSH access? [Y/n] ").strip().lower()
    if proceed not in ("", "y", "yes"):
        print("Aborted. Cannot continue without SSH keys.")
        raise SystemExit(1)
    key_path, pub_path = generate_keypair()
    public_key_text = pub_path.read_text()
    print(f"\nKeypair generated: {key_path}")
    print(f"Key tag: {_key_tag()}")
    for mid, cfg in local_machines.items():
        print(f"\nDeploying key locally for {mid} ({cfg.user}@{cfg.host})...")
        ok = deploy_key_locally(public_key_text)
        if ok:
            print("  ✓ Key deployed locally")
        else:
            print("  ✗ Failed to deploy key locally")
    max_attempts = 3
    for mid, cfg in remote_machines.items():
        print(f"\n--- {mid}: {cfg.name} ({cfg.user}@{cfg.host}) ---")
        for attempt in range(1, max_attempts + 1):
            password = getpass.getpass(f"  Password for {cfg.user}@{cfg.host}: ")
            if not password:
                print("  Empty password. Try again.")
                continue
            print(f"  Deploying key (attempt {attempt}/{max_attempts})...")
            ok, err = await deploy_key_to_host(host=cfg.host, port=cfg.port, username=cfg.user, password=password, public_key_text=public_key_text, )
            if ok:
                print(f"  ✓ Key deployed to {cfg.user}@{cfg.host}")
                break
            print(f"  ✗ {err}")
            if attempt < max_attempts:
                print(f"  Try again ({max_attempts - attempt} attempts remaining)...")
        else:
            hide_machine_in_private_config(CONFIG_DIR, mid)
            if mid not in hidden_machines:
                hidden_machines.append(mid)
            print(f"  Machine {mid} hidden after {max_attempts} failed attempts.")
    update_machines_config_key_path()
    if hidden_machines:
        print("\nHidden machines:")
        for mid in hidden_machines:
            print(f"  - {mid}")
    print("\n✓ SSH key setup complete.\n")
    return True


async def verify_key_connectivity(machines: Dict[str, MachineConfig]) -> Dict[str, bool]:
    key_path = get_key_path()
    if not key_path.exists():
        return {mid: False for mid in machines}
    results = {}
    for mid, cfg in machines.items():
        if not cfg.user:
            results[mid] = False
            continue
        try:
            conn = await asyncio.wait_for(asyncssh.connect(host=cfg.host, port=cfg.port, username=cfg.user, client_keys=[
                str(key_path)], known_hosts=None, ), timeout=10, )
            async with conn:
                results[mid] = True
        except Exception:
            results[mid] = False
    return results


async def setup_keys_from_api(machines: Dict[str, MachineConfig], credentials: Dict[str, str], users: Optional[
    Dict[str, str]] = None, ) -> Dict[str, Tuple[bool, str]]:
    if users:
        for mid, user in users.items():
            normalized = user.strip()
            if not normalized or mid not in machines:
                continue
            update_private_machine_user(CONFIG_DIR, mid, normalized)
            machines[mid].user = normalized
    generate_keypair()
    public_key_text = get_pubkey_path().read_text()
    results = {}
    for mid, cfg in machines.items():
        if not cfg.user:
            results[mid] = (False, "No SSH user configured")
            continue
        if is_local_machine(cfg.host):
            ok = deploy_key_locally(public_key_text)
            results[mid] = (ok, "" if ok else "Failed to deploy locally")
            continue
        password = credentials.get(mid, "")
        if not password:
            results[mid] = (False, "No password provided")
            continue
        ok, err = await deploy_key_to_host(host=cfg.host, port=cfg.port, username=cfg.user, password=password, public_key_text=public_key_text, )
        results[mid] = (ok, err)
    update_machines_config_key_path()
    return results

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import suppress
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import asyncssh

from models import MachineConfig, ServiceStatus

logger = logging.getLogger(__name__)
CONTROLLER_DIR = Path(__file__).parent
KEYS_DIR = CONTROLLER_DIR / "ssh_keys"


class _ConnectionEvents(asyncssh.SSHClient):
    """Record transport failures, including losses between status requests."""

    def __init__(self, manager: SSHConnectionManager, machine_id: str):
        self._manager = manager
        self._machine_id = machine_id
        self._connection: Optional[asyncssh.SSHClientConnection] = None

    def connection_made(self, conn: asyncssh.SSHClientConnection) -> None:
        self._connection = conn

    def connection_lost(self, exc: Optional[Exception]) -> None:
        # Intentional shutdowns remove the connection before closing it. An old
        # callback must also leave a newer replacement connection untouched.
        if self._manager._connections.get(self._machine_id) is not self._connection:
            return
        if self._connection is None:
            return
        self._manager._connections.pop(self._machine_id, None)
        reason = f"{type(exc).__name__}: {exc}" if exc else "remote transport closed"
        logger.warning("SSH connection lost for %s: %s", self._machine_id, reason)


class SSHConnectionManager:

    def __init__(self):
        self._connections: Dict[str, asyncssh.SSHClientConnection] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._reconnect_suppressed: set[str] = set()
        self._connection_timeout: int = 10
        self._command_timeout: int = 30

    def configure_timeouts(self, connection_timeout: int, command_timeout: int) -> None:
        self._connection_timeout = max(1, int(connection_timeout))
        self._command_timeout = max(1, int(command_timeout))

    def _resolve_key_path(self, raw_path: str) -> Optional[str]:
        if not raw_path:
            return None
        expanded = os.path.expanduser(os.path.expandvars(raw_path))
        if Path(expanded).exists():
            return expanded
        relative_to_controller = CONTROLLER_DIR / raw_path
        if relative_to_controller.exists():
            return str(relative_to_controller)
        return None

    @staticmethod
    def _shell_quote(value: str) -> str:
        return "'" + value.replace("'", "'\\''") + "'"

    def _lock_for(self, machine_id: str) -> asyncio.Lock:
        if machine_id not in self._locks:
            self._locks[machine_id] = asyncio.Lock()
        return self._locks[machine_id]

    def reconnect_enabled(self, machine_id: str) -> bool:
        return machine_id not in self._reconnect_suppressed

    async def connect(self, machine_id: str, config: MachineConfig, *, automatic: bool = False) -> bool:
        async with self._lock_for(machine_id):
            if automatic and not self.reconnect_enabled(machine_id):
                return False
            if not automatic:
                self._reconnect_suppressed.discard(machine_id)
            if self.is_connected(machine_id):
                return True

            old_conn = self._connections.pop(machine_id, None)
            if old_conn:
                with suppress(Exception):
                    old_conn.close()
                    await asyncio.wait_for(old_conn.wait_closed(), timeout=1)
            try:
                connect_kwargs = {
                    "host": config.host,
                    "port": config.port,
                    "username": config.user,
                    "known_hosts": None,
                    "client_factory": lambda: _ConnectionEvents(self, machine_id),
                    "keepalive_interval": 15,
                    "keepalive_count_max": 3,
                }
                key_path = self._resolve_key_path(config.ssh_key_path) if config.ssh_key_path else None
                if key_path:
                    connect_kwargs["client_keys"] = [key_path]
                else:
                    if config.ssh_key_path:
                        logger.warning("SSH key not found: %s", config.ssh_key_path)
                conn = await asyncio.wait_for(asyncssh.connect(**connect_kwargs), timeout=self._connection_timeout)
                self._connections[machine_id] = conn
                logger.info(f"Connected to {machine_id} ({config.host})")
                return True
            except asyncio.TimeoutError:
                logger.error(f"Connection timeout to {machine_id}")
                return False
            except asyncssh.PermissionDenied as e:
                logger.error(f"Permission denied for user {config.user} on host {config.host}")
                raise
            except Exception as e:
                logger.error(f"Failed to connect to {machine_id}: {e}")
                return False

    async def disconnect(self, machine_id: str, *, suppress_reconnect: bool = False) -> None:
        async with self._lock_for(machine_id):
            if suppress_reconnect:
                self._reconnect_suppressed.add(machine_id)
            else:
                self._reconnect_suppressed.discard(machine_id)
            conn = self._connections.pop(machine_id, None)
            if conn:
                try:
                    conn.close()
                    try:
                        await asyncio.wait_for(conn.wait_closed(), timeout=2)
                    except asyncio.TimeoutError:
                        logger.debug(f"Timeout closing connection to {machine_id}")
                except asyncio.CancelledError:
                    logger.debug(f"Disconnect cancelled for {machine_id}")
                except Exception as e:
                    logger.debug(f"Error closing connection to {machine_id}: {e}")

    async def disconnect_all(self) -> None:
        machine_ids = set(self._connections) | self._reconnect_suppressed
        for machine_id in machine_ids:
            try:
                await self.disconnect(machine_id)
            except Exception as e:
                logger.warning(f"Error disconnecting {machine_id}: {e}")

    def is_connected(self, machine_id: str) -> bool:
        if machine_id not in self._connections:
            return False
        conn = self._connections[machine_id]
        return not conn.is_closed()

    async def execute(self, machine_id: str, command: str, timeout: Optional[int] = None) -> tuple[int, str, str]:
        if not self.is_connected(machine_id):
            raise ConnectionError(f"Not connected to {machine_id}")
        conn = self._connections[machine_id]
        effective_timeout = timeout if timeout is not None else self._command_timeout

        async def run_command():
            process = await conn.create_process(command)
            try:
                return await process.wait(check=False)
            except asyncio.CancelledError:
                # Stop only this command on timeout or caller cancellation.
                with suppress(Exception):
                    process.terminate()
                raise
            finally:
                # Cancelling conn.run() alone leaves its SSH channel open.
                with suppress(Exception):
                    process.close()
                    try:
                        await asyncio.wait_for(process.wait_closed(), timeout=2)
                    except asyncio.TimeoutError:
                        process.channel.abort()

        try:
            result = await asyncio.wait_for(run_command(), timeout=effective_timeout)
            if result.exit_status is None:
                if conn.is_closed():
                    raise ConnectionError("Connection closed before the command returned an exit status")
                raise RuntimeError(f"Command on {machine_id} ended without an exit status: {command}")
            return (result.exit_status, result.stdout or "", result.stderr or "")
        except asyncio.TimeoutError:
            raise TimeoutError(f"Command timed out on {machine_id}: {command}")
        except (asyncssh.DisconnectError, OSError, EOFError) as e:
            if self._connections.get(machine_id) is conn:
                self._connections.pop(machine_id, None)
                logger.warning("SSH connection lost for %s during command: %s: %s",
                               machine_id, type(e).__name__, e)
            with suppress(Exception):
                conn.close()
            raise ConnectionError(f"SSH connection to {machine_id} was lost: {e}") from e

    async def get_service_status(self, machine_id: str, service_name: str) -> Dict[str, Any]:
        cmd = f"systemctl --user show {service_name} --no-pager"
        exit_code, stdout, stderr = await self.execute(machine_id, cmd)
        if exit_code != 0 and "not found" in stderr.lower():
            return {
                "status": ServiceStatus.UNKNOWN,
                "active_state": "not-found",
                "sub_state": "",
                "error": f"Service {service_name} not found", }
        props = self._parse_systemd_properties(stdout)
        active_state = props.get("ActiveState", "unknown")
        sub_state = props.get("SubState", "")
        status_map = {
            "active": ServiceStatus.RUNNING,
            "inactive": ServiceStatus.STOPPED,
            "failed": ServiceStatus.FAILED,
            "activating": ServiceStatus.ACTIVATING,
            "deactivating": ServiceStatus.DEACTIVATING, }
        status = status_map.get(active_state, ServiceStatus.UNKNOWN)
        return {
            "status": status,
            "active_state": active_state,
            "sub_state": sub_state,
            "exit_code": int(props.get("ExecMainStatus", 0)),
            "pid": int(props.get("MainPID", 0)) or None,
            "memory_current": props.get("MemoryCurrent", ""),
            "started_at": props.get("ActiveEnterTimestamp", ""), }

    @staticmethod
    def _parse_systemd_properties(output: str) -> Dict[str, str]:
        props: Dict[str, str] = {}
        for line in output.strip().splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            props[key] = value
        return props

    async def start_service(self, machine_id: str, service_name: str) -> tuple[bool, str]:
        cmd = f"systemctl --user start {service_name}"
        exit_code, stdout, stderr = await self.execute(machine_id, cmd, timeout=60)
        if exit_code == 0:
            return True, f"Service {service_name} started"
        else:
            return False, stderr or f"Failed to start {service_name}"

    async def stop_service(self, machine_id: str, service_name: str) -> tuple[bool, str]:
        cmd = (f"systemctl --user stop {service_name} 2>/dev/null || true; "
               f"systemctl --user reset-failed {service_name} 2>/dev/null || true")
        exit_code, stdout, stderr = await self.execute(machine_id, cmd, timeout=15)
        return True, f"Service {service_name} stopped"

    async def restart_service(self, machine_id: str, service_name: str) -> tuple[bool, str]:
        stop_cmd = (f"systemctl --user stop {service_name} 2>/dev/null || true; "
                    f"systemctl --user reset-failed {service_name} 2>/dev/null || true")
        await self.execute(machine_id, stop_cmd, timeout=15)
        start_cmd = f"systemctl --user start {service_name}"
        exit_code, stdout, stderr = await self.execute(machine_id, start_cmd, timeout=60)
        if exit_code == 0:
            return True, f"Service {service_name} restarted"
        else:
            return False, stderr or f"Failed to start {service_name}"

    async def get_service_logs(self, machine_id: str, service_name: str, lines: int = 100, since: Optional[
        str] = None, ) -> str:
        cmd = f"journalctl --user -u {self._shell_quote(service_name)} -n {lines} --no-pager -o short"
        if since:
            cmd += f" --since {self._shell_quote(since)}"
        cmd += " | awk '{time=$3; msg=\"\"; for(i=6;i<=NF;i++) msg=msg\" \"$i; print time\": \"msg}'"
        exit_code, stdout, stderr = await self.execute(machine_id, cmd, timeout=30)
        return stdout

    async def deploy_service_files(self, machine_id: str, services: List[Dict[str, str]], ) -> tuple[bool, str]:
        if not self.is_connected(machine_id):
            return False, f"Not connected to {machine_id}"
        try:
            svc_dir = "~/.config/systemd/user"
            await self.execute(machine_id, f"mkdir -p {svc_dir}", timeout=10)
            desired_filenames = {svc["filename"] for svc in services if
                re.fullmatch(r"ros2-[A-Za-z0-9_.@-]+\.service", svc["filename"])}
            if len(desired_filenames) != len(services):
                return False, "Refusing to deploy an invalid service filename"
            list_cmd = (f"find {svc_dir} -maxdepth 1 -type f -name 'ros2-*.service' "
                        "-printf '%f\\n'")
            _, installed_stdout, _ = await self.execute(machine_id, list_cmd, timeout=10)
            installed_filenames = {line.strip() for line in installed_stdout.splitlines() if
                re.fullmatch(r"ros2-[A-Za-z0-9_.@-]+\.service", line.strip())}
            obsolete_filenames = sorted(installed_filenames - desired_filenames)
            if obsolete_filenames:
                obsolete_units = " ".join(self._shell_quote(name) for name in obsolete_filenames)
                await self.execute(machine_id, f"systemctl --user stop {obsolete_units} 2>/dev/null || true", timeout=30, )
                obsolete_paths = " ".join(f"{svc_dir}/{name}" for name in obsolete_filenames)
                await self.execute(machine_id, f"rm -f {obsolete_paths}", timeout=10)
            deployed = []
            for svc in services:
                filename = svc["filename"]
                content = svc["content"]
                cmd = f"cat > {svc_dir}/{filename} << 'SERVICEEOF'\n{content}\nSERVICEEOF"
                exit_code, _, stderr = await self.execute(machine_id, cmd, timeout=15)
                if exit_code != 0:
                    return False, f"Failed to write {filename}: {stderr}"
                deployed.append(filename)
            exit_code, _, stderr = await self.execute(machine_id, "systemctl --user daemon-reload", timeout=15)
            if exit_code != 0:
                return False, f"daemon-reload failed: {stderr}"
            message = f"Deployed {len(deployed)} services without interrupting running services"
            if obsolete_filenames:
                message += f"; removed {len(obsolete_filenames)} obsolete services"
            return True, message
        except Exception as e:
            return False, str(e)

    async def write_file(self, machine_id: str, content: bytes, remote_path: str) -> None:
        if not self.is_connected(machine_id):
            raise ConnectionError(f"Not connected to {machine_id}")
        conn = self._connections[machine_id]
        async with conn.start_sftp_client() as sftp:
            async with sftp.open(remote_path, 'wb') as f:
                await f.write(content)

    async def stream_service_logs(self, machine_id: str, service_name: str) -> AsyncIterator[str]:
        if not self.is_connected(machine_id):
            raise ConnectionError(f"Not connected to {machine_id}")
        conn = self._connections[machine_id]
        cmd = (f"journalctl --user -u {self._shell_quote(service_name)} "
               "-f -n 0 --no-pager -o short")
        process = await conn.create_process(cmd)
        try:
            while True:
                raw_line = await process.stdout.readline()
                if not raw_line:
                    break
                raw_line = raw_line.rstrip("\r\n")
                parts = raw_line.split()
                if len(parts) >= 6:
                    time_part = parts[2]
                    msg_part = " ".join(parts[5:])
                    yield f"{time_part}: {msg_part}"
        finally:
            process.close()
            with suppress(Exception):
                await asyncio.wait_for(process.wait_closed(), timeout=2)


ssh_manager = SSHConnectionManager()

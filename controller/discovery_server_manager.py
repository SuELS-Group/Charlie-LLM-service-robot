from __future__ import annotations

import os
import re
import shlex
import signal
import socket
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from discovery_config import (discovery_endpoint, discovery_listen_arguments, discovery_port, format_with_domain, load_discovery_config, server_config_path, )


@dataclass
class DiscoveryServerProcess:
    port: int
    endpoint: str
    command: str
    log_file: Path
    systemd_unit: Optional[str] = None
    process: Optional[subprocess.Popen[str]] = None
    adopted_pid: Optional[int] = None
    log_handle: Optional[Any] = None
    started_at: Optional[float] = None
    last_error: Optional[str] = None


class DiscoveryServerManager:

    def __init__(self, config_dir: Path):
        self.config_dir = config_dir
        self.config: Dict[str, Any] = load_discovery_config(config_dir)
        self._state = self._build_state()

    def reload(self, advertise_host_override: Optional[str] = None) -> None:
        previous_state = self._state
        running_pid = self._running_pid(previous_state)
        was_running = running_pid is not None
        self.config = load_discovery_config(self.config_dir)
        if advertise_host_override:
            self.config["advertise_host"] = advertise_host_override
        self._state = self._build_state()
        self._state.last_error = previous_state.last_error
        if not self.config.get("enabled"):
            self._stop_state(previous_state)
            return
        # The rendered command includes the concrete listening addresses. A
        # change of the selected robot LAN therefore requires a new process;
        # reloading the same address preserves the existing discovery graph.
        command_changed = previous_state.command != self._state.command
        if command_changed and running_pid is not None:
            # On controller startup the on-disk advertised address may still be
            # localhost until runtime LAN selection. An adopted server may
            # already have exactly the final command; preserve that process.
            command_changed = not self._process_command_matches(running_pid, self._state.command)
        changed = (
                previous_state.port != self._state.port or command_changed or previous_state.systemd_unit != self._state.systemd_unit)
        if was_running and changed:
            self._stop_state(previous_state)
            if self.config.get("auto_start"):
                self.start()
            return
        self._state.process = previous_state.process
        self._state.adopted_pid = previous_state.adopted_pid
        self._state.log_handle = previous_state.log_handle
        self._state.started_at = previous_state.started_at
        if self.config.get("auto_start"):
            self.start()

    def start(self) -> Dict[str, Any]:
        if not self.config.get("enabled"):
            self._state.last_error = "Discovery manager is disabled in config"
            return self.status()
        # A discovery server must keep the same process identity while ROS
        # participants are running. Reuse one left by an earlier controller
        # process instead of replacing it and silently splitting the DDS graph.
        if self._running_pid(self._state) is not None:
            self._state.last_error = None
            return self.status()
        bind_host = str(self.config.get("bind_host", "0.0.0.0"))
        if self._is_udp_port_in_use(bind_host, self._state.port):
            self._state.last_error = f"UDP port {self._state.port} already in use"
            return self.status()
        env = os.environ.copy()
        profile_path = server_config_path(self.config_dir, self.config)
        for key, value in (self.config.get("process_env") or {}).items():
            if not isinstance(value, str):
                continue
            env[
                key] = format_with_domain(value, domain_id=0, port=self._state.port, bind_host=bind_host, endpoint=self._state.endpoint, config_file=str(profile_path), )
        if self._state.systemd_unit:
            return self._start_systemd_unit(env)
        self._open_log()
        try:
            self._state.process = subprocess.Popen(shlex.split(self._state.command), stdout=self._state.log_handle, stderr=self._state.log_handle, env=env, text=True, start_new_session=True, )
            self._state.adopted_pid = None
            self._state.started_at = time.time()
            self._state.last_error = None
        except Exception as exc:
            self._state.process = None
            self._state.started_at = None
            self._state.last_error = str(exc)
            return self.status()
        wait_ms = int(self.config.get("startup_wait_ms", 300))
        time.sleep(max(wait_ms, 0) / 1000)
        if self._state.process.poll() is not None:
            self._state.last_error = self._last_log_line(self._state.log_file) or (
                f"Exited with code {self._state.process.returncode}")
        return self.status()

    def stop(self) -> Dict[str, Any]:
        self._stop_state(self._state)
        return self.status()

    def restart(self) -> Dict[str, Any]:
        self.stop()
        return self.start()

    def stop_all(self) -> None:
        self.stop()

    def shutdown(self) -> None:
        if not self.config.get("preserve_on_controller_shutdown", True):
            self.stop_all()
            return
        running_pid = self._running_pid(self._state)
        if running_pid is not None:
            self._state.adopted_pid = running_pid
        # The discovery process owns its inherited log descriptor and its own
        # session. Dropping the Popen handle here deliberately leaves it alive
        # for the next controller process to adopt.
        self._state.process = None
        self._close_log_handle(self._state)

    def status(self) -> Dict[str, Any]:
        process = self._state.process
        if process is not None and process.poll() is not None and self._state.last_error is None:
            self._state.last_error = f"Exited with code {process.returncode}"
        pid = self._running_pid(self._state)
        running = pid is not None
        return {
            "running": running,
            "pid": pid,
            "port": self._state.port,
            "endpoint": self._state.endpoint,
            "command": self._state.command,
            "last_error": self._state.last_error,
            "started_at": self._state.started_at,
            "log_file": str(self._state.log_file), }

    def stop_existing_process(self, state: DiscoveryServerProcess) -> None:
        process = state.process
        if process is None or process.poll() is not None:
            state.process = None
            state.adopted_pid = None
            self._close_log_handle(state)
            return
        timeout = int(self.config.get("stop_timeout_seconds", 5))
        try:
            pgid = os.getpgid(process.pid)
        except Exception:
            pgid = None
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=max(timeout, 1))
        except Exception:
            with suppress(Exception):
                if pgid is not None:
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    process.kill()
        state.process = None
        state.adopted_pid = None
        self._close_log_handle(state)

    def _stop_state(self, state: DiscoveryServerProcess) -> None:
        self._stop_systemd_unit(state.systemd_unit)
        self.stop_existing_process(state)
        self._terminate_external_discovery_on_port(state.port)
        state.adopted_pid = None

    def _running_pid(self, state: DiscoveryServerProcess) -> Optional[int]:
        process = state.process
        if process is not None and process.poll() is None:
            return process.pid
        matches = self._matching_discovery_pids(state.port)
        if not matches:
            state.adopted_pid = None
            return None
        if state.adopted_pid in matches:
            return state.adopted_pid
        state.adopted_pid = min(matches)
        return state.adopted_pid

    def _start_systemd_unit(self, env: Dict[str, str]) -> Dict[str, Any]:
        unit = str(self._state.systemd_unit)
        self._state.log_file.parent.mkdir(parents=True, exist_ok=True)
        command = ["systemd-run",
            "--user",
            "--quiet",
            "--collect",
            f"--unit={unit}",
            "--property=Restart=on-failure",
            f"--property=RestartSec={int(self.config.get('systemd_restart_seconds', 2))}s",
            f"--property=TimeoutStopSec={int(self.config.get('stop_timeout_seconds', 5))}s",
            f"--property=StandardOutput=append:{self._state.log_file}",
            f"--property=StandardError=append:{self._state.log_file}", ]
        for key, value in (self.config.get("process_env") or {}).items():
            if isinstance(value, str):
                command.append(f"--setenv={key}={env[key]}")
        command.extend(shlex.split(self._state.command))
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=max(int(self.config.get("stop_timeout_seconds", 5)), 1), check=False, )
        except Exception as exc:
            self._state.last_error = str(exc)
            return self.status()
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            self._state.last_error = (detail or f"systemd-run exited with code {result.returncode}")
            return self.status()
        self._state.started_at = time.time()
        self._state.last_error = None
        wait_ms = int(self.config.get("startup_wait_ms", 300))
        time.sleep(max(wait_ms, 0) / 1000)
        if self._running_pid(self._state) is None:
            self._state.last_error = (
                    self._last_log_line(self._state.log_file) or f"{unit} started without a discovery process")
        return self.status()

    def _stop_systemd_unit(self, unit: Optional[str]) -> None:
        if not unit:
            return
        with suppress(Exception):
            subprocess.run(["systemctl",
                            "--user",
                            "stop",
                            str(unit)], capture_output=True, text=True, timeout=max(int(self.config.get("stop_timeout_seconds", 5)), 1), check=False, )

    def _build_state(self) -> DiscoveryServerProcess:
        port = discovery_port(self.config)
        endpoint = discovery_endpoint(self.config)
        command = self._render_command(port, endpoint)
        configured_unit = self.config.get("systemd_unit")
        systemd_unit = str(configured_unit) if configured_unit else None
        return DiscoveryServerProcess(port=port, endpoint=endpoint, command=command, log_file=self.config_dir / "logs" / "discovery-server.log", systemd_unit=systemd_unit, )

    def _render_command(self, port: int, endpoint: str) -> str:
        profile_path = server_config_path(self.config_dir, self.config)
        bind_host = str(self.config.get("bind_host", "0.0.0.0"))
        return format_with_domain(str(self.config.get("command_template", "")), domain_id=0, port=port, bind_host=bind_host, endpoint=endpoint, config_file=str(profile_path), listen_args=discovery_listen_arguments(self.config, port), )

    def _open_log(self) -> None:
        if self._state.log_handle:
            self._state.log_handle.close()
        self._state.log_file.parent.mkdir(parents=True, exist_ok=True)
        self._state.log_handle = open(self._state.log_file, "a", buffering=1)

    @staticmethod
    def _last_log_line(path: Path) -> Optional[str]:
        if not path.exists():
            return None
        try:
            lines = path.read_text().strip().splitlines()
            if not lines:
                return None
            return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", lines[-1])
        except Exception:
            return None

    @staticmethod
    def _is_udp_port_in_use(bind_host: str, port: int) -> bool:
        host = "0.0.0.0" if bind_host in ("", "0.0.0.0") else bind_host
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((host, port))
            return False
        except OSError:
            return True
        finally:
            sock.close()

    @staticmethod
    def _terminate_external_discovery_on_port(port: int) -> None:
        pids = DiscoveryServerManager._matching_discovery_pids(port)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except Exception:
                continue
        deadline = time.time() + 2.0
        while time.time() < deadline:
            alive = [pid for pid in pids if DiscoveryServerManager._pid_exists(pid)]
            if not alive:
                return
            time.sleep(0.1)
        for pid in pids:
            if not DiscoveryServerManager._pid_exists(pid):
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except Exception:
                continue

    @staticmethod
    def _process_command_matches(pid: int, command: str) -> bool:
        try:
            actual = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", errors="ignore")
            return [part for part in actual.split("\x00") if part] == shlex.split(command)
        except OSError:
            return False

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except Exception:
            return True

    @staticmethod
    def _matching_discovery_pids(port: int) -> list[int]:
        matches: list[int] = []
        current_pid = os.getpid()
        try:
            proc_entries = list(Path("/proc").iterdir())
        except Exception:
            return matches
        for entry in proc_entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == current_pid:
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().decode("utf-8", errors="ignore")
                argv = [part for part in cmdline.split("\x00") if part]
            except Exception:
                continue
            if DiscoveryServerManager._is_discovery_process_for_port(argv, port):
                matches.append(pid)
        return matches

    @staticmethod
    def _is_discovery_process_for_port(argv: list[str], port: int) -> bool:
        if not argv:
            return False
        executable = os.path.basename(argv[0])
        is_fast_discovery_server = executable == "fast-discovery-server"
        is_fastdds_discovery = executable == "fastdds" and len(argv) > 1 and argv[1] == "discovery"
        if not (is_fast_discovery_server or is_fastdds_discovery):
            return False
        expected_port = str(port)
        for index, arg in enumerate(argv):
            if arg == "-p" and index + 1 < len(argv) and argv[index + 1] == expected_port:
                return True
            if arg == "--port" and index + 1 < len(argv) and argv[index + 1] == expected_port:
                return True
            if arg.startswith("--udp-port=") and arg.split("=", 1)[1] == expected_port:
                return True
            if arg.startswith("--port=") and arg.split("=", 1)[1] == expected_port:
                return True
        return False

    @staticmethod
    def _close_log_handle(state: DiscoveryServerProcess) -> None:
        if not state.log_handle:
            return
        with suppress(Exception):
            state.log_handle.close()
        state.log_handle = None

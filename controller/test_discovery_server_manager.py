import json
import subprocess
from pathlib import Path

import pytest

import discovery_server_manager as discovery_module
from discovery_server_manager import DiscoveryServerManager


class FakeProcess:

    def __init__(self, pid: int = 4321):
        self.pid = pid
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True


def _config_dir(tmp_path: Path, **overrides) -> Path:
    config = {
        "enabled": True,
        "auto_start": True,
        "preserve_on_controller_shutdown": True,
        "bind_host": "0.0.0.0",
        "advertise_host": "192.0.2.10",
        "port": 30000,
        "shared_config_file": "profiles.xml",
        "command_template": ("fastdds discovery --server-id 0 "
                             "--ip-address {bind_host} --port {port}"),
        "startup_wait_ms": 0,
        "stop_timeout_seconds": 1,
        "process_env": {}, }
    config.update(overrides)
    (tmp_path / "profiles.xml").write_text("<profiles/>")
    (tmp_path / "dds_discovery_servers.json").write_text(json.dumps(config))
    return tmp_path


def test_start_adopts_existing_server_without_replacing_it(tmp_path, monkeypatch):
    manager = DiscoveryServerManager(_config_dir(tmp_path))
    monkeypatch.setattr(manager, "_matching_discovery_pids", lambda _port: [7654])
    monkeypatch.setattr(discovery_module.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in
                                                                                         ()).throw(AssertionError("existing discovery server must be reused")), )
    status = manager.start()
    assert status["running"] is True
    assert status["pid"] == 7654


def test_reload_with_runtime_advertise_address_keeps_existing_server(tmp_path, monkeypatch):
    manager = DiscoveryServerManager(_config_dir(tmp_path, advertise_host="127.0.0.1"))
    monkeypatch.setattr(manager, "_matching_discovery_pids", lambda _port: [7654])
    terminated = []
    monkeypatch.setattr(manager, "_terminate_external_discovery_on_port", lambda port: terminated.append(port), )
    manager.reload(advertise_host_override="192.0.2.10")
    assert manager.status()["running"] is True
    assert manager.status()["pid"] == 7654
    assert manager.status()["endpoint"] == "192.0.2.10:30000"
    assert terminated == []


def test_production_server_runs_in_independent_systemd_unit(tmp_path, monkeypatch):
    manager = DiscoveryServerManager(_config_dir(tmp_path, systemd_unit="fastdds-discovery-server.service", systemd_restart_seconds=2, ))
    matches = iter([[], [8765], [8765]])
    systemd_run = []
    monkeypatch.setattr(manager, "_matching_discovery_pids", lambda _port: next(matches))
    monkeypatch.setattr(manager, "_is_udp_port_in_use", lambda _host, _port: False)

    def fake_run(command, **_kwargs):
        systemd_run.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(discovery_module.subprocess, "run", fake_run)
    status = manager.start()
    assert status["running"] is True
    assert status["pid"] == 8765
    assert systemd_run[0][:5] == ["systemd-run",
        "--user",
        "--quiet",
        "--collect",
        "--unit=fastdds-discovery-server.service", ]
    assert "--property=Restart=on-failure" in systemd_run[0]


@pytest.mark.parametrize('advertise_host', ['192.168.50.50', '192.168.50.49'])
def test_wildcard_production_server_listens_only_on_selected_lan_and_loopback(advertise_host):
    manager = DiscoveryServerManager(Path(__file__).parent / 'config')
    manager.config['advertise_host'] = advertise_host
    command = manager._render_command(30000, f'{advertise_host}:30000')
    assert f'-l {advertise_host} -p 30000 -l 127.0.0.1 -p 30000' in command
    assert '0.0.0.0' not in command


def test_stop_uses_unit_recorded_in_original_state(tmp_path, monkeypatch):
    manager = DiscoveryServerManager(_config_dir(tmp_path, systemd_unit="new-discovery.service"))
    original_state = manager._state
    original_state.systemd_unit = "old-discovery.service"
    stopped_units = []
    monkeypatch.setattr(manager, "_stop_systemd_unit", lambda unit: stopped_units.append(unit))
    monkeypatch.setattr(manager, "stop_existing_process", lambda _state: None)
    monkeypatch.setattr(manager, "_terminate_external_discovery_on_port", lambda _port: None)
    manager._stop_state(original_state)
    assert stopped_units == ["old-discovery.service"]


def test_shutdown_preserves_discovery_server_for_next_controller(tmp_path, monkeypatch):
    manager = DiscoveryServerManager(_config_dir(tmp_path))
    process = FakeProcess()
    manager._state.process = process
    monkeypatch.setattr(manager, "_matching_discovery_pids", lambda _port: [process.pid], )
    manager.shutdown()
    assert process.terminated is False
    assert manager._state.process is None
    assert manager._state.adopted_pid == process.pid

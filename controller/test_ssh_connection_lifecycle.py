import asyncio

import pytest

from models import MachineConfig, MachineType
from ssh_manager import SSHConnectionManager, _ConnectionEvents
from web import lifecycle, state


class FakeConnection:
    def __init__(self, run_error=None):
        self.closed = False
        self.run_error = run_error

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None

    async def create_process(self, _command):
        if self.run_error:
            raise self.run_error
        return None


def test_connect_configures_ssh_keepalives(monkeypatch):
    manager = SSHConnectionManager()
    connection = FakeConnection()
    received_kwargs = {}

    async def fake_connect(**kwargs):
        received_kwargs.update(kwargs)
        return connection

    monkeypatch.setattr("ssh_manager.asyncssh.connect", fake_connect)
    config = MachineConfig(name="Robot", host="192.0.2.20", user="robot", type=MachineType.ROBOT)

    assert asyncio.run(manager.connect("robot", config)) is True
    assert received_kwargs["keepalive_interval"] == 15
    assert received_kwargs["keepalive_count_max"] == 3
    assert manager.is_connected("robot")


def test_transport_error_marks_connection_disconnected():
    manager = SSHConnectionManager()
    connection = FakeConnection(run_error=OSError("network down"))
    manager._connections["robot"] = connection

    with pytest.raises(ConnectionError, match="SSH connection to robot was lost"):
        asyncio.run(manager.execute("robot", "systemctl --user show ros2-demo.service"))

    assert connection.closed
    assert not manager.is_connected("robot")


def test_unexpected_transport_loss_records_reason_and_allows_reconnect(caplog):
    manager = SSHConnectionManager()
    connection = FakeConnection()
    manager._connections["robot"] = connection
    events = _ConnectionEvents(manager, "robot")
    events.connection_made(connection)

    events.connection_lost(ConnectionResetError("Connection reset by peer"))
    events.connection_lost(ConnectionResetError("Connection reset by peer"))

    assert not manager.is_connected("robot")
    assert manager.reconnect_enabled("robot")
    assert "robot: ConnectionResetError: Connection reset by peer" in caplog.text
    assert len(caplog.records) == 1


def test_old_transport_callback_preserves_replacement(caplog):
    manager = SSHConnectionManager()
    old_connection = FakeConnection()
    replacement = FakeConnection()
    events = _ConnectionEvents(manager, "robot")
    events.connection_made(old_connection)
    manager._connections["robot"] = replacement

    events.connection_lost(OSError("old transport failed"))

    assert manager._connections["robot"] is replacement
    assert manager.is_connected("robot")
    assert not caplog.records


def test_manual_disconnect_callback_is_not_reported_as_failure(caplog):
    manager = SSHConnectionManager()
    connection = FakeConnection()
    events = _ConnectionEvents(manager, "robot")
    events.connection_made(connection)
    manager._connections["robot"] = connection
    asyncio.run(manager.disconnect("robot", suppress_reconnect=True))

    events.connection_lost(None)

    assert not manager.reconnect_enabled("robot")
    assert not caplog.records


def test_automatic_reconnect_respects_manual_disconnect(monkeypatch):
    manager = SSHConnectionManager()
    manager._reconnect_suppressed.add("robot")
    config = MachineConfig(name="Robot", host="192.0.2.20", user="robot", type=MachineType.ROBOT)

    async def unexpected_connect(**_kwargs):
        raise AssertionError("A manually disconnected machine must not be reconnected")

    monkeypatch.setattr("ssh_manager.asyncssh.connect", unexpected_connect)

    assert asyncio.run(manager.connect("robot", config, automatic=True)) is False


def test_connection_supervisor_retries_disconnected_machine(monkeypatch):
    config = MachineConfig(name="Robot", host="192.0.2.20", user="robot", type=MachineType.ROBOT)
    monkeypatch.setattr(state, "machines_config", {"robot": config})
    monkeypatch.setattr(lifecycle.ssh_manager, "is_connected", lambda _machine_id: False)
    monkeypatch.setattr(lifecycle.ssh_manager, "reconnect_enabled", lambda _machine_id: True)

    async def run_supervisor():
        stop_event = asyncio.Event()
        attempted = asyncio.Event()

        async def fake_connect(machine_id, machine_config, *, automatic=False):
            assert machine_id == "robot"
            assert machine_config is config
            assert automatic is True
            attempted.set()
            stop_event.set()
            return True

        monkeypatch.setattr(lifecycle.ssh_manager, "connect", fake_connect)
        await asyncio.wait_for(lifecycle.maintain_machine_connections(stop_event), timeout=1.0)
        assert attempted.is_set()

    asyncio.run(run_supervisor())

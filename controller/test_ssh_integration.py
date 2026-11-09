"""Exercise command cleanup and recovery against a real, local SSH server."""

import asyncio
from contextlib import asynccontextmanager

import asyncssh
import pytest

from models import MachineConfig, MachineType
from ssh_manager import SSHConnectionManager
from web import lifecycle, state


class TestSession(asyncssh.SSHServerSession):
    __test__ = False

    def __init__(self, owner):
        self.owner = owner

    def connection_made(self, channel):
        self.channel = channel
        self.owner.sessions.add(self)

    def connection_lost(self, exc):
        self.owner.sessions.discard(self)

    def exec_requested(self, command):
        self.command = command
        return True

    def session_started(self):
        if self.command == 'hang':
            return
        if self.command == 'no_exit_status':
            self.channel.close()
            return
        self.channel.write('ActiveState=active\nSubState=running\nMainPID=123\nExecMainStatus=0\n')
        self.channel.exit(0)

    def eof_received(self):
        return True


class TestServer(asyncssh.SSHServer):
    __test__ = False

    def __init__(self, registry):
        self.sessions = set()
        registry.append(self)

    def connection_made(self, connection):
        self.connection = connection

    def begin_auth(self, username):
        return False

    def session_requested(self):
        if len(self.sessions) >= 2:
            raise asyncssh.ChannelOpenError(asyncssh.OPEN_RESOURCE_SHORTAGE, 'Session limit reached')
        return TestSession(self)


class LocalSSH:
    def __init__(self):
        self.servers = []
        self.host_key = asyncssh.generate_private_key('ssh-ed25519')
        self.listener = None
        self.port = 0

    async def start(self):
        self.listener = await asyncssh.create_server(
            lambda: TestServer(self.servers), '127.0.0.1', self.port,
            server_host_keys=[self.host_key], reuse_address=True,
        )
        self.port = self.listener.get_port()

    async def stop(self):
        if self.listener:
            self.listener.close()
            await self.listener.wait_closed()
            self.listener = None
        for server in self.servers:
            server.connection.abort()
        await asyncio.gather(*(server.connection.wait_closed() for server in self.servers))


async def wait_until(predicate, timeout=6):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(poll(), timeout=timeout)


@asynccontextmanager
async def connected_server():
    server = LocalSSH()
    manager = SSHConnectionManager()
    try:
        await server.start()
        config = MachineConfig(name='Local test', host='127.0.0.1', port=server.port,
                               user='test', type=MachineType.ROBOT)
        assert await manager.connect('robot', config)
        yield server, manager, config
    finally:
        await manager.disconnect_all()
        await server.stop()


def test_timeouts_and_cancellation_release_only_their_command_channel():
    async def check():
        async with connected_server() as (server, manager, _):
            connection = manager._connections['robot']
            # An unrelated session must survive the timeouts (e.g. a log stream).
            other_process = await connection.create_process('hang')
            try:
                for _ in range(4):
                    with pytest.raises(TimeoutError):
                        await manager.execute('robot', 'hang', timeout=0.1)
                    await wait_until(lambda: len(server.servers[-1].sessions) == 1)
                    assert manager._connections['robot'] is connection
                    assert not other_process.is_closing()
                    assert (await manager.get_service_status('robot', 'demo.service'))['status'] == 'running'

                task = asyncio.create_task(manager.execute('robot', 'hang'))
                await wait_until(lambda: len(server.servers[-1].sessions) == 2)
                # Let the exec handshake finish before cancelling the command.
                await asyncio.sleep(0.05)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await wait_until(lambda: len(server.servers[-1].sessions) == 1)
                assert manager.is_connected('robot')
                assert not other_process.is_closing()
            finally:
                other_process.close()
                await other_process.wait_closed()

    asyncio.run(check())


def test_channel_limit_does_not_disconnect_other_sessions():
    async def check():
        async with connected_server() as (server, manager, _):
            connection = manager._connections['robot']
            first = await connection.create_process('hang')
            second = await connection.create_process('hang')
            try:
                with pytest.raises(asyncssh.ChannelOpenError):
                    await manager.get_service_status('robot', 'demo.service')
                assert manager.is_connected('robot')
                assert manager._connections['robot'] is connection
                assert not first.is_closing()
                assert not second.is_closing()

                second.close()
                await second.wait_closed()
                await wait_until(lambda: len(server.servers[-1].sessions) == 1)
                assert (await manager.get_service_status('robot', 'demo.service'))['status'] == 'running'
            finally:
                first.close()
                second.close()
                await asyncio.gather(first.wait_closed(), second.wait_closed())

    asyncio.run(check())


def test_missing_command_exit_status_is_not_reported_as_success():
    async def check():
        async with connected_server() as (_, manager, _config):
            with pytest.raises(RuntimeError, match='without an exit status'):
                await manager.execute('robot', 'no_exit_status')
            assert manager.is_connected('robot')
            assert (await manager.get_service_status('robot', 'demo.service'))['status'] == 'running'

    asyncio.run(check())


def test_supervisor_recovers_after_interrupted_command_and_server_outage(monkeypatch):
    async def check():
        async with connected_server() as (server, manager, config):
            monkeypatch.setattr(lifecycle, 'ssh_manager', manager)
            monkeypatch.setattr(state, 'machines_config', {'robot': config})
            stop = asyncio.Event()
            supervisor = asyncio.create_task(lifecycle.maintain_machine_connections(stop))
            try:
                connection = manager._connections['robot']
                command = asyncio.create_task(manager.execute('robot', 'hang'))
                await wait_until(lambda: bool(server.servers[-1].sessions))
                await asyncio.sleep(0.05)
                server.servers[-1].connection.abort()
                with pytest.raises(ConnectionError):
                    await command
                await wait_until(lambda: manager.is_connected('robot')
                                 and manager._connections['robot'] is not connection)
                assert (await manager.get_service_status('robot', 'demo.service'))['status'] == 'running'

                connection = manager._connections['robot']
                await server.stop()
                # Leave the listener down long enough for a reconnect attempt to fail.
                await asyncio.sleep(1.25)
                await server.start()
                await wait_until(lambda: manager.is_connected('robot')
                                 and manager._connections['robot'] is not connection)
                assert (await manager.get_service_status('robot', 'demo.service'))['status'] == 'running'

                await manager.disconnect('robot', suppress_reconnect=True)
                await asyncio.sleep(1.25)
                assert not manager.is_connected('robot')
            finally:
                stop.set()
                await asyncio.wait_for(supervisor, timeout=3)

    asyncio.run(check())

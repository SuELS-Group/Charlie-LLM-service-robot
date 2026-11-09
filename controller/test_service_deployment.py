import asyncio

from ssh_manager import SSHConnectionManager


def test_deploy_does_not_interrupt_running_services(monkeypatch):
    manager = SSHConnectionManager()
    commands = []
    monkeypatch.setattr(manager, 'is_connected', lambda _machine_id: True)

    async def execute(_machine_id, command, timeout=None):
        commands.append(command)
        if command.startswith('find '):
            return 0, 'ros2-current.service\nros2-obsolete.service\n', ''
        return 0, '', ''

    monkeypatch.setattr(manager, 'execute', execute)
    services = [{
        'filename': 'ros2-current.service',
        'service_name': 'ros2-current',
        'content': '[Service]\nExecStart=/bin/true', }]
    success, message = asyncio.run(manager.deploy_service_files('robot', services))
    assert success
    assert 'without interrupting running services' in message
    assert not any('systemctl --user kill' in command for command in commands)
    assert any("systemctl --user stop 'ros2-obsolete.service'" in command for command in commands)
    assert not any("systemctl --user stop 'ros2-current.service'" in command for command in commands)
    assert any('rm -f ~/.config/systemd/user/ros2-obsolete.service' in command for command in commands)
    assert commands[-1] == 'systemctl --user daemon-reload'

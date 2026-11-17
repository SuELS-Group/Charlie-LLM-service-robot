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


def test_deploy_stops_previous_checkout_but_keeps_current_checkout_running(monkeypatch):
    manager = SSHConnectionManager()
    commands = []
    monkeypatch.setattr(manager, 'is_connected', lambda _: True)

    async def execute(_machine_id, command, timeout=None):
        commands.append(command)
        if command.startswith('find '):
            return 0, 'ros2-camera.service\nros2-action.service\n', ''
        if 'show' in command:
            root = '/old-checkout' if 'ros2-camera' in command else '/new-checkout'
            return 0, f'{{ path={root}/scripts/update_robot_ws.sh ; argv[]=... }}', ''
        return 0, '', ''

    monkeypatch.setattr(manager, 'execute', execute)
    services = [{'filename': f'ros2-{name}.service', 'service_name': f'ros2-{name}',
                 'content': '[Service]\nExecStart=/new-checkout/scripts/update_robot_ws.sh --run'}
                for name in ('camera', 'action')]
    success, message = asyncio.run(manager.deploy_service_files('robot', services))
    assert success
    assert 'stopped 1 services' in message
    assert "systemctl --user stop 'ros2-camera.service'" in commands
    assert not any('stop' in c and 'ros2-action.service' in c for c in commands)
    assert not any('systemctl --user start' in c for c in commands)


def test_deploy_aborts_if_previous_checkout_cannot_be_stopped(monkeypatch):
    manager = SSHConnectionManager()
    commands = []
    monkeypatch.setattr(manager, 'is_connected', lambda _: True)

    async def execute(_machine_id, command, timeout=None):
        commands.append(command)
        if command.startswith('find '):
            return 0, 'ros2-camera.service\n', ''
        if 'show' in command:
            return 0, '{ path=/old/scripts/update_robot_ws.sh ; argv[]=... }', ''
        if 'systemctl --user stop' in command:
            return 1, '', 'stop failed'
        return 0, '', ''

    monkeypatch.setattr(manager, 'execute', execute)
    success, message = asyncio.run(manager.deploy_service_files('robot', [{
        'filename': 'ros2-camera.service', 'service_name': 'ros2-camera',
        'content': '[Service]\nExecStart=/new/scripts/update_robot_ws.sh --run'}]))
    assert not success
    assert 'stop failed' in message
    assert not any(c.startswith('cat >') for c in commands)


def test_deploy_endpoint_regenerates_units_instead_of_reusing_old_paths(monkeypatch, tmp_path):
    from web.routes import admin
    stale = tmp_path / 'robot'
    stale.mkdir()
    (stale / 'ros2-camera.service').write_text('ExecStart=/old-checkout/scripts/update_robot_ws.sh')
    monkeypatch.setattr(admin, 'require_current_service_generator', lambda: None)
    monkeypatch.setattr(admin, 'detect_private_discovery_ip', lambda: '192.168.50.50')
    monkeypatch.setattr(admin.state, 'machines_config', {'robot': object()})
    monkeypatch.setattr(admin.state, 'GENERATED_DIR', tmp_path)
    monkeypatch.setattr(admin.ssh_manager, 'is_connected', lambda _: True)
    fresh = [{'filename': 'ros2-camera.service', 'service_name': 'ros2-camera',
              'content': 'ExecStart=/new-checkout/scripts/update_robot_ws.sh'}]
    monkeypatch.setattr(admin, 'generate_all_services', lambda *args, **kwargs: {'robot': fresh})
    deployed = []

    async def deploy(machine_id, services):
        deployed.extend(services)
        return True, 'deployed'

    monkeypatch.setattr(admin.ssh_manager, 'deploy_service_files', deploy)
    result = asyncio.run(admin.api_deploy_services('robot'))
    assert result['status'] == 'success'
    assert deployed == fresh

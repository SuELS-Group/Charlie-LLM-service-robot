from pathlib import Path
import shutil
import subprocess

import pytest

import generate_services
from discovery_config import build_service_env, load_discovery_config
from generate_services import (ensure_loaded_generator_is_current, generate_service_file, remove_stale_generated_services, )


def test_service_restart_policy_is_configurable():
    node = {
        'name': 'Vision',
        'command': 'ros2 run vision_node vision_node',
        'service_name': 'ros2-vision',
        'restart': 'on-failure',
        'restart_sec': 3,
        'start_limit_interval_sec': 60,
        'start_limit_burst': 3, }
    content = generate_service_file(node=node, machine_type='workstation', ros_domain_id=42, project_path='/tmp/project', user='robot', )
    assert 'Restart=on-failure' in content
    assert 'RestartSec=3' in content
    assert 'StartLimitIntervalSec=60' in content
    assert 'StartLimitBurst=3' in content


def test_service_without_restart_has_no_start_limit():
    node = {
        'name': 'Navigation',
        'command': 'ros2 launch command_node bringup_all.launch.py',
        'service_name': 'ros2-bringup-all', }
    content = generate_service_file(node=node, machine_type='robot', ros_domain_id=42, project_path='/tmp/project', user='robot', )
    assert 'Restart=no' in content
    assert 'StartLimitIntervalSec' not in content
    assert 'StartLimitBurst' not in content


def test_service_supports_ordered_start_and_stop_hooks():
    node = {
        'name': 'RPLidar',
        'command': 'ros2 launch stretch_core rplidar.launch.py',
        'service_name': 'ros2-rplidar',
        'requires_build': True,
        'packages_to_build': ['stretch_core'],
        'exec_start_pre': ['-/usr/bin/systemctl --user stop hello-robot-lrf-off.service',
            '-/usr/bin/pkill -f /usr/bin/hello_robot_lrf_off.py', ],
        'exec_stop_post': ['-/usr/bin/systemd-run --user --unit=hello-robot-lrf-off.service '
                           '/usr/bin/python3 /usr/bin/hello_robot_lrf_off.py', ], }
    content = generate_service_file(node=node, machine_type='robot', ros_domain_id=42, project_path='/tmp/project', user='robot', )
    build = 'ExecStartPre=/tmp/project/scripts/update_robot_ws.sh --build-only --packages stretch_core'
    stop_guard = 'ExecStartPre=-/usr/bin/systemctl --user stop hello-robot-lrf-off.service'
    kill_guard = 'ExecStartPre=-/usr/bin/pkill -f /usr/bin/hello_robot_lrf_off.py'
    restore_guard = ('ExecStopPost=-/usr/bin/systemd-run --user --unit=hello-robot-lrf-off.service '
                     '/usr/bin/python3 /usr/bin/hello_robot_lrf_off.py')
    assert content.index(build) < content.index(stop_guard) < content.index(kill_guard)
    assert restore_guard in content


def test_generator_rejects_deploy_after_source_changes(monkeypatch):
    monkeypatch.setattr(generate_services, '_IMPORTED_SOURCE_FINGERPRINT', 'stale')
    try:
        ensure_loaded_generator_is_current()
    except RuntimeError as exc:
        assert 'Restart the controller' in str(exc)
    else:
        raise AssertionError('Stale generator source was accepted')


def test_generator_removes_only_stale_service_files(tmp_path):
    stale_service = tmp_path / 'ros2-stale.service'
    unrelated_file = tmp_path / 'notes.txt'
    stale_service.write_text('stale')
    unrelated_file.write_text('keep')
    remove_stale_generated_services(tmp_path)
    assert not stale_service.exists()
    assert unrelated_file.read_text() == 'keep'


def test_real_discovery_config_generates_both_profile_names():
    config_dir = Path(__file__).parent / 'config'
    config = load_discovery_config(config_dir)
    env = build_service_env(config, domain_id=42, project_path='/tmp/project')
    profile = '/tmp/project/controller/config/fastdds_discovery_profiles.xml'
    assert env['FASTRTPS_DEFAULT_PROFILES_FILE'] == profile
    assert env['FASTDDS_DEFAULT_PROFILES_FILE'] == profile
    assert env['ROS_SUPER_CLIENT'] == 'FALSE'
    content = generate_service_file(
        node={'name': 'Vision', 'command': 'ros2 run vision_node vision_node',
              'service_name': 'ros2-vision'},
        machine_type='workstation', ros_domain_id=42, project_path='/tmp/project',
        user='robot', discovery_env=env,
    )
    for variable in ('FASTRTPS_DEFAULT_PROFILES_FILE', 'FASTDDS_DEFAULT_PROFILES_FILE'):
        assert f'Environment="{variable}={profile}"' in content
    assert 'Environment="ROS_SUPER_CLIENT=FALSE"' in content


@pytest.fixture
def fresh_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_services, 'detect_private_discovery_ip', lambda hosts: '192.168.50.50')
    root = tmp_path / 'renamed-checkout'
    shutil.copytree(Path(__file__).parent / 'config', root / 'controller/config',
                    ignore=shutil.ignore_patterns('logs', '*.private.json', '*_db.json*'))
    return root


def test_new_robot_uses_current_checkout_and_its_own_fleet(fresh_checkout):
    services = generate_services.local_service_definitions(
        fresh_checkout, 'robot', fleet_id='stretch-se3-3999', ros_domain_id=43,
        discovery_host='192.168.50.49')
    assert len(services) == 9
    for service in services:
        content = service['content']
        assert f'ExecStart={fresh_checkout}/scripts/update_robot_ws.sh' in content
        assert 'HELLO_FLEET_ID=stretch-se3-3999' in content
        assert '--ros-domain-id 43' in content
        assert 'ROS_DISCOVERY_SERVER=192.168.50.49:30000' in content
        assert f'{fresh_checkout}/controller/config/fastdds_discovery_profiles.xml' in content
        assert 'pg-llm-service-robot' not in content
    assert not (fresh_checkout / 'controller/config/machines.private.json').exists()


def test_workstation_selection_uses_local_machine_domain(fresh_checkout, monkeypatch):
    monkeypatch.setattr(generate_services, 'detect_private_discovery_ip', lambda hosts: '192.168.50.49')
    services = generate_services.local_service_definitions(fresh_checkout, 'workstation')
    for service in services:
        assert f'ExecStart={fresh_checkout}/scripts/update_workstation_ws.sh' in service['content']
        assert '--ros-domain-id 43' in service['content']
        assert 'ROS_DISCOVERY_SERVER=192.168.50.49:30000' in service['content']


def test_local_install_replaces_old_units_without_starting_nodes(fresh_checkout, tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setattr(Path, 'home', lambda: home)
    workspace = fresh_checkout / 'robot/ros_ws/install'
    workspace.mkdir(parents=True)
    (workspace / 'local_setup.bash').touch()
    venv = fresh_checkout / 'robot/.venv/bin'
    venv.mkdir(parents=True)
    (venv / 'python').touch()
    unit_dir = home / '.config/systemd/user'
    unit_dir.mkdir(parents=True)
    old = 'ExecStart=/old-checkout/scripts/update_robot_ws.sh\n'
    (unit_dir / 'ros2-camera.service').write_text(old)
    (unit_dir / 'ros2-obsolete.service').write_text(old)
    (unit_dir / 'unrelated.service').write_text('keep')
    commands = []
    monkeypatch.setattr(subprocess, 'run', lambda command, **kwargs: commands.append(command))
    services = generate_services.local_service_definitions(
        fresh_checkout, 'robot', fleet_id='stretch-se3-3999', discovery_host='192.168.50.50')
    backup = generate_services.install_local_services(fresh_checkout, 'robot', services)
    assert (backup / 'ros2-camera.service').read_text() == old
    assert (backup / 'ros2-obsolete.service').read_text() == old
    assert not (unit_dir / 'ros2-obsolete.service').exists()
    assert (unit_dir / 'unrelated.service').read_text() == 'keep'
    assert str(fresh_checkout) in (unit_dir / 'ros2-camera.service').read_text()
    assert ['systemctl', '--user', 'stop', 'ros2-camera.service', 'ros2-obsolete.service'] in commands
    assert ['systemctl', '--user', 'disable', 'ros2-obsolete.service'] in commands
    assert not any('start' in command or 'enable' in command for command in commands)
    assert commands[-1] == ['systemctl', '--user', 'daemon-reload']
    # Rerunning replaces the same units and preserves a recoverable backup.
    again = generate_services.install_local_services(fresh_checkout, 'robot', services)
    assert again != backup
    assert len(list(unit_dir.glob('ros2-*.service'))) == 9


def test_missing_workspace_cannot_replace_existing_services(fresh_checkout, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: calls.append(a))
    with pytest.raises(RuntimeError, match='Build this checkout'):
        generate_services.install_local_services(fresh_checkout, 'robot', [])
    assert calls == []


def test_invalid_unit_verification_leaves_existing_installation_untouched(fresh_checkout, tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setattr(Path, 'home', lambda: home)
    workspace = fresh_checkout / 'workstation/ament_ws/install'
    workspace.mkdir(parents=True)
    (workspace / 'local_setup.bash').touch()
    venv = fresh_checkout / 'workstation/.venv/bin'
    venv.mkdir(parents=True)
    (venv / 'python').touch()
    unit_dir = home / '.config/systemd/user'
    unit_dir.mkdir(parents=True)
    existing = unit_dir / 'ros2-vision.service'
    existing.write_text('old unit')
    def fail_verify(command, **kwargs):
        assert command[0] == 'systemd-analyze'
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(subprocess, 'run', fail_verify)
    services = generate_services.local_service_definitions(fresh_checkout, 'workstation',
        discovery_host='192.168.50.50')
    with pytest.raises(subprocess.CalledProcessError):
        generate_services.install_local_services(fresh_checkout, 'workstation', services)
    assert existing.read_text() == 'old unit'

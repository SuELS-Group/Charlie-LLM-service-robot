from pathlib import Path

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

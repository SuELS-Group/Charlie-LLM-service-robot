"""Exercise the real driver helper with simulated body/controller feedback."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest
from stretch_core.arm_safety import driver_state_snapshot, run_arm_service, update_runstop_mode
from stretch_core.rwlock import RWLock


def status():
    return {'pimu': {'runstop_event': False}, 'arm': {'pos': .0, 'vel': .0},
            'lift': {'pos': .4, 'vel': .0},
            'base': {'x_vel': .0, 'y_vel': .0, 'theta_vel': .0},
            'head': {'head_pan': {'vel': .0}, 'head_tilt': {'vel': .0}},
            'end_of_arm': {'wrist_yaw': {'pos': 3., 'vel': .0}}}


def node():
    sample = status()
    robot = SimpleNamespace(pimu=SimpleNamespace(status=sample['pimu']),
        head=SimpleNamespace(joints=['head_pan', 'head_tilt']),
        end_of_arm=SimpleNamespace(joints=['wrist_yaw']),
        get_status=lambda: deepcopy(sample), home=Mock(), stow=Mock(),
        is_homed=Mock(return_value=True), enable_collision_mgmt=Mock(),
        get_stow_pos=lambda joint: {'arm': 0., 'lift': .4, 'wrist_yaw': 3.}[joint])
    return SimpleNamespace(robot=robot, robot_mode='navigation',
        robot_mode_rwlock=RWLock(), control_modes=['navigation', 'position', 'trajectory', 'gamepad'],
        _arm_service_lock=threading.Lock(), _arm_stop_generation=0, _arm_service_mode=None,
        _driver_started_at=10., _motion_idle_since=None, streaming_position_activated=False,
        dirty_command=False, joint_trajectory_action=SimpleNamespace(_goal_handle=None),
        _cleanup_before_changing_mode=Mock(), get_logger=lambda: Mock()), sample


@pytest.mark.parametrize('operation', ['home', 'stow'])
def test_active_runstop_never_calls_body_and_new_request_succeeds_after_release(operation):
    n, sample = node()
    sample['pimu']['runstop_event'] = True
    assert run_arm_service(n, operation) == (False, f'Cannot {operation} while in mode=runstopped.')
    getattr(n.robot, operation).assert_not_called()
    sample['pimu']['runstop_event'] = False
    assert run_arm_service(n, operation)[0]
    getattr(n.robot, operation).assert_called_once()
    assert n.robot_mode == 'navigation' and not n._arm_service_lock.locked()


def test_stow_requires_calibration_but_home_can_establish_it():
    n, _ = node()
    n.robot.is_homed.return_value = False
    assert run_arm_service(n, 'stow') == (False, 'Cannot stow: robot is not homed.')
    n.robot.stow.assert_not_called()
    n.robot.home.side_effect = lambda: setattr(n.robot.is_homed, 'return_value', True)
    assert run_arm_service(n, 'home')[0]
    assert run_arm_service(n, 'stow')[0]


@pytest.mark.parametrize('operation', ['home', 'stow'])
def test_body_exception_restores_control_mode_and_releases_busy_flag(operation):
    n, _ = node()
    getattr(n.robot, operation).side_effect = RuntimeError('motor failure')
    ok, message = run_arm_service(n, operation)
    assert not ok and message.startswith('ARM_OPERATION_FAILED:')
    assert n.robot_mode == 'navigation' and not n._arm_service_lock.locked()
    n.robot.enable_collision_mgmt.assert_called_once()


@pytest.mark.parametrize('release_during_call', [False, True])
def test_runstop_during_blocking_call_is_not_reported_as_success(release_during_call):
    n, sample = node()
    def interrupt():
        sample['pimu']['runstop_event'] = True
        assert update_runstop_mode(n, True)
        assert n.prerunstop_mode == 'navigation'
        if release_during_call:
            sample['pimu']['runstop_event'] = False
            assert update_runstop_mode(n, False)
            assert n.robot_mode == 'homing'
    n.robot.home.side_effect = interrupt
    ok, message = run_arm_service(n, 'home')
    assert not ok and message.startswith('ARM_OPERATION_INTERRUPTED:')
    assert n.robot_mode == ('navigation' if release_during_call else 'runstopped')
    if not release_during_call:
        assert n.prerunstop_mode == 'navigation'
    assert n._arm_service_mode is None and not n._arm_service_lock.locked()


def test_home_false_feedback_and_stow_off_target_do_not_claim_success():
    n, sample = node()
    n.robot.is_homed.return_value = False
    assert run_arm_service(n, 'home')[1].startswith('ARM_OPERATION_FAILED:')
    n.robot.is_homed.return_value = True
    sample['arm']['pos'] = .2
    assert run_arm_service(n, 'stow')[1].startswith('ARM_OPERATION_FAILED:')


@pytest.mark.parametrize('busy', ['service', 'goal', 'streaming'])
def test_does_not_overlap_another_controller(busy):
    n, _ = node()
    if busy == 'service': n._arm_service_lock.acquire()
    if busy == 'goal': n.joint_trajectory_action._goal_handle = SimpleNamespace(is_active=True)
    if busy == 'streaming': n.streaming_position_activated = True
    assert run_arm_service(n, 'home')[1].startswith('DRIVER_BUSY:')
    n.robot.home.assert_not_called()


def test_collision_cleanup_failure_does_not_claim_success():
    n, _ = node()
    n.robot.enable_collision_mgmt.side_effect = RuntimeError('failed')
    assert run_arm_service(n, 'home')[1].startswith('ARM_OPERATION_FAILED:')
    assert not n._arm_service_lock.locked() and n.robot_mode == 'navigation'


def test_idle_diagnostic_requires_continuous_observations_and_preserves_timestamp():
    n, sample = node()
    assert not driver_state_snapshot(n, sample, 12.)['motion_idle']
    result = driver_state_snapshot(n, sample, 12.6)
    assert result['motion_idle'] and result['driver_started_at'] == 10.
    assert result['sampled_at'] == 12.6 and result['runstop_active'] is False


@pytest.mark.parametrize('fault', ['missing_velocity', 'nonfinite', 'moving_arm', 'moving_base',
    'moving_head', 'moving_wrist', 'runstop', 'unknown_runstop', 'goal', 'service', 'streaming',
    'dirty_command', 'base_command', 'homing', 'gamepad'])
def test_incomplete_or_active_driver_state_cannot_prove_idle(fault):
    n, sample = node()
    driver_state_snapshot(n, sample, 12.)
    if fault == 'missing_velocity': del sample['lift']['vel']
    if fault == 'nonfinite': sample['arm']['vel'] = float('nan')
    if fault == 'moving_arm': sample['arm']['vel'] = .02
    if fault == 'moving_base': sample['base']['theta_vel'] = .2
    if fault == 'moving_head': sample['head']['head_pan']['vel'] = .2
    if fault == 'moving_wrist': sample['end_of_arm']['wrist_yaw']['vel'] = .2
    if fault == 'runstop': sample['pimu']['runstop_event'] = True
    if fault == 'unknown_runstop': del sample['pimu']['runstop_event']
    if fault == 'goal': n.joint_trajectory_action._goal_handle = SimpleNamespace(is_active=True)
    if fault == 'service': n._arm_service_lock.acquire()
    if fault == 'streaming': n.streaming_position_activated = True
    if fault == 'dirty_command': n.dirty_command = True
    if fault in ('homing', 'gamepad'): n.robot_mode = fault
    assert not driver_state_snapshot(n, sample, 13., base_command_active=fault == 'base_command')['motion_idle']
    assert n._motion_idle_since is None


def test_invalidated_goal_handle_is_busy_instead_of_crashing_telemetry():
    class InvalidGoal:
        @property
        def is_active(self):
            raise RuntimeError('InvalidHandle')
    n, sample = node()
    n.joint_trajectory_action._goal_handle = InvalidGoal()
    assert not driver_state_snapshot(n, sample, 12.)['motion_idle']
    assert run_arm_service(n, 'home')[1].startswith('DRIVER_BUSY:')
    n.robot.home.assert_not_called()


def test_runstop_release_uses_mode_state_at_write_lock_acquisition():
    n, _ = node()
    n.robot_mode = 'runstopped'
    n._arm_service_mode = 'homing'
    n.prerunstop_mode = 'navigation'
    # Simulate service completion just before the release transition obtains
    # exclusive access. A precomputed 'homing' must not overwrite navigation.
    acquire = n.robot_mode_rwlock.acquire_write
    def finish_before_lock():
        n._arm_service_mode = None
        acquire()
    n.robot_mode_rwlock.acquire_write = finish_before_lock
    assert update_runstop_mode(n, False)
    assert n.robot_mode == 'navigation'


def test_stow_feedback_may_settle_briefly_after_body_returns(monkeypatch):
    n, sample = node()
    sample['arm']['pos'] = .03
    monkeypatch.setattr('stretch_core.arm_safety.time.sleep', lambda _: sample['arm'].update(pos=0.))
    assert run_arm_service(n, 'stow')[0]


def test_long_telemetry_gap_restarts_idle_window():
    n, sample = node()
    driver_state_snapshot(n, sample, 12.)
    assert driver_state_snapshot(n, sample, 12.6)['motion_idle']
    assert not driver_state_snapshot(n, sample, 14.)['motion_idle']

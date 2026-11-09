"""Position-mode regressions with simulated motors; never connect to hardware."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from rclpy.duration import Duration
from rclpy.time import Time
from stretch_core.position_goal_safety import GuardedContact


def motor(guarded=True, count=7):
    return {'in_guarded_event': guarded, 'guarded_event': count}


def test_prior_latched_contact_is_not_a_new_event_but_new_counter_is():
    contact = GuardedContact(motor())
    assert not contact.detected(motor())
    assert contact.detected(motor(count=8))


def test_contact_between_samples_is_detected_even_if_flag_already_cleared():
    assert GuardedContact(motor(False)).detected(motor(False, 8))


def test_clearing_then_reasserting_contact_is_not_ignored():
    contact = GuardedContact(motor())
    assert not contact.detected(motor(False))
    assert contact.detected(motor())


@pytest.mark.parametrize('count', [None, -1, True, '7'])
def test_missing_or_invalid_event_counter_does_not_bypass_contact(count):
    assert GuardedContact(motor(count=count)).detected(motor(count=count))


@pytest.fixture
def driver_modules(monkeypatch):
    # Only vendor imports unavailable on a workstation are replaced. The real
    # command-group and trajectory-executor implementations are loaded below.
    for name in ('hello_helpers', 'hello_helpers.hello_misc', 'hello_helpers.gripper_conversion',
                 'hello_helpers.simple_command_group', 'stretch_body', 'stretch_body.hello_utils',
                 'stretch_core.trajectory_components', 'serial'):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    sys.modules['hello_helpers.hello_misc'].bound_ros_command = lambda limits, value, strict: value
    sys.modules['hello_helpers.gripper_conversion'].GripperConversion = object
    sys.modules['hello_helpers.simple_command_group'].SimpleCommandGroup = object
    sys.modules['stretch_body.hello_utils'].create_time_string = lambda: 'offline'
    sys.modules['stretch_core.trajectory_components'].get_trajectory_components = lambda _: {}
    sys.modules['serial'].SerialException = OSError
    import stretch_core
    root = Path(stretch_core.__file__).parent
    loaded = []
    for name in ('command_groups', 'joint_trajectory_server'):
        fullname = 'stretch_core.' + name
        spec = importlib.util.spec_from_file_location(fullname, root / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, fullname, module)
        spec.loader.exec_module(module)
        loaded.append(module)
    return loaded


@pytest.mark.parametrize('axis', ['arm', 'lift'])
def test_real_command_group_owns_contact_since_its_current_command(driver_modules, axis):
    groups, _ = driver_modules
    cls = groups.ArmCommandGroup if axis == 'arm' else groups.LiftCommandGroup
    group = cls.__new__(cls)
    group.active = True
    group.name = 'joint_' + axis
    group.goal = dict(position=.5, velocity=None, acceleration=None, contact_threshold=None)
    group.retracted = False
    group.retracted_offset_m = 0.
    group.is_named_wrist_extension = False
    group.wrist_extension_name = 'wrist_extension'
    group.is_telescoping = False
    robot = SimpleNamespace(**{axis: SimpleNamespace(move_by=Mock())})
    sample = {axis: {'pos': .2, 'motor': motor()}}
    callback = Mock()
    group.init_execution(robot, sample)
    assert group.update_execution(sample, contact_detected_callback=callback) is not True
    callback.assert_not_called()
    sample[axis]['motor']['guarded_event'] += 1
    assert group.update_execution(sample, contact_detected_callback=callback) is True
    callback.assert_called_once()


def position_executor(driver_modules, monkeypatch, tmp_path, outcome):
    _, module = driver_modules
    clock = SimpleNamespace(now=0.)
    monkeypatch.setattr(module, 'time', SimpleNamespace(sleep=lambda delay: setattr(clock, 'now', clock.now + delay)))
    sample = {'arm': {'pos': .0, 'motor': motor()}, 'lift': {'pos': .4, 'motor': motor(False)}}
    pending, pushed = {}, []
    reads = 0
    def push():
        pushed.append(dict(pending))
        for axis, delta in pending.items():
            sample[axis]['pos'] += delta
        pending.clear()
    def status():
        nonlocal reads
        reads += 1
        if reads >= 2:
            if outcome == 'contact':
                sample['arm']['motor']['guarded_event'] = 8
            elif outcome == 'success':
                push()  # Simulate the driver's next timer tick.
        return deepcopy(sample)
    robot = SimpleNamespace(get_status=status, push_command=Mock(side_effect=push),
        arm=SimpleNamespace(move_by=lambda delta, **_: pending.update(arm=delta)),
        lift=SimpleNamespace(move_by=lambda delta, **_: pending.update(lift=delta)),
        end_of_arm=SimpleNamespace(move_by=Mock()), stop_trajectory=Mock())
    node = SimpleNamespace(robot=robot, dirty_command=False, robot_mode='position',
        streaming_position_activated=False, robot_stop_lock=threading.Lock(), stop_the_robot=False,
        robot_mode_rwlock=SimpleNamespace(acquire_read=Mock(), release_read=Mock()),
        get_logger=lambda: Mock(), get_clock=lambda: SimpleNamespace(now=lambda: Time(nanoseconds=int(clock.now * 1e9))),
        default_goal_timeout_duration=Duration(seconds=.05), default_goal_timeout_s=.05,
        fail_out_of_range_goal=True, node_name='offline')
    handle = SimpleNamespace(is_active=True, is_cancel_requested=outcome == 'cancel',
        request=SimpleNamespace(trajectory=SimpleNamespace(joint_names=['wrist_extension', 'joint_lift'],
            points=[SimpleNamespace()])), abort=Mock(), canceled=Mock(), succeed=Mock())
    class Group:
        # Fake motor/goal interface for exercising the real executor lifecycle.
        def __init__(self, axis, target):
            self.axis, self.target, self.error = axis, target, 1.
        def update(self, *args, **kwargs): return True
        def set_goal(self, *args): return True
        def get_num_valid_commands(self): return 1
        def init_execution(self, robot, sample):
            self.contact = GuardedContact(sample[self.axis]['motor'])
            getattr(robot, self.axis).move_by(self.target - sample[self.axis]['pos'])
        def goal_reached(self): return abs(self.error) < .001
        def update_execution(self, sample, contact_detected_callback):
            if self.contact.detected(sample[self.axis]['motor']):
                contact_detected_callback(self.axis + ' contact detected.')
                return True
            self.error = self.target - sample[self.axis]['pos']
            return self.axis, self.error
    server = module.JointTrajectoryAction.__new__(module.JointTrajectoryAction)
    server.node, server.debug_dir = node, tmp_path
    server._goal_handle, server._goal_lock, server.latest_goal_id = handle, threading.Lock(), 1
    server.command_groups = [Group('arm', .01), Group('lift', .97)]
    server.feedback_callback = Mock()
    return server, handle, node, sample, pending, pushed


def test_old_contact_allows_current_goal_to_complete(driver_modules, monkeypatch, tmp_path):
    server, handle, node, sample, _, _ = position_executor(driver_modules, monkeypatch, tmp_path, 'success')
    assert server.execute_cb(handle).error_code == 0
    handle.succeed.assert_called_once()
    assert sample['lift']['pos'] == pytest.approx(.97)
    node.robot.end_of_arm.move_by.assert_not_called()


@pytest.mark.parametrize('outcome,code', [('contact', 100), ('timeout', -4), ('cancel', 0)])
def test_failed_goal_cannot_leave_a_late_lift_command_queued(driver_modules, monkeypatch, tmp_path, outcome, code):
    server, handle, node, sample, pending, pushed = position_executor(driver_modules, monkeypatch, tmp_path, outcome)
    assert server.execute_cb(handle).error_code == code
    node.robot.push_command()  # A later timer tick must not move the failed goal.
    assert sample['lift']['pos'] == .4
    assert not pending and not node.dirty_command
    assert pushed[0] == {'arm': 0., 'lift': 0.}
    node.robot.end_of_arm.move_by.assert_not_called()  # Preserve the held item.
    node.robot_mode_rwlock.release_read.assert_called_once()


def test_superseded_goal_never_stops_its_replacement(driver_modules, monkeypatch, tmp_path):
    server, old, node, _, _, _ = position_executor(driver_modules, monkeypatch, tmp_path, 'contact')
    server._goal_handle = object()
    server._stop_position_goal(old)
    node.robot.push_command.assert_not_called()


def test_stop_failure_is_reported_as_unconfirmed(driver_modules, monkeypatch, tmp_path):
    server, handle, node, _, _, _ = position_executor(driver_modules, monkeypatch, tmp_path, 'contact')
    node.robot.push_command.side_effect = OSError('motor disconnected')
    result = server.execute_cb(handle)
    assert result.error_code == 101 and 'POSITION_STOP_UNCONFIRMED' in result.error_string
    node.robot_mode_rwlock.release_read.assert_called_once()
    assert node.dirty_command


def test_inactive_goal_stop_failure_releases_mode_lock(driver_modules, monkeypatch, tmp_path):
    server, handle, node, _, _, _ = position_executor(driver_modules, monkeypatch, tmp_path, 'contact')
    handle.is_active = False
    node.robot.push_command.side_effect = OSError('motor disconnected')
    assert server.execute_cb(handle).error_code == 101
    node.robot_mode_rwlock.release_read.assert_called_once()


def test_late_cancel_callback_does_not_stop_the_replacement(driver_modules, monkeypatch, tmp_path):
    from rclpy.action import CancelResponse
    server, old, node, _, _, _ = position_executor(driver_modules, monkeypatch, tmp_path, 'contact')
    server._goal_handle = object()
    assert server.cancel_cb(old) == CancelResponse.ACCEPT
    node.robot.push_command.assert_not_called()
    node.robot.end_of_arm.move_by.assert_not_called()


def test_stopping_a_full_arm_goal_holds_commanded_wrists_and_preserves_gripper():
    from stretch_core.position_goal_safety import stop_position_joints
    robot = Mock()
    node = SimpleNamespace(robot=robot, dirty_command=True)
    stop_position_joints(node, ['wrist_extension', 'joint_lift', 'joint_wrist_yaw',
        'joint_wrist_pitch', 'joint_wrist_roll'])
    robot.arm.move_by.assert_called_once_with(0.)
    robot.lift.move_by.assert_called_once_with(0.)
    assert [call.args for call in robot.end_of_arm.move_by.call_args_list] == [
        ('wrist_yaw', 0.), ('wrist_pitch', 0.), ('wrist_roll', 0.)]
    robot.base.translate_by.assert_not_called()
    robot.head.move_by.assert_not_called()
    robot.push_command.assert_called_once()
    assert not node.dirty_command

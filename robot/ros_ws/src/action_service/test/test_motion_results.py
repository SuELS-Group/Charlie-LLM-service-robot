from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from rclpy.task import Future
from action_service.common.motion import MotionHelper
import robot_common.motion as motion_module


@pytest.mark.parametrize('failure', [None, 'retract', 'stop', 'close'])
def test_empty_gripper_closes_only_after_successful_withdrawal(failure):
    order = []
    motion = MotionHelper(SimpleNamespace(log=Mock()), config=SimpleNamespace(
        gripper=SimpleNamespace(joint='finger', closed=-.35)))
    motion.safe_stow_arm = lambda **kw: order.append(('retract', kw)) or failure != 'retract'
    motion._stopped = lambda: failure == 'stop'
    motion.send_full_trajectory = lambda pose, **kw: order.append(('close', pose)) or failure != 'close'
    assert motion.stow_empty_gripper() is (failure is None)
    assert order[0] == ('retract', {'safe_lift_height': .45, 'preserve_gripper': True})
    assert len(order) == (1 if failure in {'retract', 'stop'} else 2)
    if len(order) == 2:
        assert order[1] == ('close', {'finger': -.35})


def completed(value):
    future = Future()
    future.set_result(value)
    return future


def motion_with_handle(status=4, code=0, pending=False):
    result = Future() if pending else completed(SimpleNamespace(status=status, result=SimpleNamespace(error_code=code)))
    handle = SimpleNamespace(accepted=True, get_result_async=lambda: result, cancel_goal_async=Mock())
    node = SimpleNamespace(log=Mock(), _traj_client=SimpleNamespace(send_goal_async=lambda _: completed(handle)))
    motion = MotionHelper(node)
    return motion, handle, result


@pytest.mark.parametrize('status,code,success', [(4,0,True), (6,-4,False), (5,0,False), (4,-4,False)])
def test_completed_trajectory_must_be_successful(status, code, success):
    motion, _, _ = motion_with_handle(status, code)
    assert motion.send_named_trajectory(['joint_lift'], [.7], 1) is success


def test_nonwaiting_goal_is_owned_until_controller_termination():
    motion, handle, result = motion_with_handle(pending=True)
    assert motion.send_named_trajectory(['joint_lift'], [.7], 1, wait=False)
    assert motion.has_pending_motion()
    motion.cancel_active_trajectories()
    handle.cancel_goal_async.assert_called_once()
    assert motion.has_pending_motion()
    result.set_result(SimpleNamespace(status=5))
    assert not motion.has_pending_motion()


def busy_servo_controller(monkeypatch, *, always_reject=False, terminal_status=4):
    """Model the driver's 200 ms admission window after an accepted servo goal."""
    import threading
    clock = SimpleNamespace(now=0.)
    def sleep(seconds):
        clock.now += seconds
    monkeypatch.setattr(motion_module, 'time', SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    active = Future()  # A held object can keep the last close goal unfinished.
    previous = SimpleNamespace(accepted=True, get_result_async=lambda: active, cancel_goal_async=Mock())
    calls = []
    accepted = []
    def send(goal):
        calls.append((clock.now, goal))
        if len(calls) == 1:
            return completed(previous)
        if always_reject or clock.now < .2:
            return completed(SimpleNamespace(accepted=False))
        accepted.append(goal)
        # A new arm-only goal takes over; the gripper command stays untouched.
        if not active.done():
            active.set_result(SimpleNamespace(status=6))
        result = completed(SimpleNamespace(status=terminal_status,
            result=SimpleNamespace(error_code=0 if terminal_status == 4 else -4)))
        return completed(SimpleNamespace(accepted=True, get_result_async=lambda: result, cancel_goal_async=Mock()))
    node = SimpleNamespace(log=Mock(), _action_stop_evt=threading.Event(),
        _traj_client=SimpleNamespace(send_goal_async=send),
        _latest_joint_positions={'joint_lift':.97, 'wrist_extension':.02,
            'joint_wrist_yaw':0., 'joint_wrist_pitch':-.45, 'joint_wrist_roll':0.,
            'joint_gripper_finger_left':.25})
    config = SimpleNamespace(joints=SimpleNamespace(control=list(node._latest_joint_positions)),
        lift=SimpleNamespace(max=.97),arm=SimpleNamespace(min=.01),
        wrist=SimpleNamespace(yaw=SimpleNamespace(stowed=3.),pitch=SimpleNamespace(down=-.45)),
        gripper=SimpleNamespace(joint='joint_gripper_finger_left',open=SimpleNamespace(max=.6)))
    motion = MotionHelper(node, config=config)
    assert motion.send_named_trajectory(['joint_gripper_finger_left'], [-.35], .2, wait=False)
    return motion, previous, calls, accepted, clock


def test_stow_survives_servo_admission_window_without_releasing_or_cancelling_grip(monkeypatch):
    motion, previous, calls, accepted, _ = busy_servo_controller(monkeypatch)
    assert motion.safe_stow_arm(preserve_gripper=True)
    assert len(accepted) == 3  # Each stow stage executes once.
    assert calls[1][0] < .2 <= max(stamp for stamp, goal in calls[1:] if goal is accepted[0])
    assert all('joint_gripper_finger_left' not in goal.trajectory.joint_names for goal in accepted)
    previous.cancel_goal_async.assert_not_called()
    assert not motion.has_pending_motion()


def test_persistent_admission_rejection_is_bounded_and_stops_stowing(monkeypatch):
    motion, previous, calls, accepted, clock = busy_servo_controller(monkeypatch, always_reject=True)
    assert not motion.safe_stow_arm(preserve_gripper=True)
    assert .2 <= clock.now < 1.
    assert len(calls) > 2 and not accepted
    assert motion.has_pending_motion()
    previous.cancel_goal_async.assert_not_called()
    assert motion.last_trajectory_error['code'] == 'TRAJECTORY_REJECTED'


def test_accepted_but_failed_stow_goal_is_not_replayed(monkeypatch):
    motion, _, _, accepted, _ = busy_servo_controller(monkeypatch, terminal_status=6)
    assert not motion.safe_stow_arm(preserve_gripper=True)
    assert len(accepted) == 1
    assert motion.last_trajectory_error['code'] == 'TRAJECTORY_FAILED'


def test_stop_during_admission_wait_prevents_resubmission(monkeypatch):
    motion, _, calls, accepted, _ = busy_servo_controller(monkeypatch)
    motion.sleep = lambda _: motion.node._action_stop_evt.set()
    assert not motion.safe_stow_arm(preserve_gripper=True)
    assert len(calls) == 2 and not accepted


def test_unconfirmed_cancellation_still_blocks_after_the_wait():
    motion, _, _ = motion_with_handle(pending=True)
    assert motion.send_named_trajectory(['joint_lift'], [.7], 1, wait=False)
    motion.cancel_active_trajectories()
    assert not motion.wait_for_motion_completion(timeout_s=0.)
    assert motion.has_pending_motion()


def test_trajectory_timeout_waits_for_cancellation_without_reporting_success(monkeypatch):
    motion, handle, result = motion_with_handle(pending=True)
    handle.cancel_goal_async.side_effect = lambda: result.set_result(SimpleNamespace(status=5))
    monkeypatch.setattr(motion_module, 'time', SimpleNamespace(
        monotonic=Mock(side_effect=[0., 0., 0., 7., 7.]), sleep=Mock()))
    assert not motion.send_named_trajectory(['joint_lift'], [.7], 1.)
    handle.cancel_goal_async.assert_called_once()
    assert not motion.has_pending_motion()


def test_driver_runstop_reason_is_preserved_and_cleared_on_a_new_trajectory():
    motion, _, future = motion_with_handle(status=6, code=-1)
    wrapper = future.result()
    wrapper.result.error_string = 'Cannot execute goals while in mode=runstopped'
    assert not motion.send_named_trajectory(['joint_head_pan'], [0], .5)
    assert motion.last_trajectory_error == {'code': 'RUNSTOP_ACTIVE',
        'message': wrapper.result.error_string, 'controller_error_code': -1, 'action_status': 6}
    wrapper.status = 4
    wrapper.result.error_code = 0
    wrapper.result.error_string = ''
    assert motion.send_named_trajectory(['joint_head_pan'], [0], .5)
    assert motion.last_trajectory_error is None


def test_contact_reason_is_preserved_for_the_action_result():
    motion, _, future = motion_with_handle(status=6, code=100)
    future.result().result.error_string = 'wrist_extension contact detected.'
    assert not motion.send_named_trajectory(['joint_lift'], [.7], 1.)
    assert motion.last_trajectory_error['code'] == 'CONTACT_DETECTED'


def test_failed_driver_stop_keeps_controller_work_uncertain():
    motion, _, future = motion_with_handle(status=6, code=101)
    future.result().result.error_string = 'POSITION_STOP_UNCONFIRMED: motor disconnected'
    assert not motion.send_named_trajectory(['joint_lift'], [.7], 1.)
    assert motion.last_trajectory_error['code'] == 'TRAJECTORY_STOP_UNCONFIRMED'
    assert motion.has_pending_motion()


def test_late_acceptance_after_timeout_is_cancelled(monkeypatch):
    future = Future()
    node = SimpleNamespace(log=Mock(), _traj_client=SimpleNamespace(send_goal_async=lambda _: future))
    motion = MotionHelper(node)
    monkeypatch.setattr(motion_module, 'time', SimpleNamespace(monotonic=Mock(side_effect=[0,6])))
    assert not motion.send_named_trajectory(['joint_lift'], [.7], 0)
    result = Future()
    handle = SimpleNamespace(accepted=True, get_result_async=lambda: result, cancel_goal_async=Mock())
    future.set_result(handle)
    handle.cancel_goal_async.assert_called_once()
    assert motion.has_pending_motion()

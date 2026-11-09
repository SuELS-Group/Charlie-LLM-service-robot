import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from rclpy.task import Future
from sensor_msgs.msg import JointState
from robot_common.config import load
from robot_common.k2_runtime import K2Runtime
from action_interfaces.srv import ExecuteAction
from action_service.action_service_node import ActionServiceNode


def runtime():
    state = K2Runtime(load(Path(__file__).parents[1] / 'config/k2_runtime.yaml'))
    message = JointState()
    message.header.stamp.sec = 100
    message.name = ['wrist_extension', 'joint_lift', 'joint_wrist_yaw', 'joint_wrist_pitch', 'joint_wrist_roll']
    message.position = [.01, .45, 3., -.45, 0.]
    state.update_joints(message)
    return state


def test_base_and_arm_cannot_win_concurrent_admission():
    state = runtime()
    barrier = threading.Barrier(2)
    results = []
    def arm():
        barrier.wait()
        results.append(state.begin_action('arm'))
    def base():
        barrier.wait()
        results.append(bool(state.acquire_base('', 100.1)[0]))
    threads = [threading.Thread(target=arm), threading.Thread(target=base)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert results.count(True) == 1


@pytest.mark.parametrize('fault', ['stale_capture', 'stale_receipt', 'extended_arm', 'nan_lift', 'missing_wrist'])
def test_invalid_measured_posture_blocks_base(fault):
    state = runtime()
    if fault == 'stale_capture': state.joint_captured = 90.
    if fault == 'stale_receipt': state.joint_received = 0.
    if fault == 'extended_arm': state.joints['wrist_extension'] = .1
    if fault == 'nan_lift': state.joints['joint_lift'] = float('nan')
    if fault == 'missing_wrist': del state.joints['joint_wrist_yaw']
    lease, error = state.acquire_base('', 100.1)
    assert lease is None and error


def test_only_owner_can_turn_during_handover_and_only_when_compact():
    state = runtime()
    assert state.begin_action('handoff')
    assert not state.acquire_base('other', 100.1)[0]
    lease, error = state.acquire_base('handoff', 100.1)
    assert lease and not error
    assert not state.release_base('guessed')
    assert state.release_base(lease)
    state.joints['wrist_extension'] = .1
    assert not state.acquire_base('handoff', 100.1)[0]


@pytest.mark.parametrize('extension', [-.000006, 0., .04])
def test_retracted_encoder_noise_does_not_block_base(extension):
    state = runtime()
    state.joints['wrist_extension'] = extension
    assert state.acquire_base('', 100.1)[0]


@pytest.mark.parametrize('extension', [-.001001, .040001])
def test_encoder_tolerance_preserves_extension_limits(extension):
    state = runtime()
    state.joints['wrist_extension'] = extension
    lease, error = state.acquire_base('', 100.1)
    assert lease is None and f'wrist_extension={extension}' in error


@pytest.mark.parametrize('tolerance', [-.0001, .002001, float('nan'), float('inf')])
def test_encoder_tolerance_cannot_disable_posture_limits(tolerance):
    config = load(Path(__file__).parents[1] / 'config/k2_runtime.yaml')
    config.retracted_encoder_tolerance_metres = tolerance
    with pytest.raises(ValueError, match='encoder tolerance'):
        K2Runtime(config)


def test_watchdog_cannot_check_an_arm_move_against_a_released_base_lease():
    state = runtime()
    state.lease = 'turn'
    checking, resume, releasing, released = [threading.Event() for _ in range(4)]
    def clock():
        checking.set()
        assert resume.wait(1.)
        return SimpleNamespace(nanoseconds=100_100_000_000)
    node = SimpleNamespace(k2_runtime=state, _safety_fault_lease='', get_clock=lambda: SimpleNamespace(now=clock),
        log=Mock(), global_stop_pub=Mock())
    def begin_arm():
        releasing.set()
        assert state.release_base('turn')
        with state.lock:
            state.joints['wrist_extension'] = .2
        released.set()
    watcher = threading.Thread(target=ActionServiceNode._k2_watchdog, args=(node,))
    arm = threading.Thread(target=begin_arm)
    watcher.start()
    try:
        assert checking.wait(1.)
        arm.start()
        assert releasing.wait(1.)
        assert not released.wait(.05)
    finally:
        resume.set()
        watcher.join(1.)
        if arm.ident is not None:
            arm.join(1.)
    assert released.is_set()
    node.global_stop_pub.publish.assert_not_called()


@pytest.mark.parametrize('fault', ['extended', 'stale'])
def test_watchdog_still_stops_once_per_active_faulted_lease(fault):
    from builtin_interfaces.msg import Time
    state = runtime()
    state.lease = 'turn'
    if fault == 'extended': state.joints['wrist_extension'] = .2
    if fault == 'stale': state.joint_captured = 90.
    node = SimpleNamespace(k2_runtime=state, _safety_fault_lease='', log=Mock(), global_stop_pub=Mock(),
        get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(
            nanoseconds=100_100_000_000, to_msg=lambda: Time(sec=100))))
    ActionServiceNode._k2_watchdog(node)
    ActionServiceNode._k2_watchdog(node)
    node.global_stop_pub.publish.assert_called_once()


def test_timed_out_internal_turn_retains_late_goal_and_cancels_it(monkeypatch):
    import action_service.common.handoff as module
    from test_take_from_hand_action import _node
    from action_service.take_from_hand_action import TakeFromHandAction
    node = _node()
    future = Future()
    node.turn_relative_client = SimpleNamespace(wait_for_server=lambda **_: True,
                                               send_goal_async=lambda _: future)
    action = TakeFromHandAction(node)
    ticks = iter([0., 0., 31.])
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
    assert not action._turn_base_relative(90.)
    assert action.turn_motion.has_pending_motion()
    terminal = Future()
    handle = SimpleNamespace(accepted=True, cancel_goal_async=Mock(), get_result_async=lambda: terminal)
    future.set_result(handle)
    handle.cancel_goal_async.assert_called_once()
    assert action.turn_motion.has_pending_motion()
    terminal.set_result(SimpleNamespace(status=5))
    assert not action.turn_motion.has_pending_motion()


def test_old_budget_timer_cannot_stop_new_request():
    node = SimpleNamespace(_deadline_lock=threading.Lock(), _request_generation='new',
        _budget_expired=False, _action_stop_evt=threading.Event(), stop_current_action=Mock())
    ActionServiceNode._expire_action_budget(node, 'old')
    node.stop_current_action.assert_not_called()
    ActionServiceNode._expire_action_budget(node, 'new')
    assert node._budget_expired and node._action_stop_evt.is_set()
    node.stop_current_action.assert_called_once()


def test_finalization_does_not_issue_a_second_halt_after_budget_expiry():
    node = SimpleNamespace(_deadline_lock=threading.Lock(), _request_generation='current',
        _budget_expired=False, _action_stop_evt=threading.Event(), stop_current_action=Mock())
    ActionServiceNode._expire_action_budget(node, 'current')
    ActionServiceNode._finish_action_budget(node, 'current', 0.)
    ActionServiceNode._expire_action_budget(node, 'current')
    assert node._budget_expired and node._request_generation == ''
    node.stop_current_action.assert_called_once()


@pytest.mark.parametrize('orientation,mode', [(False, True), (True, False)])
def test_failed_restoration_cannot_report_travel_ready(orientation, mode):
    from test_action_admission import node as basic_node
    node = basic_node()
    node.k2_runtime = runtime()
    node.get_clock = lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=100_100_000_000))
    node.take_from_hand_action = SimpleNamespace(_arm_motion_started=True, _arm_retracted=True,
        _object_secured=True, _orientation_restored=orientation, _navigation_mode_restored=mode)
    response = ExecuteAction.Response()
    response.status.status = 1
    node._action_handlers['take_from_hand'] = Mock(return_value=response)
    result = ActionServiceNode.handle_action(node, SimpleNamespace(action_type='take_from_hand',
        parameters='{"_issued_at":110}'), response)
    assert result.status.status != 1
    assert not json.loads(result.result_data)['travel_ready']

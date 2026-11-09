from types import SimpleNamespace
from unittest.mock import Mock

from action_service.common.motion import MotionHelper


def _config():
    return SimpleNamespace(joints=SimpleNamespace(control=['joint_lift',
        'wrist_extension',
        'joint_wrist_yaw',
        'joint_wrist_pitch',
        'joint_wrist_roll',
        'joint_gripper_finger_left', ]), arm=SimpleNamespace(min=0.01), lift=SimpleNamespace(max=0.97), wrist=SimpleNamespace(yaw=SimpleNamespace(stowed=3.0), pitch=SimpleNamespace(down=-0.45), ), gripper=SimpleNamespace(joint='joint_gripper_finger_left', open=SimpleNamespace(max=0.6), ), )


def _motion():
    node = SimpleNamespace(log=Mock())
    node.log.sub.return_value = Mock()
    motion = MotionHelper(node, config=_config(), log_prefix='test')
    motion.send_full_trajectory = Mock(return_value=True)
    motion.sleep = Mock()
    return motion


def _sent_positions(motion):
    return [call.args[0] for call in motion.send_full_trajectory.call_args_list]


def test_safe_stow_preserves_active_gripper_command():
    motion = _motion()
    assert motion.safe_stow_arm(gripper_pos=-0.35, preserve_gripper=True)
    assert motion.send_full_trajectory.call_count == 3
    assert all('joint_gripper_finger_left' not in positions for positions in _sent_positions(motion))
    assert all(call.kwargs['omit_joints'] == ('joint_gripper_finger_left',) for call in
               motion.send_full_trajectory.call_args_list)


def test_safe_stow_commands_explicit_gripper_position_by_default():
    motion = _motion()
    assert motion.safe_stow_arm(gripper_pos=-0.35)
    assert motion.send_full_trajectory.call_count == 3
    assert all(positions['joint_gripper_finger_left'] == -0.35 for positions in _sent_positions(motion))
    assert all(call.kwargs['omit_joints'] is None for call in motion.send_full_trajectory.call_args_list)


def test_full_trajectory_omits_joint_from_controller_goal():
    motion = _motion()
    motion.send_full_trajectory = (MotionHelper.send_full_trajectory.__get__(motion))
    motion.current_joint_positions = Mock(return_value={joint_name: float(index) for index, joint_name in
        enumerate(motion.config.joints.control)})
    motion.send_named_trajectory = Mock(return_value=True)
    assert motion.send_full_trajectory({'joint_lift': 0.8}, omit_joints=('joint_gripper_finger_left',), )
    sent_joint_names = motion.send_named_trajectory.call_args.args[0]
    assert 'joint_gripper_finger_left' not in sent_joint_names
    assert sent_joint_names == motion.config.joints.control[:-1]


def test_safe_stow_stops_after_failed_stage():
    motion = _motion()
    motion.send_full_trajectory.side_effect = [True, False]
    assert not motion.safe_stow_arm(gripper_pos=-0.35, preserve_gripper=True)

    assert motion.send_full_trajectory.call_count == 2

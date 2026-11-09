"""Contact ownership and stopping for incremental position-mode commands."""


class GuardedContact:
    """Distinguish a previous latched contact from contact in this command.

    Stretch reports both a latched flag and a monotonically changing event
    counter. A new command must still detect another contact even when the old
    flag has not cleared between status samples. Missing counter evidence is
    treated conservatively, using the flag as before.
    """

    def __init__(self, motor_status):
        self.initial_count = motor_status.get('guarded_event')
        self.initially_guarded = bool(motor_status.get('in_guarded_event'))
        self.cleared = not self.initially_guarded

    def detected(self, motor_status):
        guarded = bool(motor_status.get('in_guarded_event'))
        count = motor_status.get('guarded_event')
        valid_counts = all(type(value) is int and value >= 0 for value in (self.initial_count, count))
        if valid_counts and count != self.initial_count:
            return True  # Also catches a contact that cleared between samples.
        if guarded:
            return self.cleared or not valid_counts
        self.cleared = True
        return False


def stop_position_joints(node, joint_names):
    """Replace this goal's pending commands before reporting it terminated.

    The caller owns the current goal and holds robot_stop_lock, shared with the
    driver's push loop. Stop only commanded joints, retaining an unrelated
    gripper close. stop_trajectory alone does not stop position-mode commands.
    """
    names = set(joint_names)
    robot = node.robot
    if names.intersection({'joint_arm', 'wrist_extension', 'joint_arm_l0',
                           'joint_arm_l1', 'joint_arm_l2', 'joint_arm_l3'}):
        robot.arm.move_by(0.)
    if 'joint_lift' in names:
        robot.lift.move_by(0.)
    if names.intersection({'translate_mobile_base', 'rotate_mobile_base', 'joint_mobile_base_translation'}):
        robot.base.translate_by(0.)
        robot.base.rotate_by(0.)
    for joint in ('head_pan', 'head_tilt'):
        if 'joint_' + joint in names:
            robot.head.move_by(joint, 0.)
    for joint in ('wrist_yaw', 'wrist_pitch', 'wrist_roll'):
        if 'joint_' + joint in names:
            robot.end_of_arm.move_by(joint, 0.)
    if names.intersection({'joint_gripper_finger_left', 'joint_gripper_finger_right', 'gripper_aperture'}):
        robot.end_of_arm.move_by('stretch_gripper', 0.)
    node.dirty_command = True
    robot.push_command()
    node.dirty_command = False

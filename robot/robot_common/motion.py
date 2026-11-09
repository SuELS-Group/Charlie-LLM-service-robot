import time
import threading

from control_msgs.action import FollowJointTrajectory
from geometry_msgs.msg import Twist
from robot_common import get_logger
from std_srvs.srv import Trigger
from trajectory_msgs.msg import JointTrajectoryPoint


class MotionHelper:

    def __init__(self, node, config=None, stop_evt=None, joint_positions_getter=None, log_prefix='action'):
        self.node = node
        self.config = config
        self._stop_evt = stop_evt
        self._joint_positions_getter = joint_positions_getter
        self.log_prefix = log_prefix
        node_log = getattr(node, 'log', None)
        self.log = node_log.sub(log_prefix) if node_log is not None else get_logger(log_prefix)
        self._goal_lock = threading.Lock()
        self._goal_generation = 0
        self._active_goals = {}
        self._pending_goals = {}
        self._pending_services = {}
        self._uncertain_motion = False
        self.last_trajectory_error = None
        self.last_goal_handle = None
        self.last_goal_result = None
        self.last_gripper_completion = None

    def _require_config(self):
        if self.config is None:
            raise RuntimeError(f'{self.log_prefix}: motion config is required')
        return self.config

    def _stopped(self):
        shared = getattr(self.node, '_action_stop_evt', None)
        return ((self._stop_evt is not None and self._stop_evt.is_set()) or
                (shared is not None and shared.is_set() is True))

    def sleep(self, seconds: float):
        end = time.monotonic() + float(seconds)
        while time.monotonic() < end:
            if self._stopped():
                return
            time.sleep(0.01)

    def traj_client(self):
        client = getattr(self.node, '_traj_client', None)
        if client is None:
            client = getattr(self.node, 'traj_client', None)
        return client

    def wait_for_traj_server(self, timeout_s: float) -> bool:
        client = self.traj_client()
        if client is None:
            return False
        t0 = time.monotonic()
        while (time.monotonic() - t0) < float(timeout_s):
            if self._stopped():
                return False
            try:
                if client.wait_for_server(timeout_sec=0.5):
                    return True
            except Exception:
                pass
        return False

    def call_trigger(self, client, timeout_s: float, warn_name=None, allow_when_stopped: bool = False) -> bool:
        if client is None:
            return False
        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            if self._stopped() and not allow_when_stopped:
                return False
            try:
                if client.wait_for_service(timeout_sec=0.2):
                    break
            except Exception:
                pass
            time.sleep(0.03)
        try:
            if not client.service_is_ready():
                return False
        except Exception:
            return False
        if time.monotonic() >= deadline or (self._stopped() and not allow_when_stopped):
            return False
        try:
            future = client.call_async(Trigger.Request())
            with self._goal_lock:
                self._pending_services[id(future)] = future
            future.add_done_callback(self._track_trigger_result)
        except Exception as exc:
            self._uncertain_motion = True
            if warn_name:
                self.log.warning(f'Failed to call {warn_name}: {exc}')
            return False
        while time.monotonic() < deadline:
            if self._stopped() and not allow_when_stopped:
                return False
            if future.done():
                try:
                    result = future.result()
                    return getattr(result, 'success', None) is True
                except Exception:
                    return False
            time.sleep(0.02)
        return False

    def _track_trigger_result(self, future):
        try:
            result = future.result()
            if not isinstance(getattr(result, 'success', None), bool):
                self._uncertain_motion = True
        except Exception:
            self._uncertain_motion = True
        finally:
            with self._goal_lock:
                self._pending_services.pop(id(future), None)

    def switch_to_position_mode(self, timeout_s: float = 3.0) -> bool:
        return self.call_trigger(getattr(self.node, 'pos_mode_client', None), timeout_s, 'position mode')

    def switch_to_navigation_mode(self, timeout_s: float = 3.0) -> bool:
        return self.call_trigger(getattr(self.node, 'nav_mode_client', None), timeout_s, 'navigation mode', allow_when_stopped=True)

    def current_joint_positions(self):
        if self._joint_positions_getter is not None:
            try:
                return dict(self._joint_positions_getter())
            except Exception:
                return {}
        joint_positions = getattr(self.node, '_latest_joint_positions', None)
        if joint_positions:
            return dict(joint_positions)
        grab_action = getattr(self.node, 'grab_item_action', None)
        if grab_action is not None:
            return dict(getattr(grab_action, 'current_joint_pos', {}))
        return {}

    def send_named_trajectory(self, joint_names, positions, duration: float, wait: bool = True) -> bool:
        self.last_trajectory_error = None
        if self._stopped():
            return False
        client = self.traj_client()
        if client is None:
            return False
        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = list(joint_names)
        point = JointTrajectoryPoint()
        point.positions = [float(value) for value in positions]
        point.time_from_start.sec = int(duration)
        point.time_from_start.nanosec = int((duration % 1.0) * 1e9)
        goal.trajectory.points = [point]
        try:
            with self._goal_lock:
                generation = self._goal_generation
                follows_owned_motion = bool(self._active_goals or self._pending_goals)
            # The position driver rejects goals for 200 ms after accepting an
            # active goal. A blocking step immediately after servoing can hit
            # this window. Retry only explicit rejections: accepted/uncertain
            # work must never be replayed. Do not cancel an object-holding close
            # goal merely to get the following arm-only stow admitted.
            retry_deadline = time.monotonic() + .5 if wait and follows_owned_motion else None
            rejections = 0
            while True:
                if self._stopped():
                    self.cancel_active_trajectories()
                    return False
                with self._goal_lock:
                    if generation != self._goal_generation:
                        return False
                if rejections and (retry_deadline is None or time.monotonic() >= retry_deadline):
                    if wait:
                        self.log.warning(self.last_trajectory_error['message'])
                    return False
                send_future = client.send_goal_async(goal)
                with self._goal_lock:
                    self._pending_goals[id(send_future)] = send_future
                send_future.add_done_callback(lambda future: self._track_accepted_goal(future, generation))
                t0 = time.monotonic()
                while (time.monotonic() - t0) < (float(duration) + 5.0):
                    if self._stopped():
                        self.cancel_active_trajectories()
                        return False
                    if send_future.done():
                        break
                    self.sleep(0.02)
                if not send_future.done():
                    self.cancel_active_trajectories()
                    return False
                goal_handle = send_future.result()
                if goal_handle is None:
                    return False
                if goal_handle.accepted:
                    self.last_trajectory_error = None
                    break
                rejections += 1
                self.last_trajectory_error = {
                    'code': 'TRAJECTORY_REJECTED',
                    'message': 'Trajectory was rejected by the controller before execution'}
                if retry_deadline is not None:
                    self.sleep(.05)
            if rejections:
                self.log.info(f'Trajectory accepted after {rejections} admission retries')
            self.last_goal_handle = goal_handle
            self.last_goal_result = goal_handle.get_result_async()
            if not wait:
                return True  # Accepted; intentionally does not claim completion.
            result_future = goal_handle.get_result_async()
            t1 = time.monotonic()
            while (time.monotonic() - t1) < (float(duration) + 5.0):
                if self._stopped():
                    self.cancel_active_trajectories()
                    return False
                if result_future.done():
                    return self._trajectory_succeeded(result_future)
                self.sleep(0.02)
            self.cancel_active_trajectories()
            self.wait_for_motion_completion(timeout_s=1.5)
            return False
        except Exception:
            self._uncertain_motion = True
            return False

    def _trajectory_succeeded(self, future):
        from action_msgs.msg import GoalStatus
        try:
            wrapper = future.result()
            if (wrapper.status == GoalStatus.STATUS_SUCCEEDED and
                    wrapper.result.error_code == FollowJointTrajectory.Result.SUCCESSFUL):
                return True
            message = str(getattr(wrapper.result, 'error_string', '') or 'Trajectory did not complete successfully')
            stop_unconfirmed = wrapper.result.error_code == 101 and 'POSITION_STOP_UNCONFIRMED' in message
            if stop_unconfirmed:
                self._uncertain_motion = True
            self.last_trajectory_error = {
                'code': ('TRAJECTORY_STOP_UNCONFIRMED' if stop_unconfirmed else
                    'RUNSTOP_ACTIVE' if 'mode=runstopped' in message else
                    'CONTACT_DETECTED' if wrapper.result.error_code == 100 else 'TRAJECTORY_FAILED'),
                'message': message, 'action_status': wrapper.status,
                'controller_error_code': wrapper.result.error_code}
            self.log.warning(f'Trajectory failed: {message}')
            return False
        except Exception as exc:
            self.last_trajectory_error = {'code': 'TRAJECTORY_RESULT_UNAVAILABLE', 'message': str(exc)}
            return False

    def _track_accepted_goal(self, future, generation):
        try:
            handle = future.result()
            if handle is None:
                raise ValueError('Goal acceptance response is unavailable')
            if not handle.accepted:
                with self._goal_lock:
                    self._pending_goals.pop(id(future), None)
                return
            with self._goal_lock:
                self._active_goals[id(handle)] = handle
                self._pending_goals.pop(id(future), None)
                cancelled = generation != self._goal_generation or self._stopped()
            if cancelled:
                handle.cancel_goal_async()
            result = handle.get_result_async()
            result.add_done_callback(lambda completed: self._forget_terminal_goal(handle, completed))
        except Exception as exc:
            with self._goal_lock:
                self._uncertain_motion = True
                self._pending_goals.pop(id(future), None)
            self.log.warning(f'{self.log_prefix}: could not track trajectory: {exc}')

    def track_goal_future(self, future):
        """Retain ownership of an internal base goal, including late acceptance."""
        with self._goal_lock:
            generation = self._goal_generation
            self._pending_goals[id(future)] = future
        future.add_done_callback(lambda completed: self._track_accepted_goal(completed, generation))

    def finish_gripper_close(self, timeout_s=5., *, contact_check=None, cancel_timeout_s=1.5):
        """Wait for completion or sustained contact, then settle owned work.

        A timer-driven cancellation alone is never evidence of a successful
        pickup. A contact indication still leaves possession an assumption.
        """
        self.last_gripper_completion = None
        handle, result = self.last_goal_handle, self.last_goal_result
        if handle is None or result is None or self._stopped():
            return False
        cancellation = None
        contact = False
        try:
            deadline = time.monotonic() + float(timeout_s)
            while not result.done() and not self._stopped():
                if contact_check is not None and contact_check():
                    contact = True
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(.02)
            if not result.done():
                cancellation = handle.cancel_goal_async()
            deadline = time.monotonic() + float(cancel_timeout_s)
            while (not result.done() or (cancellation is not None and not cancellation.done())) and time.monotonic() < deadline and not self._stopped():
                time.sleep(.01)
            if not result.done() or self._stopped():
                return False
            wrapper = result.result()
            succeeded = wrapper.status == 4 and wrapper.result.error_code == FollowJointTrajectory.Result.SUCCESSFUL
            cancelled = (wrapper.status == 5 and cancellation is not None and cancellation.done() and
                cancellation.result().return_code == 0 and bool(cancellation.result().goals_canceling))
            self.last_gripper_completion = {'gripper_position_reached': succeeded,
                'gripper_goal_settled': True, 'gripper_goal_cancelled_after_close': cancelled,
                'gripper_contact_indicated': contact}
            if not (succeeded or (cancelled and contact)):
                self._trajectory_succeeded(result)  # Retain the actual driver error.
                return False
            return True
        except Exception:
            return False

    def _forget_terminal_goal(self, handle, future):
        from action_msgs.msg import GoalStatus
        try:
            if future.result().status in {GoalStatus.STATUS_SUCCEEDED, GoalStatus.STATUS_CANCELED, GoalStatus.STATUS_ABORTED}:
                with self._goal_lock:
                    self._active_goals.pop(id(handle), None)
        except Exception:
            pass  # Keep unknown controller work blocked until its state is known.

    def cancel_active_trajectories(self):
        with self._goal_lock:
            self._goal_generation += 1
            goals = list(self._active_goals.values())
        for handle in goals:
            try:
                handle.cancel_goal_async()
            except Exception as exc:
                self.log.warning(f'{self.log_prefix}: trajectory cancellation failed: {exc}')

    def has_pending_motion(self):
        with self._goal_lock:
            return bool(self._active_goals or self._pending_goals or self._pending_services or self._uncertain_motion)

    def wait_for_motion_completion(self, timeout_s=1.5):
        """Wait for owned work to terminate, including cancellation results.

        This never sends a new command and can also finish cleanup after a stop.
        A cancellation request alone does not establish controller completion.
        """
        deadline = time.monotonic() + float(timeout_s)
        while self.has_pending_motion():
            if time.monotonic() >= deadline:
                return False
            time.sleep(.01)
        return True

    def send_full_trajectory(self, positions, duration: float = 2.0, wait: bool = True, omit_joints=None) -> bool:
        if self._stopped():
            return False
        config = self._require_config()
        joint_positions = self.current_joint_positions()
        omitted = set(omit_joints or ())
        controlled_joints = [joint_name for joint_name in config.joints.control if joint_name not in omitted]
        full = {}
        missing = []
        for joint_name in controlled_joints:
            if joint_name in positions:
                full[joint_name] = float(positions[joint_name])
            elif joint_name in joint_positions:
                full[joint_name] = float(joint_positions[joint_name])
            else:
                missing.append(joint_name)
        if missing:
            self.log.warning(f'{self.log_prefix}: missing joints {missing}, cannot send trajectory')
            return False
        ordered = [full[joint_name] for joint_name in controlled_joints]
        return self.send_named_trajectory(controlled_joints, ordered, duration, wait=wait)

    def open_gripper(self, duration_s: float = 1.2, wait: bool = True) -> bool:
        config = self._require_config()
        return self.send_named_trajectory([config.gripper.joint], [float(config.gripper.open.max)], float(duration_s), wait=wait, )

    def move_head(self, pan: float = None, tilt: float = None, duration: float = 1.5, wait: bool = True) -> bool:
        if self._stopped():
            return False
        names = []
        positions = []
        if pan is not None:
            names.append('joint_head_pan')
            positions.append(float(pan))
        if tilt is not None:
            names.append('joint_head_tilt')
            positions.append(float(tilt))
        if not names:
            return True
        client = self.traj_client()
        if client is None:
            return False
        try:
            if not client.wait_for_server(timeout_sec=1.0):
                return False
        except Exception:
            return False
        return self.send_named_trajectory(names, positions, float(duration), wait=wait)

    def publish_base_velocity(self, linear: float = 0.0, angular: float = 0.0) -> bool:
        vel_pub = getattr(self.node, '_vel_pub', None)
        if vel_pub is None:
            return False
        msg = Twist()
        msg.linear.x = float(linear)
        msg.angular.z = float(angular)
        try:
            vel_pub.publish(msg)
            return True
        except Exception:
            return False

    def stop_base(self, num_commands: int = 5, delay: float = 0.02):
        for i in range(int(num_commands)):
            self.publish_base_velocity(0.0, 0.0)
            if i < int(num_commands) - 1 and delay > 0:
                self.sleep(delay)

    def rotate_base(self, angle_rad: float, angular_vel: float = 0.25):
        if self._stopped():
            self.stop_base()
            return
        if not self.publish_base_velocity(0.0, 0.0):
            self.log.warning(f'{self.log_prefix}: no velocity publisher, cannot rotate base')
            return
        direction = 1.0 if float(angle_rad) >= 0.0 else -1.0
        duration = abs(float(angle_rad)) / max(float(angular_vel), 1e-6)
        end = time.monotonic() + duration
        while time.monotonic() < end:
            if self._stopped():
                break
            self.publish_base_velocity(0.0, direction * float(angular_vel))
            time.sleep(0.05)
        self.stop_base()
        self.sleep(0.3)

    def stow_empty_gripper(self, safe_lift_height: float = 0.45):
        """Withdraw from the person/surface before closing an emptied gripper.

        Call only after confirmed opening/release. Never use this for retained
        objects, uncertain releases, or stop cleanup.
        """
        if not self.safe_stow_arm(safe_lift_height=safe_lift_height, preserve_gripper=True):
            return False
        if self._stopped():
            return False
        config = self._require_config()
        return self.send_full_trajectory({config.gripper.joint: float(config.gripper.closed)},
            duration=1.2, wait=True)

    def safe_stow_arm(self, gripper_pos=None, safe_lift_height: float = 0.45, preserve_gripper: bool = False):
        """Stow the arm in stages while optionally preserving the active grip.

        When ``preserve_gripper`` is true, the gripper is omitted from each
        trajectory. This keeps its previous close command active without making
        an object-blocked finger position part of the arm trajectory's success
        condition.
        """
        if self._stopped():
            return False
        config = self._require_config()
        if gripper_pos is None:
            gripper_pos = config.gripper.open.max
        omitted_joints = (config.gripper.joint,) if preserve_gripper else None

        def stow_positions(positions):
            if not preserve_gripper:
                positions[config.gripper.joint] = float(gripper_pos)
            return positions

        if not self.send_full_trajectory(stow_positions({
            'joint_lift': config.lift.max,
            'wrist_extension': config.arm.min, }), duration=2.5, wait=True, omit_joints=omitted_joints, ):
            self.log.warning(f'{self.log_prefix}: failed to lift and retract while stowing')
            return False
        if self._stopped():
            return False
        self.sleep(0.2)
        if not self.send_full_trajectory(stow_positions({
            'joint_lift': config.lift.max,
            'wrist_extension': config.arm.min,
            'joint_wrist_yaw': config.wrist.yaw.stowed,
            'joint_wrist_pitch': config.wrist.pitch.down,
            'joint_wrist_roll': 0.0, }), duration=2.5, wait=True, omit_joints=omitted_joints, ):
            self.log.warning(f'{self.log_prefix}: failed to rotate wrist while stowing')
            return False
        if self._stopped():
            return False
        self.sleep(0.2)
        if not self.send_full_trajectory(stow_positions({
            'joint_lift': float(safe_lift_height),
            'wrist_extension': config.arm.min,
            'joint_wrist_yaw': config.wrist.yaw.stowed,
            'joint_wrist_pitch': config.wrist.pitch.down,
            'joint_wrist_roll': 0.0, }), duration=2.5, wait=True, omit_joints=omitted_joints, ):
            self.log.warning(f'{self.log_prefix}: failed to lower lift while stowing')
            return False
        self.sleep(0.3)
        return not self._stopped()

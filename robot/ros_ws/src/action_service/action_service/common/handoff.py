import json
import math
import threading
import time
import uuid

from std_msgs.msg import String

from action_service.common.motion import MotionHelper
from command_interfaces.action import TurnTo as TurnToAction
from command_interfaces.action import TurnToRelative as TurnToRelativeAction
from command_interfaces.srv import GetPosition
from tool_status.msg import ToolStatus as RosToolStatus


class HandoffActionBase:

    def __init__(self, node, config, *, log_prefix: str, action_key: str):
        self.node = node
        self.config = config
        self.action_key = action_key
        # The approach owns person selection. Handoff only needs a fresh
        # gesture for this session, never a face/body recheck after moving.
        self._recipient_policy = 'gesture_only'
        self.log = node.log.sub(log_prefix)
        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self.latest_gesture = None
        self._base_turned_for_handoff = False
        self._base_turn_attempted = False
        self._original_base_heading = None
        self._head_moved_for_handoff = False
        self._request_time = 0.0
        self._gesture_session = ''
        self._armed_at = 0.
        self._recipient_track = None
        self._recipient_name = None
        self._orientation_restored = True
        self._navigation_mode_restored = True
        self._handoff_motion_started = False
        self._recipient_failure_details = {}
        self._recipient_failure_reason = None
        self._selected_recipient_name = None
        self._recipient_visible_count = None
        self._gesture_diagnostic = {}
        self.motion = MotionHelper(node, config=config, stop_evt=self._stop_evt, log_prefix=log_prefix)
        self.turn_motion = MotionHelper(node, stop_evt=self._stop_evt, log_prefix=log_prefix + '_turn')

    def handle_gesture(self, msg: String):
        try:
            payload = json.loads(msg.data)
        except (json.JSONDecodeError, AttributeError):
            return
        if not isinstance(payload, dict):
            return
        try:
            stamp = float(payload['stamp'])
            confidence = float(payload['confidence'])
            if (not self._gesture_session or payload.get('session_id') != self._gesture_session or
                    payload.get('action') != self.action_key or not self._gesture_recipient_matches(payload) or
                    not math.isfinite(stamp) or not self._armed_at <= stamp <= self._clock_seconds() or
                    self._clock_seconds() - stamp > float(self.config.handoff.gesture_max_age_seconds) or
                    not math.isfinite(confidence) or not 0 <= confidence <= 1 or
                    int(payload.get('stable_frames', 0)) < 3):
                return
            gesture = {
                'session_id': payload['session_id'], 'track_id': payload.get('track_id'), 'stamp': stamp,
                'recipient_policy': payload.get('recipient_policy'),
                'gesture': str(payload.get('gesture') or ''),
                'confidence': float(payload.get('confidence', 0.0)),
                'stable_frames': int(payload.get('stable_frames', 0)),
                'received_monotonic': time.monotonic(), }
        except (KeyError, TypeError, ValueError, OverflowError):
            return
        with self._lock:
            if self._gesture_session == gesture['session_id'] and self._gesture_recipient_matches(gesture):
                self.latest_gesture = gesture

    def _gesture_recipient_matches(self, payload):
        return payload.get('recipient_policy') == self._recipient_policy

    def _clock_seconds(self):
        return self.node.get_clock().now().nanoseconds / 1e9

    def handle_diagnostic(self, msg):
        try:
            data=json.loads(msg.data)
            stamp=float(data['stamp'])
            with self._lock:
                if (not self._gesture_session or data.get('session_id') != self._gesture_session or
                        data.get('action') != self.action_key or not math.isfinite(stamp) or
                        not self._armed_at <= stamp <= self._clock_seconds() or self._clock_seconds()-stamp > 2.):
                    return
                self._gesture_diagnostic={key:data[key] for key in ('reason','stamp','stable_frames',
                    'neutral_frames','release_latched','image_age_seconds','person_age_seconds','recipient_distance_m') if key in data}
        except (ValueError,TypeError,KeyError,AttributeError):
            return

    def _begin_handoff(self, parameters=None):
        self._stop_evt.clear()
        if getattr(self.node, '_action_stop_evt', threading.Event()).is_set():
            self._stop_evt.set()
        self._request_time = getattr(self.node, 'current_request_time', 0.0)
        self._base_turned_for_handoff = False
        self._base_turn_attempted = False
        self._original_base_heading = None
        self._head_moved_for_handoff = False
        self._orientation_restored = False
        self._navigation_mode_restored = False
        self._gesture_session = ''
        self._armed_at = 0.
        params = json.loads(parameters or '{}') if isinstance(parameters, str) else (parameters or {})
        self._recipient_name = params.get('person')  # Approach diagnostic only.
        self._recipient_track = None
        self._handoff_motion_started = False
        self._recipient_failure_details = {}
        self._recipient_failure_reason = None
        self._selected_recipient_name = None
        self._recipient_visible_count = None
        self._gesture_diagnostic = {}
        self._set_gesture_detection('disarm', wait_for_subscriber=False)
        self._clear_gesture()

    def _clear_gesture(self):
        with self._lock:
            self.latest_gesture = None

    def _set_gesture_detection(self, command: str, *, wait_for_subscriber: bool = True) -> bool:
        publisher = getattr(self.node, 'handoff_gesture_control_pub', None)
        if publisher is None:
            self.log.warning(f'{self.action_key}: gesture control publisher is unavailable')
            return False
        if wait_for_subscriber:
            deadline = time.monotonic() + 2.0
            while (
                    not self._stop_evt.is_set() and time.monotonic() < deadline and publisher.get_subscription_count() < 1):
                time.sleep(0.05)
            if publisher.get_subscription_count() < 1:
                self.log.warning(f'{self.action_key}: gesture detector is unavailable')
                return False
        payload = {'command': str(command), 'action': self.action_key,
                   'session_id': self._gesture_session, 'armed_at': self._armed_at,
                   'track_id': self._recipient_track,
                   'recipient_policy': self._recipient_policy}
        publisher.publish(String(data=json.dumps(payload)))
        return True

    def _arm_gesture_detection(self) -> bool:
        with self._lock:
            self._gesture_session = uuid.uuid4().hex
            self._armed_at = self._clock_seconds()
            self.latest_gesture = None
        return self._set_gesture_detection('arm')

    def _disarm_gesture_detection(self):
        with self._lock:
            self._set_gesture_detection('disarm', wait_for_subscriber=False)
            self._gesture_session = ''

    def _timeout_from_parameters(self, parameters) -> float:
        value = float(self.config.handoff.timeout_seconds)
        params = {}
        if isinstance(parameters, dict):
            params = parameters
        elif isinstance(parameters, str) and parameters.strip():
            try:
                params = json.loads(parameters)
            except json.JSONDecodeError:
                self.log.warning(f'{self.action_key}: failed to parse parameters JSON: '
                                 f'{parameters}')
        try:
            value = float(params.get('timeout_seconds', value))
        except (TypeError, ValueError):
            pass
        if not math.isfinite(value):
            value = float(self.config.handoff.timeout_seconds)
        return min(60.0, max(1.0, value))

    def _wait_for_joints(self, timeout_s: float) -> bool:
        need = set(self.config.joints.control)
        t0 = time.monotonic()
        while (not self._stop_evt.is_set() and (time.monotonic() - t0) < float(timeout_s)):
            have = set(getattr(self.node, '_latest_joint_positions', {}).keys())
            if need.issubset(have):
                return True
            time.sleep(0.02)
        return False

    def _aim_head_for_handoff(self) -> bool:
        head = self.config.handoff.head
        self.log.info(f'{self.action_key}: aiming head for handoff '
                      f'pan={float(head.pan):.2f}, tilt={float(head.tilt):.2f}')
        return self.motion.move_head(pan=float(head.pan), tilt=float(head.tilt), duration=float(head.move_duration), wait=True, )

    def _turn_base_for_handoff(self) -> bool:
        original_heading = self._get_base_heading()
        if original_heading is None:
            self.log.warning(
                f'{self.action_key}: refusing handoff turn because the '
                'original base heading is unavailable')
            return False
        self._original_base_heading = original_heading
        # Set this before sending the relative turn: an aborted Spin can still
        # have moved the base and must therefore be restored.
        self._base_turn_attempted = True
        angle = float(self.config.handoff.base.rotation_degrees)
        self.log.info(
            f'{self.action_key}: turning base {angle:.1f} degrees for handoff '
            f'from captured heading {original_heading:.1f} degrees')
        return self._turn_base_relative(angle)

    def _get_base_heading(self):
        client = getattr(self.node, 'get_position_client', None)
        if client is None:
            self.log.warning(
                f'{self.action_key}: get-position service client is unavailable')
            return None
        timeout_s = float(getattr(
            self.config.handoff.base, 'position_timeout_seconds', 5.0))
        deadline = time.monotonic() + timeout_s
        while not self._stop_evt.is_set() and time.monotonic() < deadline:
            try:
                if client.wait_for_service(timeout_sec=0.5):
                    break
            except Exception:
                pass
        else:
            self.log.warning(
                f'{self.action_key}: get-position service is unavailable')
            return None
        try:
            future = client.call_async(GetPosition.Request())
        except Exception as exc:
            self.log.warning(
                f'{self.action_key}: failed to request current heading: {exc}')
            return None
        while not self._stop_evt.is_set() and time.monotonic() < deadline:
            if future.done():
                try:
                    response = future.result()
                    status = getattr(
                        getattr(response, 'status', None), 'status', None)
                    if status == RosToolStatus.STATUS_SUCCESS:
                        return float(response.yaw_deg)
                    message = getattr(
                        getattr(response, 'status', None), 'message', '')
                    self.log.warning(
                        f'{self.action_key}: current heading request failed: '
                        f'{message or status}')
                except Exception as exc:
                    self.log.warning(
                        f'{self.action_key}: current heading response failed: '
                        f'{exc}')
                return None
            time.sleep(0.02)
        if not self._stop_evt.is_set():
            self.log.warning(
                f'{self.action_key}: current heading request timed out after '
                f'{timeout_s:.1f}s')
        return None

    def _turn_base_relative(self, angle_degrees: float) -> bool:
        client = getattr(self.node, 'turn_relative_client', None)
        if client is None:
            self.log.warning(f'{self.action_key}: relative-turn action client is unavailable')
            return False
        goal = TurnToRelativeAction.Goal()
        goal.angle = float(angle_degrees)
        goal.issued_at = float(self._request_time)
        goal.workflow_id = getattr(self.node, 'current_workflow_id', '')
        goal.request_tool = getattr(self.node, 'current_request_tool', self.action_key)
        return self._execute_turn_goal(
            client, goal, action_label='relative-turn')

    def _turn_base_absolute(self, heading_degrees: float) -> bool:
        client = getattr(self.node, 'turn_absolute_client', None)
        if client is None:
            self.log.warning(
                f'{self.action_key}: absolute-turn action client is unavailable')
            return False
        goal = TurnToAction.Goal()
        goal.orientation = float(heading_degrees)
        goal.issued_at = float(self._request_time)
        goal.workflow_id = getattr(self.node, 'current_workflow_id', '')
        goal.request_tool = getattr(self.node, 'current_request_tool', self.action_key)
        return self._execute_turn_goal(
            client, goal, action_label='absolute-turn')

    def _execute_turn_goal(self, client, goal, *, action_label: str) -> bool:
        timeout_s = min(30., float(self.config.handoff.base.turn_timeout_seconds))
        deadline = time.monotonic() + timeout_s
        while not self._stop_evt.is_set() and time.monotonic() < deadline:
            try:
                if client.wait_for_server(timeout_sec=0.5):
                    break
            except Exception:
                pass
        else:
            self.log.warning(
                f'{self.action_key}: {action_label} action server is unavailable')
            return False

        try:
            send_future = client.send_goal_async(goal)
            self.turn_motion.track_goal_future(send_future)
        except Exception as exc:
            self.log.warning(
                f'{self.action_key}: failed to send {action_label} goal: {exc}')
            return False

        goal_handle = None
        while not self._stop_evt.is_set() and time.monotonic() < deadline:
            if send_future.done():
                try:
                    goal_handle = send_future.result()
                except Exception as exc:
                    self.log.warning(
                        f'{self.action_key}: {action_label} goal failed before '
                        f'acceptance: {exc}')
                    return False
                break
            time.sleep(0.02)
        if goal_handle is None or not goal_handle.accepted:
            self.turn_motion.cancel_active_trajectories()
            self.log.warning(
                f'{self.action_key}: {action_label} goal was not accepted')
            return False

        result_future = goal_handle.get_result_async()
        while not self._stop_evt.is_set() and time.monotonic() < deadline:
            if result_future.done():
                try:
                    wrapper = result_future.result()
                    result = wrapper.result
                    status = getattr(getattr(result, 'status', None), 'status', None)
                    details = json.loads(getattr(result, 'result_data', '') or '{}')
                    if (wrapper.status == 4 and status == RosToolStatus.STATUS_SUCCESS and
                            details.get('completion_confirmed') is True and not details.get('execution_uncertain')):
                        return True
                    message = getattr(getattr(result, 'status', None), 'message', '')
                    self.log.warning(
                        f'{self.action_key}: {action_label} action failed: '
                        f'{message or status}')
                except Exception as exc:
                    self.log.warning(
                        f'{self.action_key}: {action_label} result failed: {exc}')
                return False
            time.sleep(0.02)

        try:
            self.turn_motion.cancel_active_trajectories()
        except Exception:
            pass
        if not self._stop_evt.is_set():
            self.log.warning(
                f'{self.action_key}: {action_label} action timed out after '
                f'{timeout_s:.1f}s')
        return False

    def _restore_handoff_orientation(self):
        if self._stop_evt.is_set():
            return False

        if self._base_turn_attempted:
            if self.motion.switch_to_navigation_mode(timeout_s=3.0):
                self.log.info(
                    f'{self.action_key}: restoring captured base heading '
                    f'{self._original_base_heading:.1f} degrees')
                if self._turn_base_absolute(self._original_base_heading):
                    self._base_turn_attempted = False
                    self._base_turned_for_handoff = False
                else:
                    self.log.warning(f'{self.action_key}: unable to restore base orientation')
            else:
                self.log.warning(f'{self.action_key}: unable to enter navigation mode for '
                                 'base restore')

        if self._head_moved_for_handoff:
            head = self.config.handoff.head.restore
            self.log.info(f'{self.action_key}: restoring head camera pose')
            if not self.motion.switch_to_position_mode(timeout_s=3.0):
                self.log.warning(f'{self.action_key}: unable to enter position mode for '
                                 'head camera restore')
            elif self.motion.move_head(pan=float(head.pan), tilt=float(head.tilt), duration=float(head.move_duration), wait=True):
                self._head_moved_for_handoff = False
            else:
                self.log.warning(f'{self.action_key}: unable to restore head camera pose')
        return not self._base_turn_attempted and not self._head_moved_for_handoff

    def _send_handoff_pose(self, gripper_position: float = None, preserve_gripper: bool = False) -> bool:
        pose = self.config.handoff.pose
        positions = {
            'joint_lift': float(pose.joint_lift),
            'wrist_extension': float(pose.wrist_extension),
            'joint_wrist_yaw': float(pose.joint_wrist_yaw),
            'joint_wrist_pitch': float(pose.joint_wrist_pitch),
            'joint_wrist_roll': float(pose.joint_wrist_roll), }
        omitted_joints = None
        if preserve_gripper:
            omitted_joints = (self.config.gripper.joint,)
        else:
            positions[self.config.gripper.joint] = float(gripper_position)
        return self.motion.send_full_trajectory(positions, duration=float(self.config.durations.pose_seconds), wait=True, omit_joints=omitted_joints)

    def _wait_for_gesture(self, timeout_s: float):
        deadline = time.monotonic() + float(timeout_s)
        required = str(self.config.handoff.required_gesture)
        min_conf = float(self.config.handoff.min_confidence)
        max_age = float(self.config.handoff.gesture_max_age_seconds)
        reminder_at = time.monotonic() + 15.
        reminded = False
        while not self._stop_evt.is_set() and time.monotonic() < deadline:
            with self._lock:
                gesture = (dict(self.latest_gesture) if self.latest_gesture else None)
            if gesture is not None:
                age = time.monotonic() - float(gesture.get('received_monotonic', 0.0))
                if (
                        gesture.get('session_id') == self._gesture_session and
                        self._gesture_recipient_matches(gesture) and
                        self._armed_at <= gesture.get('stamp', 0.) <= self._clock_seconds() and
                        self._clock_seconds() - gesture.get('stamp', 0.) <= max_age and
                        gesture.get('gesture') == required and float(gesture.get('confidence', 0.0)) >= min_conf and
                        0 <= age <= max_age):
                    self._disarm_gesture_detection()
                    return gesture
            if not reminded and time.monotonic() >= reminder_at:
                self.node.announce_handoff_ready(self.action_key, reminder=True)
                reminded = True
            time.sleep(0.05)
        return None

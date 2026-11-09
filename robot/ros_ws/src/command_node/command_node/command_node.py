import json
import math
import threading
import time
from pathlib import Path

import rclpy
import tf2_ros
import yaml
from action_msgs.msg import GoalStatus, GoalStatusArray
from command_node.navigation_readiness import occupancy_grid_is_usable
from command_node.turning import normalize_heading_degrees
from control_msgs.action import FollowJointTrajectory
from geometry_msgs.msg import Point32, PointStamped, Polygon, Pose, PoseStamped
from nav2_msgs.action import NavigateToPose
from nav_msgs.msg import OccupancyGrid
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.duration import Duration
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from robot_common import get_config, get_logger
from robot_common.stop_control import issued_before_stop, stop_timestamp
from robot_common.motion import MotionHelper
from robot_common.study_policy import operation_mode
from std_msgs.msg import String
from std_srvs.srv import Trigger
from tf2_geometry_msgs import do_transform_point
from tf2_ros import TransformException
from trajectory_msgs.msg import JointTrajectoryPoint

from command_interfaces.action import MoveTo as MoveToAction, MoveToWaypoint as MoveToWaypointAction, PositionForAction as PositionForActionAction, TurnTo as TurnToAction, TurnToRelative as TurnToRelativeAction
from command_interfaces.srv import GetPosition, StartFollowing, StopFollowing
from global_stop.msg import GlobalStop
from tool_status.msg import ToolStatus as RosToolStatus
from waypoint_interfaces.srv import GetWaypoint
from action_interfaces.srv import ExecuteAction

config = get_config('command_node', Path(__file__).stem)


class CommandNode(Node):

    def __init__(self):
        super().__init__('command_node')
        self.log = get_logger()
        self.operation_mode = operation_mode()
        self.service_callback_group = ReentrantCallbackGroup()
        self.llm_info_publisher_ = self.create_publisher(String, config.topics.llm_info, config.qos.default_depth)
        self.goal_publisher_ = self.create_publisher(PoseStamped, config.topics.goal_pose, config.qos.default_depth)
        self.footprint_pub_local = self.create_publisher(Polygon, config.topics.local_footprint, config.qos.default_depth)
        self.footprint_pub_global = self.create_publisher(Polygon, config.topics.global_footprint, config.qos.default_depth)
        # Subscribe before any navigation request. Slam Toolbox only rasterizes
        # its pose graph while it has a matched /map subscriber, and Nav2 may
        # otherwise wait indefinitely if its static layer misses startup.
        map_qos = QoSProfile(depth=config.qos.map_depth, reliability=QoSReliabilityPolicy.RELIABLE, history=QoSHistoryPolicy.KEEP_LAST, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.navigation_map_ready = False
        self.navigation_map_subscription = self.create_subscription(OccupancyGrid, config.topics.map, self._navigation_map_callback, map_qos)
        self.get_waypoint_client = self.create_client(GetWaypoint, config.services.waypoint_get, callback_group=self.service_callback_group)
        self.get_position_service = self.create_service(GetPosition, config.services.get_position, self.get_position_callback, callback_group=self.service_callback_group)
        self.start_following_service = self.create_service(StartFollowing, config.services.follow_start, self.start_following_callback, callback_group=self.service_callback_group)
        self.stop_following_service = self.create_service(StopFollowing, config.services.follow_stop, self.stop_following_callback, callback_group=self.service_callback_group)
        self.i = 0
        self.tf_buffer = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)
        self.base_frame = config.frames.base
        self.arm_root_frame = config.frames.arm_root
        self.debug_footprint = ''
        self.footprint_timer = self.create_timer(config.timers.footprint_period_seconds, self.update_dynamic_footprint)
        self.current_pose = None
        self._pose_received = 0.
        self.pose_timer = self.create_timer(config.timers.pose_update_period_seconds, self.update_pose_from_tf)
        self.follow_mode_active = False
        self.follow_target = None
        self.follow_distance = config.follow.distance
        self.follow_update_threshold = config.follow.update_threshold
        self.last_follow_position = None
        self.latest_global_stop = None
        self._last_stop_time = 0.0
        self._driver_stop_confirmed = True
        self._driver_stop_generation = 0
        self.driver_stop_client = self.create_client(Trigger, '/stop_the_robot', callback_group=self.service_callback_group)
        self.base_lease_client = self.create_client(ExecuteAction, '/k2/base_lease', callback_group=self.service_callback_group)
        self._base_lease_id = ''
        self._base_completion_uncertain = False
        self._base_terminal_confirmed = True
        self._lease_cleanup_pending = False
        self._lease_request_pending = False
        self._lease_request_uncertain = False
        self._lease_cleanup_retry_at = 0.
        self._navigation_deadline = None
        self._navigation_budget_expired = False
        self._navigation_abort_reason = ''
        self.navigation_watchdog = self.create_timer(.1, self._navigation_watchdog, callback_group=self.service_callback_group)
        qos_det = QoSProfile(depth=config.qos.best_effort_depth, reliability=QoSReliabilityPolicy.BEST_EFFORT, history=QoSHistoryPolicy.KEEP_LAST, )
        self.depth_detections_subscription = self.create_subscription(String, config.topics.depth_detections, self.depth_detections_callback, qos_det)
        self.current_detections = {}
        self.follow_timer = self.create_timer(config.timers.follow_period_seconds, self.follow_mode_update)
        self.use_nav2_action = config.navigation.use_nav2_action
        self.log.info(f'Command node ready. feedback={config.topics.llm_info}, nav_action={self.use_nav2_action}')
        self.nav_to_pose_client = ActionClient(self, NavigateToPose, config.actions.navigate_to_pose)
        self.move_to_action_server = self._create_navigation_action_server(MoveToAction, config.actions.move_to, self.execute_move_to_action)
        self.turn_to_action_server = self._create_navigation_action_server(TurnToAction, config.actions.turn_to, self.execute_turn_to_action)
        self.turn_to_relative_action_server = self._create_navigation_action_server(TurnToRelativeAction, config.actions.turn_to_relative, self.execute_turn_to_relative_action)
        self.move_to_waypoint_action_server = self._create_navigation_action_server(MoveToWaypointAction, config.actions.move_to_waypoint, self.execute_move_to_waypoint_action)
        self.position_for_action_action_server = self._create_navigation_action_server(PositionForActionAction, config.actions.position_for_action, self.execute_position_for_action_action)
        self.current_nav_goal_handle = None
        self.current_nav_action_name = None
        self.current_nav_target_name = None
        self._nav_goal_generation = 0
        self._nav_wait_lock = threading.Lock()
        self._active_nav_action_goal = None
        self._active_nav2_action_goal = None
        self._nav_action_stop_requested = False
        self.trajectory_client = ActionClient(self, FollowJointTrajectory, config.actions.follow_joint_trajectory,
            callback_group=self.service_callback_group)
        self._traj_client = self.trajectory_client
        self._head_stop_evt = threading.Event()
        self._head_motion = MotionHelper(self, stop_evt=self._head_stop_evt, log_prefix='navigation_head')
        self._native_head_control = False
        self.nav_head_pan = config.navigation.head_nav_pan
        self.nav_head_tilt = config.navigation.head_nav_tilt
        self.default_head_pan = config.navigation.head_default_pan
        self.default_head_tilt = config.navigation.head_default_tilt
        self.head_in_nav_position = False
        goal_pose_qos = QoSProfile(reliability=QoSReliabilityPolicy.RELIABLE, history=QoSHistoryPolicy.KEEP_LAST, depth=config.qos.goal_pose_depth, durability=DurabilityPolicy.VOLATILE)
        self.goal_pose_subscription = self.create_subscription(PoseStamped, config.topics.goal_pose, self._external_goal_callback, goal_pose_qos)
        self.log.info(f'Navigation head control listening on {config.topics.goal_pose}')
        nav_status_qos = QoSProfile(reliability=QoSReliabilityPolicy.BEST_EFFORT, history=QoSHistoryPolicy.KEEP_LAST, depth=config.qos.nav_status_depth)
        self.nav_status_subscription = self.create_subscription(GoalStatusArray, config.topics.nav_status, self._nav_status_callback, nav_status_qos)
        self.last_nav_status = None
        stop_qos = QoSProfile(reliability=QoSReliabilityPolicy.RELIABLE, history=QoSHistoryPolicy.KEEP_LAST, depth=config.qos.global_stop_depth, durability=DurabilityPolicy.VOLATILE)
        self.global_stop_subscription = self.create_subscription(GlobalStop, config.topics.global_stop,
            self.global_stop_callback, stop_qos, callback_group=self.service_callback_group)

    def _navigation_map_callback(self, msg: OccupancyGrid):
        if not occupancy_grid_is_usable(msg):
            self.log.warning('Ignoring unusable navigation map with dimensions '
                             f'{msg.info.width}x{msg.info.height} and '
                             f'{len(msg.data)} cells')
            return
        if not self.navigation_map_ready:
            self.log.info(f'Navigation map ready: {msg.info.width}x{msg.info.height} '
                          f'at {msg.info.resolution:.3f}m/cell')
        self.navigation_map_ready = True

    def global_stop_callback(self, msg: GlobalStop):
        with self._nav_wait_lock:
            self.latest_global_stop = msg
            self._last_stop_time = max(self._last_stop_time, stop_timestamp(msg))
            self._head_stop_evt.set()
            self._driver_stop_generation += 1
            self._nav_goal_generation += 1
            generation = self._driver_stop_generation
            self._driver_stop_confirmed = False
        try:
            if self.driver_stop_client.service_is_ready():
                future = self.driver_stop_client.call_async(Trigger.Request())
                future.add_done_callback(lambda result: self._driver_stop_result(result, generation))
            else:
                self.log.error('Driver stop service is unavailable; use the operator/hardware stop')
        except Exception as exc:
            self.log.error(f'Could not request driver stop: {exc}')
        self._head_motion.cancel_active_trajectories()
        self.log.warning(f'GLOBAL STOP received - stopping all tasks: {self.describe_global_stop(msg)}')
        if self.follow_mode_active:
            self.log.info('Stopping follow mode...')
            self.follow_mode_active = False
            self.follow_target = None
            self.last_follow_position = None
        with self._nav_wait_lock:
            self._nav_action_stop_requested = self._active_nav_action_goal is not None
            active_nav2_goal = self._active_nav2_action_goal
        if active_nav2_goal is not None:
            self.log.info('Cancelling active navigation action goal...')
            try:
                active_nav2_goal.cancel_goal_async()
            except Exception as e:
                self.log.warning(f'Failed to cancel navigation action goal: {e}')
        if self.current_nav_action_name is not None:
            action_name = self.current_nav_action_name
            target_name = self.current_nav_target_name
            if self.current_nav_goal_handle is not None:
                self.log.info('Cancelling current navigation goal...')
                try:
                    self.current_nav_goal_handle.cancel_goal_async()
                except Exception as e:
                    self.log.warning(f'Failed to cancel navigation goal: {e}')
            self._publish_action_completion(RosToolStatus.STATUS_STOPPED, self.format_global_stop_message('Navigation stopped'), action_name=action_name, target_name=target_name, )
            self.current_nav_goal_handle = None
            self.current_nav_action_name = None
            self.current_nav_target_name = None
        feedback = String()
        feedback.data = json.dumps({
            'type': 'global_stop',
            'status': 'stopped',
            'message': self.format_global_stop_message('Global stop requested')})
        self.llm_info_publisher_.publish(feedback)

    def _driver_stop_result(self, future, generation):
        try:
            confirmed = getattr(future.result(), 'success', None) is True
        except Exception:
            confirmed = False
        with self._nav_wait_lock:
            if generation == self._driver_stop_generation:
                self._driver_stop_confirmed = confirmed
        if not confirmed:
            self.log.error('Driver stop was not acknowledged; use the operator/hardware stop')

    def _external_goal_callback(self, msg: PoseStamped):
        if self.operation_mode == 'study':
            return
        if self._active_nav_action_goal is not None:
            return
        self._native_head_control = False
        self.log.info(f'Received goal_pose: x={msg.pose.position.x:.2f}, y={msg.pose.position.y:.2f}')
        if not self.head_in_nav_position:
            self.log.info('Tilting head down for obstacle detection')
            self._move_head_for_navigation()
        else:
            self.log.info('Head already in navigation position')

    def _nav_status_callback(self, msg: GoalStatusArray):
        if self._native_head_control:
            return
        if not msg.status_list:
            return
        current_status = msg.status_list[-1].status
        terminal_states = [GoalStatus.STATUS_SUCCEEDED, GoalStatus.STATUS_CANCELED, GoalStatus.STATUS_ABORTED]
        active_states = [GoalStatus.STATUS_ACCEPTED, GoalStatus.STATUS_EXECUTING]
        if not self.head_in_nav_position and current_status in active_states:
            if self.last_nav_status is None or self.last_nav_status in terminal_states or self.last_nav_status == GoalStatus.STATUS_UNKNOWN:
                self.log.info(f'Navigation started (status={current_status}), tilting head for obstacle detection')
                self._move_head_for_navigation()
        if self.head_in_nav_position and self.last_nav_status is not None:
            was_active = self.last_nav_status in active_states
            if was_active and current_status in terminal_states:
                self.log.info(f'Navigation ended (status={current_status}), resetting head')
                self._reset_head_after_navigation()
        self.last_nav_status = current_status

    def update_pose_from_tf(self):
        try:
            t = self.tf_buffer.lookup_transform('map', self.base_frame, rclpy.time.Time())
            p = Pose()
            p.position.x = t.transform.translation.x
            p.position.y = t.transform.translation.y
            p.position.z = t.transform.translation.z
            p.orientation = t.transform.rotation
            stamp = t.header.stamp.sec + t.header.stamp.nanosec * 1e-9
            now = self.get_clock().now().nanoseconds / 1e9
            if not 0 <= now - stamp <= 1.:
                return
            self.current_pose = p
            self._pose_received = time.monotonic()
        except TransformException:
            pass

    def _set_follow_mode(self, target: str, distance: float):
        if self.operation_mode == 'study':
            return False, 'Following is not enabled in the study profile'
        self.latest_global_stop = None
        follow_target = target.strip().lower()
        if not follow_target:
            return False, "Target name is required (e.g., 'person')"
        self.follow_mode_active = True
        self.follow_target = follow_target
        self.follow_distance = float(distance)
        self.last_follow_position = None
        self.log.info(f'Follow mode activated for {follow_target!r} at distance {self.follow_distance}m')
        return True, f'Now following {follow_target}'

    def _stop_follow_mode(self):
        was_active = self.follow_mode_active
        self.follow_mode_active = False
        self.follow_target = None
        self.last_follow_position = None
        if was_active:
            self.log.info('Follow mode deactivated')
            return True, 'Stopped following', True
        self.log.info('Follow mode was not active')
        return True, 'Follow mode was not active', False

    def start_following_callback(self, request, response):
        success, message = self._set_follow_mode(request.target, request.distance)
        self._set_response_status(response, RosToolStatus.STATUS_SUCCESS if success else RosToolStatus.STATUS_FAILURE, message)
        response.target = self.follow_target or ''
        response.distance = self.follow_distance if success else 0.0
        status = 'ok' if success else 'error'
        self._publish_llm_feedback({
            'function': 'start_following()',
            'status': status,
            'message': message,
            'target': response.target,
            'distance': response.distance})
        return response

    def stop_following_callback(self, _request, response):
        success, message, was_active = self._stop_follow_mode()
        self._set_response_status(response, RosToolStatus.STATUS_SUCCESS if success else RosToolStatus.STATUS_FAILURE, message)
        response.was_active = was_active
        self._publish_llm_feedback({
            'function': 'stop_following()',
            'status': 'ok',
            'message': message,
            'was_active': was_active})
        return response

    def _publish_llm_feedback(self, payload):
        feedback = String()
        feedback.data = json.dumps(payload)
        self.llm_info_publisher_.publish(feedback)

    def _wait_for_waypoint_service(self, client, service_name):
        if client.service_is_ready():
            return True
        if client.wait_for_service(timeout_sec=1.0):
            return True
        self.log.warning(f'Waypoint service {service_name} not available.')
        return False

    def _get_waypoint(self, name):
        if not self._wait_for_waypoint_service(self.get_waypoint_client, config.services.waypoint_get):
            return None, 'Waypoint service unavailable'
        request = GetWaypoint.Request()
        request.name = name
        try:
            response = self.get_waypoint_client.call(request)
        except Exception as exc:
            self.log.error(f'Waypoint lookup failed for {name!r}: {exc}')
            return None, str(exc)
        if response is None:
            return None, 'Waypoint lookup failed'
        if response.status.status != RosToolStatus.STATUS_SUCCESS:
            self.log.warning(f'Waypoint lookup failed for {name!r}: {response.status.message}')
            return None, response.status.message
        return response.waypoint, response.status.message

    def _create_navigation_action_server(self, action_type, action_name: str, execute_callback):
        return ActionServer(self, action_type, action_name, execute_callback=execute_callback, goal_callback=self.navigation_goal_callback, cancel_callback=self.navigation_cancel_callback, callback_group=self.service_callback_group, )

    def navigation_goal_callback(self, _goal_request):
        if issued_before_stop(getattr(_goal_request, 'issued_at', 0.0), self._last_stop_time):
            return GoalResponse.REJECT
        if not self.use_nav2_action:
            self.log.warning('Rejected navigation action goal because Nav2 action mode is disabled')
            return GoalResponse.REJECT
        with self._nav_wait_lock:
            if (not self._driver_stop_confirmed or self._head_motion.has_pending_motion() or self._base_lease_id or
                    self._lease_request_pending or self._lease_request_uncertain):
                self.log.warning('Rejected navigation while driver stop/head termination is unconfirmed')
                return GoalResponse.REJECT
            if self._active_nav_action_goal is not None or self.current_nav_goal_handle is not None:
                self.log.warning('Rejected navigation action goal while navigation is already active')
                return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def navigation_cancel_callback(self, goal_handle):
        with self._nav_wait_lock:
            current_goal = self._active_nav_action_goal
            nav2_goal = self._active_nav2_action_goal if current_goal == goal_handle else None
        if nav2_goal is not None:
            self.log.info('Cancel requested for navigation action goal')
            try:
                nav2_goal.cancel_goal_async()
            except Exception as e:
                self.log.warning(f'Failed to cancel Nav2 goal for navigation action: {e}')
        return CancelResponse.ACCEPT

    async def _execute_navigation_action(self, goal_handle, *, action_name: str, target_name: str, goal_x: float, goal_y: float, goal_orientation: float, setup_success: bool, setup_message: str, feedback_type, result_builder):
        if not setup_success:
            message = setup_message
            self.log.warning(f'Navigation action setup failed: {message}')
            goal_handle.abort()
            return result_builder(RosToolStatus.STATUS_FAILURE, message)
        if not self.navigation_map_ready:
            message = ('Navigation map unavailable; localization has not published '
                       f'a usable map on {config.topics.map}')
            self.log.warning(f'[{action_name}] {message}')
            goal_handle.abort()
            return result_builder(RosToolStatus.STATUS_FAILURE, message)
        with self._nav_wait_lock:
            if issued_before_stop(getattr(goal_handle.request, 'issued_at', 0.0), self._last_stop_time):
                goal_handle.abort()
                return result_builder(RosToolStatus.STATUS_STOPPED, 'Command was issued before the global stop')
            if (not self._driver_stop_confirmed or self._head_motion.has_pending_motion() or
                    self._lease_request_pending or self._lease_request_uncertain or self._base_lease_id or
                    self._active_nav_action_goal is not None):
                goal_handle.abort()
                return result_builder(RosToolStatus.STATUS_FAILURE, 'Previous navigation/head work has not terminated')
            self.latest_global_stop = None
            self._head_stop_evt.clear()
            self._native_head_control = True
            self._active_nav_action_goal = goal_handle
            self._active_nav2_action_goal = None
            self._nav_action_stop_requested = False
        base_terminal = True
        try:
            lease, error = self._acquire_base_lease(goal_handle.request, action_name,
                target=target_name, goal=[goal_x, goal_y, goal_orientation])
            if not lease:
                goal_handle.abort()
                result = result_builder(RosToolStatus.STATUS_FAILURE, error)
                if str(error).startswith('Transit posture not confirmed:'):
                    details = json.loads(result.result_data or '{}')
                    details.update({'phase': 'transit_posture', 'base_started': False,
                        'posture_error': error})
                    result.result_data = json.dumps(details)
                return result
            self._base_lease_id = lease
            self._navigation_budget_expired = False
            self._navigation_abort_reason = ''
            self._navigation_deadline = time.monotonic() + (30. if action_name in {'turn_to', 'turn_to_relative'} else 120.)
            head_ready = (self._reset_head_after_navigation() if action_name in {'turn_to', 'turn_to_relative'}
                          else self._move_head_for_navigation())
            if not head_ready:
                self._await_controller_cleanup()
                goal_handle.abort()
                status = RosToolStatus.STATUS_STOPPED if self._head_stop_evt.is_set() else RosToolStatus.STATUS_FAILURE
                controller_error = getattr(self._head_motion, 'last_trajectory_error', None)
                controller_error = controller_error if isinstance(controller_error, dict) else {}
                message = ('Head preparation stopped' if self._head_stop_evt.is_set()
                    else 'Head position could not be confirmed')
                if controller_error.get('message'):
                    message += ': ' + controller_error['message']
                result = result_builder(status, message)
                details = json.loads(result.result_data)
                details.update({'phase': 'head_preparation', 'base_started': False,
                    'controller_error': controller_error})
                result.result_data = json.dumps(details)
                return result
            if not self.nav_to_pose_client.wait_for_server(timeout_sec=config.navigation.navigate_wait_seconds):
                message = 'NavigateToPose action server not available'
                message = self._restore_head_after_navigation_failure(goal_handle, message, base_terminal=base_terminal)
                self.log.warning(message)
                goal_handle.abort()
                return result_builder(RosToolStatus.STATUS_FAILURE, message)
            with self._nav_wait_lock:
                stop_requested = self._nav_action_stop_requested
            if goal_handle.is_cancel_requested or stop_requested or self.latest_global_stop is not None:
                message = self.format_global_stop_message('Navigation stopped')
                goal_handle.abort()
                self._publish_action_completion(RosToolStatus.STATUS_STOPPED, message, action_name=action_name, target_name=target_name)
                return result_builder(RosToolStatus.STATUS_STOPPED, message)
            pose_msg = self._build_goal_pose(goal_x, goal_y, goal_orientation)
            nav_goal = NavigateToPose.Goal()
            nav_goal.pose = pose_msg
            self.log.info(f'[{action_name}] Sending navigation action goal: x={goal_x:.2f}, y={goal_y:.2f}, '
                          f'yaw={goal_orientation:.1f}°')
            self._publish_navigation_feedback(goal_handle, feedback_type, 'starting', 'Navigation starting')
            base_terminal = False
            self._base_completion_uncertain = True
            nav_goal_handle = await self.nav_to_pose_client.send_goal_async(nav_goal)
            if not nav_goal_handle.accepted:
                base_terminal = True
                self._base_completion_uncertain = False
                message = 'Navigation goal was rejected by Nav2'
                message = self._restore_head_after_navigation_failure(goal_handle, message, base_terminal=base_terminal)
                self.log.warning(f'[{action_name}] {message}')
                goal_handle.abort()
                return result_builder(RosToolStatus.STATUS_FAILURE, message)
            with self._nav_wait_lock:
                if self._active_nav_action_goal == goal_handle:
                    self._active_nav2_action_goal = nav_goal_handle
                stop_requested = self._nav_action_stop_requested
            self._publish_action_started(action_name, target_name)
            self._publish_navigation_feedback(goal_handle, feedback_type, 'executing', 'Navigation in progress')
            if goal_handle.is_cancel_requested or stop_requested:
                try:
                    nav_goal_handle.cancel_goal_async()
                except Exception as e:
                    self.log.warning(f'Failed to cancel Nav2 goal after stop request: {e}')
            nav_result = await nav_goal_handle.get_result_async()
            base_terminal = nav_result.status in {GoalStatus.STATUS_SUCCEEDED, GoalStatus.STATUS_CANCELED, GoalStatus.STATUS_ABORTED}
            self._base_completion_uncertain = not base_terminal
            if self._head_stop_evt.is_set():
                self._await_controller_cleanup()
            status_code, message = self._map_nav2_result(nav_result.status, default_stop_message=self.format_global_stop_message('Navigation stopped'), )
            if self._navigation_budget_expired:
                status_code = RosToolStatus.STATUS_FAILURE if self._navigation_abort_reason == 'localization' else RosToolStatus.STATUS_TIMEOUT
                message = ('Localization is no longer current; stop requested' if self._navigation_abort_reason == 'localization'
                           else 'Navigation time budget expired; stop requested')
            if status_code == RosToolStatus.STATUS_SUCCESS and action_name not in {'turn_to', 'turn_to_relative'}:
                if not self._reset_head_after_navigation():
                    status_code = RosToolStatus.STATUS_STOPPED if self._head_stop_evt.is_set() else RosToolStatus.STATUS_FAILURE
                    message = 'Base reached the destination, but head restoration was not confirmed'
            elif nav_result.status == GoalStatus.STATUS_ABORTED:
                message = self._restore_head_after_navigation_failure(goal_handle, message, base_terminal=base_terminal)
            if status_code == RosToolStatus.STATUS_SUCCESS:
                goal_handle.succeed()
            elif status_code == RosToolStatus.STATUS_STOPPED:
                if goal_handle.is_cancel_requested:
                    goal_handle.canceled()
                else:
                    goal_handle.abort()
            else:
                goal_handle.abort()
            self._publish_action_completion(status_code, message, action_name=action_name, target_name=target_name)
            result = result_builder(status_code, message)
            details = json.loads(result.result_data)
            details['base_reached'] = nav_result.status == GoalStatus.STATUS_SUCCEEDED
            result.result_data = json.dumps(details)
            return result
        except Exception as e:
            message = f'Navigation action execution failed: {e}'
            self.log.exception(message)
            goal_handle.abort()
            return result_builder(RosToolStatus.STATUS_FAILURE, message)
        finally:
            self._navigation_deadline = None
            self._base_terminal_confirmed = base_terminal
            if (self._base_lease_id and base_terminal and self._driver_stop_confirmed and not self._head_motion.has_pending_motion()):
                if self._release_base_lease(self._base_lease_id):
                    self._base_lease_id = ''
            with self._nav_wait_lock:
                if self._active_nav_action_goal == goal_handle:
                    self._active_nav_action_goal = None
                    self._active_nav2_action_goal = None
                    self._nav_action_stop_requested = False

    def _lease_service_call(self, operation, params):
        deadline = time.monotonic() + 2.
        if not self.base_lease_client.wait_for_service(timeout_sec=1.):
            return None, 'Motion coordinator is unavailable; base movement was not started'
        request = ExecuteAction.Request()
        request.action_type, request.parameters = operation, json.dumps(params)
        if operation == 'acquire':
            with self._nav_wait_lock:
                if self._lease_request_pending or self._lease_request_uncertain or self._base_lease_id:
                    return None, 'Previous motion coordination is still unresolved'
                self._lease_request_pending = True
        try:
            future = self.base_lease_client.call_async(request)
            while not future.done() and time.monotonic() < deadline:
                time.sleep(.01)
            if not future.done():
                if operation == 'acquire':
                    future.add_done_callback(self._late_lease_acquisition)
                return None, 'Motion coordination timed out; base movement was not started'
            response = future.result()
            details = json.loads(response.result_data or '{}')
            if not isinstance(details, dict):
                raise ValueError('Invalid motion coordination result')
            if operation == 'acquire':
                if response.status.status == RosToolStatus.STATUS_SUCCESS and (not isinstance(details.get('lease_id'), str) or not details['lease_id']):
                    raise ValueError('Motion ownership identifier is unavailable')
                with self._nav_wait_lock:
                    self._lease_request_pending = False
            if response.status.status != RosToolStatus.STATUS_SUCCESS:
                return None, response.status.message
            return details, None
        except Exception as exc:
            if operation == 'acquire':
                with self._nav_wait_lock:
                    self._lease_request_pending = False
                    self._lease_request_uncertain = True
            return None, f'Motion coordination failed: {exc}'

    def _late_lease_acquisition(self, future):
        try:
            response = future.result()
            if response.status.status == RosToolStatus.STATUS_SUCCESS:
                details = json.loads(response.result_data or '{}')
                lease = details.get('lease_id') if isinstance(details, dict) else None
                if not isinstance(lease, str) or not lease:
                    raise ValueError('Late motion ownership identifier is unavailable')
                with self._nav_wait_lock:
                    if self._base_lease_id and self._base_lease_id != lease:
                        raise ValueError('Conflicting late motion ownership')
                    self._base_lease_id = lease
                    # Acquisition timed out before any physical dispatch. The
                    # watchdog can release this late reservation once idle.
                    self._base_terminal_confirmed = True
        except Exception as exc:
            with self._nav_wait_lock:
                self._lease_request_uncertain = True
            self.log.error(f'Late motion coordination could not be resolved: {exc}')
        finally:
            with self._nav_wait_lock:
                self._lease_request_pending = False

    def _acquire_base_lease(self, request, action, *, target, goal):
        position = self.get_position()
        if position is None:
            return None, 'Fresh robot localization is required before movement'
        details, error = self._lease_service_call('acquire', {
            'workflow_id': getattr(request, 'workflow_id', ''), 'action': action,
            'request_tool': getattr(request, 'request_tool', ''), 'target': target, 'goal': goal,
            'current': [position['x'], position['y'], position['yaw_deg']], 'mode': self.operation_mode})
        return (details.get('lease_id'), None) if details else (None, error)

    def _release_base_lease(self, lease):
        _, error = self._lease_service_call('release', {'lease_id': lease})
        if error:
            self.log.error(error)
        return error is None

    def _navigation_watchdog(self):
        # A driver acknowledgement may arrive just after the synchronous cleanup
        # window. Release only the known-finished operation, never unknown motion.
        with self._nav_wait_lock:
            cleanup = (self._active_nav_action_goal is None and self._base_lease_id and
                self._base_terminal_confirmed and self._driver_stop_confirmed and
                not self._head_motion.has_pending_motion() and not self._lease_cleanup_pending and
                not self._lease_request_pending and not self._lease_request_uncertain and
                time.monotonic() >= self._lease_cleanup_retry_at)
            if cleanup:
                self._lease_cleanup_pending = True
                lease = self._base_lease_id
        if cleanup:
            try:
                if self._release_base_lease(lease):
                    self._base_lease_id = ''
                else:
                    self._lease_cleanup_retry_at = time.monotonic() + 1.
            finally:
                self._lease_cleanup_pending = False
        deadline = self._navigation_deadline
        if deadline is None or self._navigation_budget_expired:
            return
        if time.monotonic() < deadline and self.get_position() is not None:
            return
        self._navigation_abort_reason = 'budget' if time.monotonic() >= deadline else 'localization'
        self._navigation_budget_expired = True
        message = GlobalStop()
        message.stamp = self.get_clock().now().to_msg()
        message.reason = 'Navigation budget or localization freshness check failed'
        self.global_stop_callback(message)

    def _set_action_result_status(self, result, status_code: int, message: str):
        result.status.status = status_code
        result.status.message = message
        result.message = message
        uncertain = (not self._driver_stop_confirmed or self._head_motion.has_pending_motion() or
                     getattr(self, '_base_completion_uncertain', False) or self._lease_request_uncertain)
        result.result_data = json.dumps({'completion_confirmed': not uncertain,
            'execution_uncertain': uncertain, 'driver_stop_acknowledged': self._driver_stop_confirmed})
        return result

    def _await_controller_cleanup(self):
        deadline = time.monotonic() + 1.0
        while (not self._driver_stop_confirmed or self._head_motion.has_pending_motion()) and time.monotonic() < deadline:
            time.sleep(.01)

    def _build_move_to_result(self, status_code: int, message: str, goal_x: float, goal_y: float, goal_orientation: float):
        result = MoveToAction.Result()
        self._set_action_result_status(result, status_code, message)
        result.x = float(goal_x)
        result.y = float(goal_y)
        result.orientation = float(goal_orientation)
        return result

    def _build_turn_to_result(self, status_code: int, message: str, orientation: float):
        result = TurnToAction.Result()
        self._set_action_result_status(result, status_code, message)
        result.orientation = float(orientation)
        return result

    def _build_turn_to_relative_result(self, status_code: int, message: str, angle: float, orientation: float):
        result = TurnToRelativeAction.Result()
        self._set_action_result_status(result, status_code, message)
        result.angle = float(angle)
        result.orientation = float(orientation)
        return result

    def _build_move_to_waypoint_result(self, status_code: int, message: str, name: str, goal_x: float, goal_y: float, goal_orientation: float):
        result = MoveToWaypointAction.Result()
        self._set_action_result_status(result, status_code, message)
        result.name = name or ''
        result.goal_x = float(goal_x)
        result.goal_y = float(goal_y)
        result.goal_orientation = float(goal_orientation)
        return result

    def _build_position_for_action_result(self, status_code: int, message: str, target: str, goal_x: float, goal_y: float, goal_orientation: float):
        result = PositionForActionAction.Result()
        self._set_action_result_status(result, status_code, message)
        result.target = target or ''
        result.goal_x = float(goal_x)
        result.goal_y = float(goal_y)
        result.goal_orientation = float(goal_orientation)
        return result

    def _publish_navigation_feedback(self, goal_handle, feedback_type, status: str, message: str):
        feedback = feedback_type()
        feedback.status = status
        feedback.message = message
        goal_handle.publish_feedback(feedback)

    async def execute_move_to_action(self, goal_handle):
        request = goal_handle.request
        success, message, orientation = self._prepare_move_to(request.x, request.y, request.orientation)
        goal_x = float(request.x)
        goal_y = float(request.y)
        goal_orientation = float(orientation)
        return await self._execute_navigation_action(goal_handle, action_name='move_to', target_name='', goal_x=goal_x, goal_y=goal_y, goal_orientation=goal_orientation, setup_success=success, setup_message=message, feedback_type=MoveToAction.Feedback, result_builder=lambda status_code, result_message: self._build_move_to_result(status_code, result_message, goal_x, goal_y, goal_orientation), )

    async def execute_turn_to_action(self, goal_handle):
        request = goal_handle.request
        success, message, orientation = self._prepare_turn_to(request.orientation)
        goal_x = self.current_pose.position.x if self.current_pose is not None else 0.0
        goal_y = self.current_pose.position.y if self.current_pose is not None else 0.0
        goal_orientation = float(orientation)
        return await self._execute_navigation_action(goal_handle, action_name='turn_to', target_name='', goal_x=goal_x, goal_y=goal_y, goal_orientation=goal_orientation, setup_success=success, setup_message=message, feedback_type=TurnToAction.Feedback, result_builder=lambda status_code, result_message: self._build_turn_to_result(status_code, result_message, goal_orientation), )

    async def execute_turn_to_relative_action(self, goal_handle):
        request = goal_handle.request
        success, message, orientation = self._prepare_turn_to_relative(request.angle)
        goal_x = self.current_pose.position.x if self.current_pose is not None else 0.0
        goal_y = self.current_pose.position.y if self.current_pose is not None else 0.0
        goal_orientation = float(orientation)
        angle = float(request.angle)
        return await self._execute_navigation_action(goal_handle, action_name='turn_to_relative', target_name='', goal_x=goal_x, goal_y=goal_y, goal_orientation=goal_orientation, setup_success=success, setup_message=message, feedback_type=TurnToRelativeAction.Feedback, result_builder=lambda status_code, result_message: self._build_turn_to_relative_result(status_code, result_message, angle, goal_orientation), )

    async def execute_move_to_waypoint_action(self, goal_handle):
        request = goal_handle.request
        success, message, goal = self._prepare_move_to_waypoint(request.name)
        goal_x = 0.0 if goal is None else goal['x']
        goal_y = 0.0 if goal is None else goal['y']
        goal_orientation = 0.0 if goal is None else goal['orientation']
        target_name = '' if goal is None else goal['target']
        return await self._execute_navigation_action(goal_handle, action_name='move_to_waypoint', target_name=target_name, goal_x=goal_x, goal_y=goal_y, goal_orientation=goal_orientation, setup_success=success, setup_message=message, feedback_type=MoveToWaypointAction.Feedback, result_builder=lambda status_code, result_message: self._build_move_to_waypoint_result(status_code, result_message, target_name, goal_x, goal_y, goal_orientation), )

    async def execute_position_for_action_action(self, goal_handle):
        request = goal_handle.request
        success, message, goal = self._prepare_position_for_action(request.target, request.x, request.y, request.distance)
        goal_x = 0.0 if goal is None else goal['x']
        goal_y = 0.0 if goal is None else goal['y']
        goal_orientation = 0.0 if goal is None else goal['orientation']
        target_name = request.target
        return await self._execute_navigation_action(goal_handle, action_name='position_for_action', target_name=target_name, goal_x=goal_x, goal_y=goal_y, goal_orientation=goal_orientation, setup_success=success, setup_message=message, feedback_type=PositionForActionAction.Feedback, result_builder=lambda status_code, result_message: self._build_position_for_action_result(status_code, result_message, target_name, goal_x, goal_y, goal_orientation), )

    def _map_nav2_result(self, nav_status: int, default_stop_message: str) -> tuple[int, str]:
        if self.latest_global_stop is not None:
            return RosToolStatus.STATUS_STOPPED, default_stop_message
        if nav_status == GoalStatus.STATUS_SUCCEEDED:
            return RosToolStatus.STATUS_SUCCESS, 'Navigation completed successfully'
        if nav_status == GoalStatus.STATUS_CANCELED:
            return RosToolStatus.STATUS_STOPPED, 'Navigation was canceled'
        return RosToolStatus.STATUS_FAILURE, f'Navigation failed with status code: {nav_status}'

    def _build_goal_pose(self, x: float, y: float, yaw_deg: float) -> PoseStamped:
        yaw_rad = math.radians(yaw_deg)
        qz = math.sin(yaw_rad / 2.0)
        qw = math.cos(yaw_rad / 2.0)
        pose_msg = PoseStamped()
        pose_msg.header.stamp = self.get_clock().now().to_msg()
        pose_msg.header.frame_id = 'map'
        pose_msg.pose.position.x = x
        pose_msg.pose.position.y = y
        pose_msg.pose.position.z = 0.0
        pose_msg.pose.orientation.x = 0.0
        pose_msg.pose.orientation.y = 0.0
        pose_msg.pose.orientation.z = qz
        pose_msg.pose.orientation.w = qw
        return pose_msg

    def _publish_action_started(self, action_name: str, target_name: str | None = None):
        started_feedback = String()
        started_feedback.data = json.dumps({
            'type': 'action_started',
            'action': action_name,
            'target': target_name,
            'status': 'executing',
            'message': 'Navigation in progress'})
        self.llm_info_publisher_.publish(started_feedback)

    def get_position_callback(self, _request, response):
        position = self.get_position()
        if position is None:
            self._set_response_status(response, RosToolStatus.STATUS_FAILURE, 'No current pose available from TF')
            return response
        self._set_response_status(response, RosToolStatus.STATUS_SUCCESS, 'Current pose returned')
        response.x = position['x']
        response.y = position['y']
        response.yaw_deg = position['yaw_deg']
        response.qx = position['qx']
        response.qy = position['qy']
        response.qz = position['qz']
        response.qw = position['qw']
        return response

    def depth_detections_callback(self, msg: String):
        if not self.follow_mode_active:
            return
        try:
            data = json.loads(msg.data)
        except (json.JSONDecodeError, AttributeError):
            return
        frame_id = data.get('header', {}).get('frame_id', 'base_link')
        try:
            transform = self.tf_buffer.lookup_transform('map', frame_id, rclpy.time.Time(), timeout=Duration(seconds=config.navigation.follow_transform_timeout_seconds))
        except Exception:
            return
        new_detections = {}
        detections = data.get('detections', [])
        for det in detections:
            label = det.get('label', 'unknown')
            score = float(det.get('confidence', 0.0))
            if label == 'unknown':
                continue
            pos_3d = det.get('position_3d', {})
            if not pos_3d:
                continue
            pt = PointStamped()
            pt.header.frame_id = frame_id
            pt.header.stamp = rclpy.time.Time().to_msg()
            pt.point.x = float(pos_3d.get('x', 0.0))
            pt.point.y = float(pos_3d.get('y', 0.0))
            pt.point.z = float(pos_3d.get('z', 0.0))
            try:
                pt_map = do_transform_point(pt, transform)
            except Exception:
                continue
            if label not in new_detections or score > new_detections[label]['score']:
                new_detections[label] = {
                    'x': pt_map.point.x,
                    'y': pt_map.point.y,
                    'z': pt_map.point.z,
                    'score': score,
                    'class': label}
        self.current_detections = new_detections

    def follow_mode_update(self):
        if not self.follow_mode_active or not self.follow_target:
            return
        self._update_follow_goal()

    def _update_follow_goal(self):
        if self.current_pose is None:
            self.log.warning('No current pose available for follow mode')
            return
        target = None
        for label, det in self.current_detections.items():
            if self.follow_target.lower() in label.lower():
                target = det
                break
        if target is None:
            self.log.debug(f'Follow target {self.follow_target!r} not currently visible')
            return
        target_x = target.get('x', 0.0)
        target_y = target.get('y', 0.0)
        dx = target_x - self.current_pose.position.x
        dy = target_y - self.current_pose.position.y
        distance_to_target = math.sqrt(dx ** 2 + dy ** 2)
        if distance_to_target <= self.follow_distance:
            self.log.debug(f'Already at follow distance ({distance_to_target:.2f}m <= {self.follow_distance}m)')
            return
        if self.last_follow_position:
            last_x, last_y = self.last_follow_position
            movement = math.sqrt((target_x - last_x) ** 2 + (target_y - last_y) ** 2)
            if movement < self.follow_update_threshold:
                return
        angle_to_target = math.atan2(dy, dx)
        move_distance = distance_to_target - self.follow_distance
        map_goal_x = self.current_pose.position.x + move_distance * math.cos(angle_to_target)
        map_goal_y = self.current_pose.position.y + move_distance * math.sin(angle_to_target)
        map_goal_yaw = math.degrees(angle_to_target)
        while map_goal_yaw < 0:
            map_goal_yaw += 360
        while map_goal_yaw >= 360:
            map_goal_yaw -= 360
        self.publish_goal(map_goal_x, map_goal_y, map_goal_yaw, f'follow({self.follow_target})')
        self.last_follow_position = (target_x, target_y)
        self.log.info(f'Follow mode: Moving towards {self.follow_target} (distance: {distance_to_target:.2f}m, goal distance: {move_distance:.2f}m)')

    def _calculate_facing_orientation(self, target_x, target_y):
        if self.current_pose is None:
            return 0.0
        robot_x = self.current_pose.position.x
        robot_y = self.current_pose.position.y
        dx = target_x - robot_x
        dy = target_y - robot_y
        yaw_rad = math.atan2(dy, dx)
        yaw_deg = math.degrees(yaw_rad)
        if not all(math.isfinite(value) for value in (x, y, yaw_deg)):
            return None
        while yaw_deg < 0:
            yaw_deg += 360
        while yaw_deg >= 360:
            yaw_deg -= 360
        return yaw_deg

    def get_position(self):
        if self.current_pose is None or time.monotonic() - self._pose_received > 1.:
            self.log.warning('No current pose received from /amcl_pose yet.')
            return None
        x = self.current_pose.position.x
        y = self.current_pose.position.y
        qz = self.current_pose.orientation.z
        qw = self.current_pose.orientation.w
        yaw_rad = math.atan2(2.0 * qw * qz, 1.0 - 2.0 * qz * qz)
        yaw_deg = math.degrees(yaw_rad)
        self.log.debug(f'[getPosition] Current pose: x={x:.2f}, y={y:.2f}, yaw={yaw_deg:.1f}')
        return {
            'x': x,
            'y': y,
            'yaw_deg': yaw_deg,
            'qx': self.current_pose.orientation.x,
            'qy': self.current_pose.orientation.y,
            'qz': self.current_pose.orientation.z,
            'qw': self.current_pose.orientation.w}

    def _prepare_move_to(self, x, y, yaw_deg=None):
        try:
            goal_x = float(x)
            goal_y = float(y)
            if yaw_deg is not None:
                goal_orientation = float(yaw_deg)
            else:
                goal_orientation = self._calculate_facing_orientation(goal_x, goal_y)
        except (TypeError, ValueError):
            self.log.error('Invalid move_to arguments.')
            return False, 'Invalid navigation coordinates', 0.0
        return True, 'Navigation started', goal_orientation

    def _prepare_turn_to(self, yaw_deg):
        if self.current_pose is None:
            self.log.warning('No current pose received from /amcl_pose yet.')
            return False, 'Robot position not available', 0.0
        try:
            orientation = normalize_heading_degrees(float(yaw_deg))
        except (TypeError, ValueError):
            self.log.error('Invalid turn_to arguments.')
            return False, 'Invalid orientation argument', 0.0
        return True, 'Turn started', orientation

    def _prepare_turn_to_relative(self, relative_angle):
        if self.current_pose is None:
            self.log.warning('No current pose available for turn_to_relative.')
            return False, 'Robot position not available', 0.0
        try:
            rotation = float(relative_angle)
        except (TypeError, ValueError):
            self.log.error('Invalid turn_to_relative arguments.')
            return False, 'Invalid angle argument', 0.0
        current_yaw_deg = self._current_yaw_degrees()
        new_yaw_deg = normalize_heading_degrees(current_yaw_deg + rotation)
        self.log.info(f'[turn_to_relative] Turning {rotation}° from {current_yaw_deg:.1f}° to {new_yaw_deg:.1f}°')
        return True, 'Turn started', new_yaw_deg

    def _current_yaw_degrees(self):
        if self.current_pose is None:
            return None
        qz = self.current_pose.orientation.z
        qw = self.current_pose.orientation.w
        yaw_rad = math.atan2(2.0 * qw * qz, 1.0 - 2.0 * qz * qz)
        return math.degrees(yaw_rad)

    def _prepare_move_to_waypoint(self, name):
        waypoint_name = name.strip().lower()
        if not waypoint_name:
            self.log.error('Waypoint name is required.')
            return False, 'Waypoint name is required', None
        waypoint, message = self._get_waypoint(waypoint_name)
        if waypoint is None:
            return False, message, None
        return True, message, {
            'x': waypoint.x,
            'y': waypoint.y,
            'orientation': waypoint.orientation,
            'target': waypoint_name}

    def _prepare_position_for_action(self, target, obj_x, obj_y, distance):
        if obj_x is None or obj_y is None:
            self.log.error('Target coordinates (x, y) are required for position_for_action.')
            return False, 'Target coordinates (x, y) are required', None
        try:
            obj_x = float(obj_x)
            obj_y = float(obj_y)
            distance = float(distance)
        except (TypeError, ValueError):
            self.log.error('Invalid coordinate format for position_for_action.')
            return False, 'Invalid target coordinates', None
        if self.current_pose is None:
            self.log.warning('No current pose available.')
            return False, 'Robot position not available', None
        robot_x = self.current_pose.position.x
        robot_y = self.current_pose.position.y
        angle_to_object = math.atan2(obj_y - robot_y, obj_x - robot_x)
        goal_x = obj_x - distance * math.cos(angle_to_object)
        goal_y = obj_y - distance * math.sin(angle_to_object)
        goal_yaw_rad = angle_to_object + math.pi / 2
        goal_yaw_deg = math.degrees(goal_yaw_rad)
        while goal_yaw_deg < 0:
            goal_yaw_deg += 360
        while goal_yaw_deg >= 360:
            goal_yaw_deg -= 360
        self.log.info(f'[position_for_action] Target: {target} at ({obj_x:.2f}, {obj_y:.2f}). '
                      f'Moving to ({goal_x:.2f}, {goal_y:.2f}) with yaw {goal_yaw_deg:.1f}° '
                      f'(distance: {distance}m)')
        return True, 'Positioning started', {'x': goal_x, 'y': goal_y, 'orientation': goal_yaw_deg}

    def publish_goal(self, x, y, yaw_deg, action_name, target_name=None):
        if self.operation_mode == 'study':
            self.log.warning('Legacy navigation is disabled in the study profile; use the guarded action interface')
            return
        self.latest_global_stop = None
        pose_msg = self._build_goal_pose(x, y, yaw_deg)
        self.current_nav_action_name = action_name
        self.current_nav_target_name = target_name
        self._nav_goal_generation += 1
        current_generation = self._nav_goal_generation
        if self.use_nav2_action and self.nav_to_pose_client.wait_for_server(timeout_sec=config.navigation.navigate_wait_seconds):
            goal_msg = NavigateToPose.Goal()
            goal_msg.pose = pose_msg
            self.log.info(f'[{action_name}] Sending navigation goal via action: x={x:.2f}, y={y:.2f}, yaw={yaw_deg:.1f}°')
            send_goal_future = self.nav_to_pose_client.send_goal_async(goal_msg, feedback_callback=self._nav_feedback_callback)
            send_goal_future.add_done_callback(lambda f, gen=current_generation: self._nav_goal_response_callback(f, gen))
        else:
            self.goal_publisher_.publish(pose_msg)
            self.log.info(f'[{action_name}] Published goal via topic: x={x:.2f}, y={y:.2f}, yaw={yaw_deg:.1f}°')
            feedback = String()
            feedback.data = json.dumps({
                'type': 'action_started',
                'action': action_name,
                'target': target_name,
                'status': 'started',
                'message': f'Navigation started to x={x:.2f}, y={y:.2f}'})
            self.llm_info_publisher_.publish(feedback)

    def _nav_feedback_callback(self, feedback_msg):
        pass

    def _move_head_for_navigation(self):
        self._head_motion.last_trajectory_error = None
        if self.latest_global_stop is not None:
            return False
        if not self._head_motion.wait_for_traj_server(config.navigation.trajectory_wait_seconds):
            self.log.warning('Trajectory server not available, skipping head positioning')
            return False
        ok = self._head_motion.move_head(self.nav_head_pan, self.nav_head_tilt,
            duration=float(config.navigation.head_motion_seconds), wait=True)
        self.head_in_nav_position = bool(ok)
        if ok:
            self.log.info(f'Head positioned for navigation: pan={self.nav_head_pan:.2f}, tilt={self.nav_head_tilt:.2f}')
        return bool(ok)

    def _restore_head_after_navigation_failure(self, goal_handle, message, *, base_terminal):
        # A rejected/finished failed drive must not leave the camera looking at
        # the floor. Stop, cancellation and unresolved motion never authorize
        # a new cleanup movement. Keep the original navigation failure.
        if (not base_terminal or not self.head_in_nav_position or
                self.latest_global_stop is not None or self._head_stop_evt.is_set() or
                self._nav_action_stop_requested or goal_handle.is_cancel_requested or
                not self._driver_stop_confirmed or self._head_motion.has_pending_motion()):
            return message
        self.log.info('Navigation failed with confirmed completion; restoring normal head pose')
        if not self._reset_head_after_navigation():
            return message + '; head restoration was not confirmed'
        return message

    def _reset_head_after_navigation(self):
        self._head_motion.last_trajectory_error = None
        if self.latest_global_stop is not None:
            return False
        if not self._head_motion.wait_for_traj_server(config.navigation.trajectory_wait_seconds):
            self.log.warning('Trajectory server not available, skipping head reset')
            return False
        ok = self._head_motion.move_head(self.default_head_pan, self.default_head_tilt,
            duration=float(config.navigation.head_motion_seconds), wait=True)
        if ok:
            self.head_in_nav_position = False
            self.log.info(f'Head reset to default: pan={self.default_head_pan:.2f}, tilt={self.default_head_tilt:.2f}')
        return bool(ok)

    def _nav_goal_response_callback(self, future, goal_generation):
        if goal_generation != self._nav_goal_generation:
            self.log.info(f'Ignoring stale goal response (gen={goal_generation}, current={self._nav_goal_generation})')
            try:
                handle = future.result()
                if handle is not None and handle.accepted:
                    handle.cancel_goal_async()
            except Exception as exc:
                self.log.warning(f'Could not cancel stale legacy navigation goal: {exc}')
            return
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.log.warning(f'[{self.current_nav_action_name}] Navigation goal was rejected!')
            self._publish_action_completion(RosToolStatus.STATUS_FAILURE, 'Navigation goal was rejected by Nav2')
            self.current_nav_goal_handle = None
            self.current_nav_action_name = None
            self.current_nav_target_name = None
            return
        self.log.info(f'[{self.current_nav_action_name}] Navigation goal accepted, executing...')
        self.current_nav_goal_handle = goal_handle
        self._move_head_for_navigation()
        started_feedback = String()
        started_feedback.data = json.dumps({
            'type': 'action_started',
            'action': self.current_nav_action_name,
            'target': self.current_nav_target_name,
            'status': 'executing',
            'message': 'Navigation in progress'})
        self.llm_info_publisher_.publish(started_feedback)
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(lambda f, gen=goal_generation: self._nav_result_callback(f, gen))

    def _nav_result_callback(self, future, goal_generation):
        if goal_generation != self._nav_goal_generation:
            status = future.result().status
            self.log.info(f'Suppressed stale nav result (status={status}, gen={goal_generation}, '
                          f'current={self._nav_goal_generation})')
            return
        result = future.result()
        status_code, message = self._map_nav2_result(result.status, default_stop_message=self.format_global_stop_message('Navigation stopped'), )
        if status_code == RosToolStatus.STATUS_SUCCESS:
            self.log.info(f'[{self.current_nav_action_name}] Navigation succeeded!')
        elif result.status == GoalStatus.STATUS_CANCELED:
            self.log.info(f'[{self.current_nav_action_name}] Navigation was canceled')
        else:
            self.log.warning(f'[{self.current_nav_action_name}] Navigation failed with status: {result.status}')
        self._publish_action_completion(status_code, message)
        self.current_nav_goal_handle = None
        self.current_nav_action_name = None
        self.current_nav_target_name = None

    def _publish_action_completion(self, status_code: int, message: str, action_name: str | None = None, target_name: str | None = None):
        completion_feedback = String()
        resolved_action = self.current_nav_action_name if action_name is None else action_name
        resolved_target = self.current_nav_target_name if target_name is None else target_name
        payload = {
            'type': 'action_completed',
            'action': resolved_action,
            'target': resolved_target,
            'status': self._status_name(status_code),
            'message': message}
        completion_feedback.data = json.dumps(payload)
        self.llm_info_publisher_.publish(completion_feedback)
        self.log.info(f'Published action completion: {resolved_action} - {payload["status"]}')

    def describe_global_stop(self, msg: GlobalStop | None = None) -> str:
        stop_msg = msg or self.latest_global_stop
        if stop_msg is None:
            return ''
        if stop_msg.reason:
            reason = stop_msg.reason.strip()
            if reason == 'voice_stop':
                return ''
            return reason
        return ''

    def format_global_stop_message(self, fallback: str = 'Stopped by global stop') -> str:
        detail = self.describe_global_stop()
        if not detail:
            return fallback
        return f'{fallback}: {detail}'

    @staticmethod
    def _set_response_status(response, status_code: int, message: str):
        response.status.status = status_code
        response.status.message = message
        return response

    @staticmethod
    def _status_name(status_code: int) -> str:
        if status_code == RosToolStatus.STATUS_SUCCESS:
            return 'success'
        if status_code == RosToolStatus.STATUS_STOPPED:
            return 'stopped'
        if status_code == RosToolStatus.STATUS_TIMEOUT:
            return 'timeout'
        return 'failure'

    def yaw_towards_point(self, gx, gy) -> float:
        if self.current_pose is None:
            self.log.warning('No current pose available, defaulting yaw to 0°')
            return 0.0
        rx = self.current_pose.position.x
        ry = self.current_pose.position.y
        dx = gx - rx
        dy = gy - ry
        yaw_rad = math.atan2(dy, dx)
        yaw_deg = math.degrees(yaw_rad)
        msg = String()
        msg.data = json.dumps({
            'function': 'yaw_towards_point()',
            'status': 'ok',
            'target': {'x': gx, 'y': gy},
            'yaw_deg': yaw_deg, })
        self.llm_info_publisher_.publish(msg)
        return yaw_deg

    def get_arm_descendant_frames(self):
        try:
            frames_yaml = self.tf_buffer.all_frames_as_yaml()
        except Exception as e:
            self.log.info(f'TF all_frames_as_yaml failed: {e}')
            return []
        try:
            data = yaml.safe_load(frames_yaml)
        except Exception as e:
            self.log.info(f'YAML parse failed: {e}')
            return []
        if not isinstance(data, dict):
            return []
        parent_map = {}
        for child, info in data.items():
            if isinstance(info, dict):
                parent = info.get('parent')
                if parent:
                    parent_map[child] = parent
        descendants = set()
        root = self.arm_root_frame
        for frame in parent_map.keys():
            cur = frame
            visited = set()
            while cur in parent_map and cur not in visited:
                visited.add(cur)
                parent = parent_map[cur]
                if parent == root:
                    descendants.add(frame)
                    break
                cur = parent
        descendants.add(root)
        return list(descendants)

    def update_dynamic_footprint(self):
        frames = self.get_arm_descendant_frames()
        if not frames:
            self.log.info('No arm descendant frames found.')
            return
        base_front = config.footprint.base_front
        base_back = config.footprint.base_back
        base_half_width = config.footprint.base_half_width
        safety_margin = config.footprint.safety_margin
        min_x = base_back
        max_x = base_front
        min_y = -base_half_width
        max_y = base_half_width
        for frame in frames:
            try:
                tf = self.tf_buffer.lookup_transform(self.base_frame, frame, rclpy.time.Time())
            except TransformException:
                continue
            x = tf.transform.translation.x
            y = tf.transform.translation.y
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x
            if y < min_y:
                min_y = y
            if y > max_y:
                max_y = y
        min_x -= safety_margin
        max_x += safety_margin
        min_y -= safety_margin
        max_y += safety_margin
        poly = Polygon()
        poly.points = [Point32(x=max_x, y=max_y, z=0.0),
                       Point32(x=max_x, y=min_y, z=0.0),
                       Point32(x=min_x, y=min_y, z=0.0),
                       Point32(x=min_x, y=max_y, z=0.0)]
        self.footprint_pub_local.publish(poly)
        self.footprint_pub_global.publish(poly)
        debug_footprint = f'Footprint updated. x:[{min_x:.3f}, {max_x:.3f}], y:[{min_y:.3f}, {max_y:.3f}]'
        if debug_footprint != self.debug_footprint:
            self.debug_footprint = debug_footprint
            self.log.info(self.debug_footprint)


def main(args=None):
    rclpy.init(args=args)
    command_node = CommandNode()
    executor = MultiThreadedExecutor()
    executor.add_node(command_node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        command_node.destroy_node()
        try:
            rclpy.shutdown()
        except Exception:
            pass


if __name__ == '__main__':
    main()

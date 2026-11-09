#!/usr/bin/env python3
import json
import threading
import time
import math
import uuid

import rclpy
from action_service.give_to_person_action import GiveToPersonAction
from action_service.take_from_hand_action import TakeFromHandAction
from action_service.wave_hand_action import WaveHandAction
from control_msgs.action import FollowJointTrajectory
from geometry_msgs.msg import Twist
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from robot_common import get_logger, get_config
from robot_common.k2_runtime import K2Runtime
from robot_common.driver_state import DriverState
from robot_common.object_state import ObjectState, OBJECT_ACTIONS
from robot_common.study_policy import StudyPolicy
from sensor_msgs.msg import JointState, BatteryState
from std_msgs.msg import String
from std_srvs.srv import Trigger
from tf2_ros import Buffer, TransformListener

from action_interfaces.srv import ExecuteAction
from action_service.common.motion import MotionHelper
from action_service.dock_action import DockAction
from action_service.get_look_around_objects_action import GetLookAroundObjectsAction
from action_service.get_visible_objects_action import GetVisibleObjectsAction
from action_service.grab_item_action import GrabItemAction
from action_service.place_item_action import PlaceItemAction
from action_service.release_item_action import ReleaseItemAction
from command_interfaces.action import TurnTo as TurnToAction
from command_interfaces.action import TurnToRelative as TurnToRelativeAction
from command_interfaces.srv import GetPosition
from global_stop.msg import GlobalStop
from robot_common.stop_control import action_issue_time, issued_before_stop, stop_timestamp
from robot_common.handoff import RecipientEvidence
from tool_status.msg import ToolStatus as RosToolStatus


# These reports update the possession ledger; they never initiate movement.
# Networked hosts can differ by a few milliseconds even with synchronized clocks.
STATE_REPORT_FUTURE_TOLERANCE_SECONDS = .1


class ActionServiceNode(Node):

    def __init__(self):
        super().__init__('action_service_node')
        self.log = get_logger()
        self._startup_time = time.time()
        self._latest_joint_positions = {}
        self.driver_state = DriverState()
        self._latest_person_tracks = None
        self.recipient_evidence = RecipientEvidence()
        self.latest_global_stop = None
        self._last_stop_time = 0.0
        self._action_lock = threading.Lock()
        runtime_config = get_config('action_service', 'k2_runtime')
        self.k2_runtime = K2Runtime(runtime_config)
        self.object_state = ObjectState(state_path=runtime_config.object_state_file,
            reset_on_start=bool(getattr(runtime_config, 'object_state_reset_on_start', False)))
        self._last_object_state_time = self._startup_time
        self.study_policy = StudyPolicy(get_config('action_service', 'study_policy'))
        self.current_request_tool = ''
        self.current_workflow_id = ''
        self._request_generation = ''
        self._budget_expired = False
        self._deadline_lock = threading.Lock()
        self._safety_fault_lease = ''
        self._action_stop_evt = threading.Event()
        self.current_request_time = 0.0
        self.current_request_language = 'en'
        self._driver_stop_confirmed = True
        self._driver_stop_pending = False
        self._driver_stop_generation = 0
        self._stop_ack_lock = threading.Lock()
        self.cb_sub = ReentrantCallbackGroup()
        self.cb_srv = ReentrantCallbackGroup()
        self.srv = self.create_service(ExecuteAction, 'execute_action', self.handle_action, callback_group=self.cb_srv)
        self.base_lease_service = self.create_service(ExecuteAction, '/k2/base_lease', self._handle_base_lease, callback_group=self.cb_srv)
        self.object_state_service = self.create_service(ExecuteAction, '/k2/object_state', self._handle_object_state, callback_group=self.cb_srv)
        self.study_policy_service = self.create_service(ExecuteAction, '/k2/study_policy', self._handle_study_policy, callback_group=self.cb_srv)
        self.k2_watchdog = self.create_timer(.1, self._k2_watchdog, callback_group=self.cb_sub)
        self._traj_client = ActionClient(self, FollowJointTrajectory, '/stretch_controller/follow_joint_trajectory', callback_group=self.cb_sub)
        self.turn_absolute_client = ActionClient(self, TurnToAction, '/command_node/turn_to', callback_group=self.cb_sub)
        self.turn_relative_client = ActionClient(self, TurnToRelativeAction, '/command_node/turn_to_relative', callback_group=self.cb_sub)
        self.get_position_client = self.create_client(GetPosition, '/command_node/get_position', callback_group=self.cb_sub)
        self._vel_pub = self.create_publisher(Twist, '/stretch/cmd_vel', 10)
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.pos_mode_client = self.create_client(Trigger, '/switch_to_position_mode', callback_group=self.cb_sub)
        self.nav_mode_client = self.create_client(Trigger, '/switch_to_navigation_mode', callback_group=self.cb_sub)
        self.stow_client = self.create_client(Trigger, '/stow_the_robot', callback_group=self.cb_sub)
        self.driver_stop_client = self.create_client(Trigger, '/stop_the_robot', callback_group=self.cb_sub)
        stop_qos = QoSProfile(reliability=QoSReliabilityPolicy.RELIABLE, history=QoSHistoryPolicy.KEEP_LAST, depth=1, durability=DurabilityPolicy.VOLATILE)
        self.global_stop_sub = self.create_subscription(GlobalStop, '/global_stop', self._global_stop_callback, stop_qos, callback_group=self.cb_sub)
        self.global_stop_pub = self.create_publisher(GlobalStop, '/global_stop', stop_qos)
        self.handoff_gesture_control_pub = self.create_publisher(String, '/handoff/gesture_control', stop_qos)
        self.handoff_speech_pub = self.create_publisher(String, '/llm_to_robot_communication', 10)
        while rclpy.ok() and not self._traj_client.wait_for_server(timeout_sec=1.0):
            self.log.warning('Waiting for /stretch_controller/follow_joint_trajectory ...')
        t0 = time.time()
        while rclpy.ok() and not self.pos_mode_client.wait_for_service(timeout_sec=0.5):
            if time.time() - t0 > 6.0:
                self.log.warning('Position-mode service not available; continuing anyway.')
                break
        self.grab_item_action = GrabItemAction(self)
        self.release_item_action = ReleaseItemAction(self)
        self.place_item_action = PlaceItemAction(self)
        self.take_from_hand_action = TakeFromHandAction(self)
        self.give_to_person_action = GiveToPersonAction(self)
        self.get_look_around_objects_action = GetLookAroundObjectsAction(self)
        self.get_visible_objects_action = GetVisibleObjectsAction(self)
        self.dock_action = DockAction(self)
        self._motion_helper = MotionHelper(self, stop_evt=self._action_stop_evt, log_prefix='action_service')
        self.wave_hand_action = WaveHandAction(self)
        self._action_handlers = {
            'get_look_around_objects': lambda req, resp: self.get_look_around_objects_action.execute(req, resp),
            'get_tracked_people': lambda req, resp: self._handle_get_tracked_people(req, resp),
            'get_visible_objects': lambda req, resp: self.get_visible_objects_action.execute(req, resp),
            'grab_item': lambda req, resp: self.grab_item_action.execute(req, resp, req.parameters),
            'take_from_hand': lambda req, resp: self.take_from_hand_action.execute(req, resp, req.parameters),
            'give_to_person': lambda req, resp: self.give_to_person_action.execute(req, resp, req.parameters),
            'release_item': lambda req, resp: self.release_item_action.execute(req, resp, req.parameters),
            'place_item': lambda req, resp: self.place_item_action.execute(req, resp, req.parameters),
            'localize_trash': lambda req, resp: self.place_item_action.localize_trash(req, resp),
            'dock': lambda req, resp: self.dock_action.execute(req, resp),
            'move_head': lambda req, resp: self._handle_move_head(req, resp),
            'wave_hand': lambda req, resp: self.wave_hand_action.execute(req, resp, req.parameters), }
        self._stoppable_actions = [('get_look_around_objects', self.get_look_around_objects_action, 'scan_active'),
                                   ('get_visible_objects', self.get_visible_objects_action, 'snapshot_active'),
                                   ('grab_item', self.grab_item_action, 'grab_active'),
                                   ('take_from_hand', self.take_from_hand_action, 'handoff_active'),
                                   ('give_to_person', self.give_to_person_action, 'give_active'),
                                   ('release_item', self.release_item_action, 'release_active'),
                                   ('place_item', self.place_item_action, 'place_active'),
                                   ('dock', self.dock_action, 'dock_active'),
                                   ('wave_hand', self.wave_hand_action, 'wave_active'), ]
        qos_head_det = QoSProfile(depth=1, reliability=QoSReliabilityPolicy.BEST_EFFORT, history=QoSHistoryPolicy.KEEP_LAST, )
        self.head_det_sub = self.create_subscription(String, '/depth_mapping/head/detections', self._head_detection_callback, qos_head_det, callback_group=self.cb_sub)
        tracked_people_qos = QoSProfile(depth=1, reliability=QoSReliabilityPolicy.RELIABLE, history=QoSHistoryPolicy.KEEP_LAST, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.person_tracks_sub = self.create_subscription(String, '/person_tracking/tracks', self._person_tracks_callback, tracked_people_qos, callback_group=self.cb_sub)
        qos_gripper_det = QoSProfile(depth=1, reliability=QoSReliabilityPolicy.BEST_EFFORT, history=QoSHistoryPolicy.KEEP_LAST, )
        joint_qos = QoSProfile(depth=1, reliability=QoSReliabilityPolicy.BEST_EFFORT, history=QoSHistoryPolicy.KEEP_LAST, )
        self.gripper_det_sub = self.create_subscription(String, '/depth_mapping/gripper/detections', self._gripper_detection_callback, qos_gripper_det, callback_group=self.cb_sub, )
        self.handoff_gesture_sub = self.create_subscription(String, '/handoff/gripper_gesture', self._handoff_gesture_callback, qos_gripper_det, callback_group=self.cb_sub, )
        self.handoff_status_sub = self.create_subscription(String, '/handoff/gesture_status', self._handoff_status_callback, qos_gripper_det, callback_group=self.cb_sub)
        self.joint_sub = self.create_subscription(JointState, '/stretch/joint_states', self._joint_callback, joint_qos, callback_group=self.cb_sub, )
        self.battery_sub = self.create_subscription(BatteryState,'/battery',self._battery_callback,1,callback_group=self.cb_sub)
        self.driver_state_sub = self.create_subscription(String, '/stretch/driver_state',
            self._driver_state_callback, 1, callback_group=self.cb_sub)
        self.log.info("Action service ready on 'execute_action'")

    def handle_action(self, request, response):
        action_type = request.action_type
        self.log.info(f'Received action: {action_type} with params: {request.parameters}')
        handler = self._action_handlers.get(action_type)
        if handler is None:
            self.set_failure(response, f'Unknown action type: {action_type}')
            return response
        try:
            params = json.loads(request.parameters or '{}')
            if not isinstance(params, dict):
                raise ValueError('Parameters must be an object')
            if '_request_tool' in params and not isinstance(params['_request_tool'], str):
                raise ValueError('Invalid workflow origin')
        except (ValueError, TypeError):
            return self.set_failure(response, 'Action parameters must be a JSON object')
        policy_error = self.study_policy.action_error(action_type, params)
        if policy_error:
            return self.set_failure(response, policy_error[1], json.dumps({
                'error_code': policy_error[0], 'execution_started': False, 'execution_uncertain': False,
                'completion_confirmed': True}))
        if action_type in {'take_from_hand', 'give_to_person'}:
            timeout = params.get('timeout_seconds', 45.)
            person = params.get('person')
            if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                    not math.isfinite(timeout) or not 1 <= timeout <= 60 or
                    (person is not None and (not isinstance(person, str) or not person.strip()))):
                return self.set_failure(response, 'Invalid handover timeout or recipient', json.dumps({
                    'error_code': 'INVALID_ARGUMENTS', 'execution_started': False,
                    'execution_uncertain': False, 'completion_confirmed': True}))
        if not self._action_lock.acquire(blocking=False):
            return self.set_failure(response, 'Another action is still finishing')
        timer = None
        workflow = uuid.uuid4().hex
        try:
            if not self.k2_runtime.begin_action(workflow):
                return self.set_failure(response, 'Base motion or another arm workflow is active')
            self.current_workflow_id = workflow
            issued_at = action_issue_time(request.parameters)
            if issued_before_stop(issued_at, self._last_stop_time):
                return self.set_stopped(response, 'Command was issued before the global stop',
                    json.dumps({'execution_started': False, 'completion_confirmed': True, 'execution_uncertain': False}))
            if self._has_unfinished_work():
                return self.set_failure(response, 'Previous controller motion has not terminated',
                    json.dumps({'execution_uncertain': True, 'completion_confirmed': False}))
            object_error = self.object_state.admission_error(action_type)
            if object_error:
                return self.set_failure(response, object_error, json.dumps({
                    **self.object_state.snapshot(), 'error_code': 'OBJECT_STATE_INVALID', 'execution_started': False,
                    'execution_uncertain': False, 'completion_confirmed': True}))
            with self._stop_ack_lock:
                # A stop may arrive while policy/object checks are running.
                if issued_before_stop(issued_at, self._last_stop_time):
                    return self.set_stopped(response, 'Command was issued before the global stop',
                        json.dumps({'execution_started': False, 'completion_confirmed': True, 'execution_uncertain': False}))
                self.current_request_time = issued_at or time.time()
                self.current_request_language = 'de' if params.get('_language') == 'de' else 'en'
                self.current_request_tool = params.get('_request_tool', action_type)
                self.latest_global_stop = None
                self._action_stop_evt.clear()
            try:
                raw_budget = params.get('_budget_s', 165. if action_type in {'take_from_hand', 'give_to_person'} else 120.)
                if isinstance(raw_budget, bool):
                    raise ValueError('Invalid budget')
                budget = float(raw_budget)
                if not math.isfinite(budget) or budget <= 0:
                    raise ValueError('Invalid budget')
            except (ValueError, TypeError):
                return self.set_failure(response, 'Invalid action time budget')
            with self._deadline_lock:
                self._request_generation = workflow
                self._budget_expired = False
            deadline = time.monotonic() + min(budget, 240.)
            timer = threading.Timer(min(budget, 240.), self._expire_action_budget, args=(workflow,))
            timer.daemon = True
            timer.start()
            self.object_state.begin(action_type)
            result = handler(request, response)
            self._await_worker_cleanup()
            self._finish_action_budget(workflow, deadline)
            if self._action_stop_evt.is_set():
                self._await_driver_stop_ack()
            pending = self._has_unfinished_work()
            action = getattr(self, action_type + '_action', None)
            possession_updated = self.object_state.complete(action_type,
                success=result.status.status == RosToolStatus.STATUS_SUCCESS and not pending, action=action,
                object_id=self.study_policy.active_object if self.study_policy.mode == 'study' else params.get('item'))
            if possession_updated:
                self._last_object_state_time = time.time()
            if self._budget_expired:
                self.set_timeout(result, 'Action time budget expired; stop requested')
            elif self._action_stop_evt.is_set():
                self.set_stopped(result, 'Action stopped')
            if pending and result.status.status == RosToolStatus.STATUS_SUCCESS:
                self.set_failure(result, 'Controller or worker completion was not confirmed')
            if pending or action_type not in {'get_visible_objects', 'get_tracked_people', 'get_look_around_objects'}:
                try:
                    details = json.loads(result.result_data or '{}')
                    if not isinstance(details, dict):
                        details = {}
                except (ValueError, TypeError):
                    details = {}
                details.update({'completion_confirmed': not pending,
                    'execution_uncertain': pending or bool(details.get('execution_uncertain')),
                    'driver_stop_acknowledged': self._driver_stop_confirmed})
                details.update(self.object_state.snapshot())
                if action_type in {'release_item', 'place_item'}:
                    details.update({'gripper_opened': bool(getattr(action, '_gripper_opened', False)),
                        'release_attempted': bool(getattr(action, '_release_attempted', False))})
                if action_type in {'take_from_hand', 'give_to_person'}:
                    action = getattr(self, action_type + '_action')
                    posture_error = self.k2_runtime.posture_error(self.get_clock().now().nanoseconds / 1e9)
                    restoration_ready = ((not action._arm_motion_started or action._arm_retracted) and
                        action._orientation_restored and action._navigation_mode_restored)
                    handoff_started = bool(getattr(action, '_handoff_motion_started', False) or action._arm_motion_started)
                    travel_ready = bool(restoration_ready and not posture_error and not pending)
                    details.update({'travel_ready': travel_ready,
                        'gesture_reason': getattr(action,'_gesture_diagnostic',{}).get('reason'),
                        'gesture_diagnostic':dict(getattr(action,'_gesture_diagnostic',{})),
                        'recipient': getattr(action, '_recipient_name', None) or getattr(action, '_selected_recipient_name', None),
                        'recipient_track': getattr(action, '_recipient_track', None),
                        'recipient_policy': getattr(action, '_recipient_policy', 'gesture_only'),
                        'identity_continuity_verified': False,
                        'visible_person_count': getattr(action, '_recipient_visible_count', None),
                        'handoff_motion_started': handoff_started,
                        'arm_motion_started': bool(action._arm_motion_started),
                        'arm_retracted': action._arm_retracted,
                        'orientation_restored': action._orientation_restored,
                        'navigation_mode_restored': action._navigation_mode_restored,
                        'posture_error': posture_error,
                        'gripper_close_accepted': getattr(action, '_object_secured', False),
                        'gripper_opened': getattr(action, '_gripper_opened', False)})
                    details.update(getattr(action, '_recipient_failure_details', {}))
                    if action_type == 'take_from_hand':
                        completion = getattr(getattr(action, 'motion', None), 'last_gripper_completion', None)
                        if isinstance(completion, dict):
                            details.update(completion)
                    if not travel_ready:
                        # A known extended arm blocks driving. It does not
                        # imply that a rejected preflight left motors running.
                        if handoff_started and not restoration_ready:
                            details['execution_uncertain'] = True
                        if result.status.status == RosToolStatus.STATUS_SUCCESS:
                            self.set_failure(result, 'Gripper action finished but transit posture, orientation or navigation mode was not confirmed')
                result.result_data = json.dumps(details)
            return result
        except Exception as exc:
            if action_type in {'take_from_hand', 'give_to_person', 'grab_item', 'release_item', 'place_item'}:
                try:
                    if self.object_state.invalidate(action_type, 'workflow_exception',
                            action=getattr(self, action_type + '_action', None)):
                        self._last_object_state_time = time.time()
                except OSError:
                    self.log.exception('Could not persist unknown object state')
                self._action_stop_evt.set()
                self.stop_current_action()
            self.log.exception(f'Action execution failed: {exc}')
            return self.set_failure(response, 'Action failed; operator check required', json.dumps({
                **self.object_state.snapshot(), 'completion_confirmed': False, 'execution_uncertain': True}))
        finally:
            with self._deadline_lock:
                if self._request_generation == workflow:
                    self._request_generation = ''
            if timer is not None:
                timer.cancel()
            self.k2_runtime.finish_action(workflow)
            self.current_workflow_id = ''
            self._action_lock.release()

    def _expire_action_budget(self, generation):
        with self._deadline_lock:
            if self._request_generation != generation:
                return
            self._budget_expired = True
            self._action_stop_evt.set()
            self.stop_current_action()

    def _finish_action_budget(self, generation, deadline):
        # Close the timer before inspecting controller completion or assembling
        # the result. Also enforce the clock deadline if its callback was delayed.
        with self._deadline_lock:
            if self._request_generation != generation:
                return
            if not self._budget_expired and time.monotonic() >= deadline:
                self._budget_expired = True
                self._action_stop_evt.set()
                self.stop_current_action()
            self._request_generation = ''

    def _handle_base_lease(self, request, response):
        try:
            params = json.loads(request.parameters or '{}')
            if not isinstance(params, dict):
                raise ValueError('Parameters must be an object')
            if request.action_type == 'release':
                if self.k2_runtime.release_base(params.get('lease_id')):
                    return self.set_success(response, 'Base ownership released')
                return self.set_failure(response, 'Unknown base lease')
            if request.action_type != 'acquire':
                return self.set_failure(response, 'Unknown base lease operation')
            if params.get('mode') != self.study_policy.mode:
                return self.set_failure(response, 'Command node and action service operation modes do not match')
            token = params.get('workflow_id', '')
            if token and (token != self.current_workflow_id or params.get('action') not in {'turn_to', 'turn_to_relative'}):
                return self.set_failure(response, 'Invalid internal turn ownership')
            if self._has_unfinished_work(ignore_internal_turn=bool(token)):
                return self.set_failure(response, 'Controller completion is not confirmed')
            if self.object_state.snapshot()['possession'] == 'unknown':
                return self.set_failure(response, 'Object state is unknown; operator check required before base movement')
            error = self.study_policy.base_error(action=params.get('action'), root=params.get('request_tool'),
                target=params.get('target'), goal=params.get('goal', []), current=params.get('current', []), internal=bool(token))
            if error:
                return self.set_failure(response, error)
            lease, error = self.k2_runtime.acquire_base(token, self.get_clock().now().nanoseconds / 1e9)
            if error:
                return self.set_failure(response, error)
            return self.set_success(response, 'Base movement admitted', json.dumps({'lease_id': lease}))
        except (ValueError, TypeError):
            return self.set_failure(response, 'Invalid base lease parameters')

    def _handle_object_state(self, request, response):
        # Only read is available for model planning. Human reports are routed
        # deterministically from real input; they do not reset motion recovery.
        if request.action_type == 'read':
            with self.k2_runtime.lock:
                state = self.object_state.snapshot()
                inactive = (not self._action_lock.locked() and not self.k2_runtime.workflow and
                            not self.k2_runtime.lease and not self._has_unfinished_work())
                now = self.get_clock().now().nanoseconds / 1e9
                feedback_error = self.k2_runtime.feedback_error(now)
                state.update({'recovery_ready': bool(inactive and not feedback_error and
                                                      state['possession'] != 'unknown'),
                    'driver_state': self.driver_state.snapshot(),
                    'controller_work_finished': bool(inactive),
                    'charger_connected':self.k2_runtime.charger_connected,
                    'driver_stop_acknowledged': self._driver_stop_confirmed,
                    'joint_feedback_error': feedback_error,
                    'travel_posture_error': self.k2_runtime.posture_error(now),
                    'checked_at': time.time(), 'runtime_started_at': self._startup_time,
                    'last_stop_at': self._last_stop_time})
            return self.set_success(response, 'Object and controller state', json.dumps(state))
        human_report = request.action_type in {'report_empty', 'report_holding'}
        calibrated = request.action_type == 'calibration_completed'
        if request.action_type != 'operator_set' and not human_report and not calibrated:
            return self.set_failure(response, 'Unknown object state operation')
        with self.k2_runtime.lock:
            if not human_report and (self.k2_runtime.workflow or self.k2_runtime.lease or self._has_unfinished_work()
                    or self._action_lock.locked()):
                return self.set_failure(response, 'Finish all controller work before correcting object state',
                    json.dumps({'error_code': 'OBJECT_STATE_BUSY', 'human_report_recorded': False}))
            try:
                params = json.loads(request.parameters)
                if not isinstance(params, dict):
                    raise ValueError('Parameters must be an object')
                if calibrated:
                    now = self.get_clock().now().nanoseconds / 1e9
                    issued = action_issue_time(request.parameters)
                    started = params.get('_started_at')
                    if (not isinstance(started, (int, float)) or isinstance(started, bool) or
                            not self._startup_time <= started <= issued <= now + STATE_REPORT_FUTURE_TOLERANCE_SECONDS or now - issued > 30. or
                            not isinstance(params.get('_request_id'), str) or not params['_request_id'].strip() or
                            issued_before_stop(started, self._last_stop_time)):
                        return self.set_failure(response, 'Calibration completion report expired')
                    # A late homing response must not erase a newer user report
                    # or a subsequent object action.
                    if getattr(self, '_last_object_state_time', self._startup_time) > started:
                        return self.set_success(response, 'Newer object state preserved', json.dumps({
                            **self.object_state.snapshot(), 'calibration_completed': True,
                            'calibration_state_reset': False, 'newer_state_preserved': True}))
                    self.object_state.set('empty', 'completed_arm_calibration')
                elif human_report:
                    now = self.get_clock().now().nanoseconds / 1e9
                    issued = action_issue_time(request.parameters)
                    changed = getattr(self, '_last_object_state_time', getattr(self, '_startup_time', 0.))
                    if (not isinstance(params.get('_request_id'), str) or not params['_request_id'].strip() or
                            not 0 < issued <= now + STATE_REPORT_FUTURE_TOLERANCE_SECONDS or now - issued > 30. or issued < changed or
                            issued_before_stop(issued, self._last_stop_time)):
                        return self.set_failure(response, 'Give a fresh explicit gripper-state report',
                            json.dumps({'error_code': 'OBJECT_REPORT_EXPIRED', 'human_report_recorded': False}))
                    empty = request.action_type == 'report_empty'
                    self.object_state.set('empty' if empty else 'holding_assumed',
                        'explicit_user_empty_report' if empty else 'explicit_user_holding_report')
                else:
                    self.object_state.set(params['possession'], 'operator_visual_check')
                self._last_object_state_time = time.time()
                if not human_report and not calibrated:
                    self.object_state.label_assumed_object(self.study_policy.active_object)
            except (ValueError, TypeError, KeyError):
                return self.set_failure(response, 'Invalid operator object state')
            except OSError:
                return self.set_failure(response, 'Could not store object state; operator check required',
                    json.dumps(self.object_state.snapshot()))
        return self.set_success(response, 'Object state report recorded', json.dumps({**self.object_state.snapshot(),
            'human_report_recorded': human_report,
            **({'calibration_completed': True, 'calibration_state_reset': True} if calibrated else {})}))

    def _handle_study_policy(self, request, response):
        summary = self.study_policy.summary()
        if request.action_type == 'summary':
            return self.set_success(response, 'Study profile', json.dumps(summary))
        try:
            params = json.loads(request.parameters)
            if not isinstance(params, dict):
                raise ValueError('Parameters must be an object')
            args = params.get('arguments', {})
            if not isinstance(args, dict):
                raise ValueError('Invalid tool arguments')
            error = self.study_policy.tool_error(params['tool'], args, params.get('root_tool'))
        except (TypeError, ValueError, KeyError):
            return self.set_failure(response, 'Invalid policy request')
        if error:
            return self.set_failure(response, error[1], json.dumps({**summary, 'error_code': error[0]}))
        return self.set_success(response, 'Study policy permits the request', json.dumps(summary))

    def _k2_watchdog(self):
        runtime = self.k2_runtime
        with runtime.lock:
            lease = runtime.lease
            if not lease or lease == self._safety_fault_lease:
                return
            # Sample posture while ownership is still held. A completed turn
            # may otherwise release its lease and start arm presentation between
            # the lease lookup and this check, causing an obsolete false stop.
            error = runtime.posture_error(self.get_clock().now().nanoseconds / 1e9)
            if error:
                self._safety_fault_lease = lease
                self.log.error(error)
                message = GlobalStop()
                message.stamp = self.get_clock().now().to_msg()
                message.reason = error
                self.global_stop_pub.publish(message)

    def _has_unfinished_work(self, ignore_internal_turn=False):
        if not self._driver_stop_confirmed:
            return True
        motions = [self._motion_helper]
        for _, action, _ in self._stoppable_actions:
            worker = getattr(action, '_op_thread', None)
            if worker is not None and worker.is_alive():
                return True
            motions.append(getattr(action, 'motion', None))
            if not ignore_internal_turn:
                motions.append(getattr(action, 'turn_motion', None))
        return any(motion is not None and motion.has_pending_motion() for motion in motions)

    def _await_worker_cleanup(self):
        # A worker may signal done just before its final log/return. Give that
        # normal exit a short bounded join; an overrun remains owned and blocked.
        for _, action, _ in self._stoppable_actions:
            worker = getattr(action, '_op_thread', None)
            if worker is not None and worker is not threading.current_thread() and worker.is_alive():
                worker.join(timeout=.25)

    def _await_driver_stop_ack(self):
        deadline = time.monotonic() + 1.0
        while self._driver_stop_pending and time.monotonic() < deadline:
            time.sleep(.01)

    def announce_handoff_ready(self, action_type, reminder=False):
        if self._action_stop_evt.is_set() or issued_before_stop(self.current_request_time, self._last_stop_time):
            return
        messages = {
            'take_from_hand': (
                'Hold the item between my gripper fingers. Show my head camera a relaxed hand briefly, then a thumbs-up. Keep supporting the item until my gripper closes.',
                'Halte den Gegenstand zwischen meinen Greiferfingern. Zeige meiner Kopfkamera erst kurz eine Hand ohne Daumenzeichen, dann einen Daumen nach oben. Halte den Gegenstand fest, bis sich mein Greifer schließt.'),
            'give_to_person': (
                'Support the item securely. Show my head camera a relaxed hand briefly, then a thumbs-up. I will open my gripper.',
                'Halte den Gegenstand sicher fest. Zeige meiner Kopfkamera erst kurz eine Hand ohne Daumenzeichen, dann einen Daumen nach oben. Ich öffne dann meinen Greifer.'),
        }
        text = messages[action_type][1 if self.current_request_language == 'de' else 0]
        if reminder:
            text = ('Ich warte noch. ' if self.current_request_language == 'de' else 'I am still waiting. ') + text
        self.handoff_speech_pub.publish(String(data=json.dumps({'text': text,
            'lang': self.current_request_language, 'issued_at': self.current_request_time})))

    def _head_detection_callback(self, msg: String):
        try:
            self.recipient_evidence.update(json.loads(msg.data))
        except (TypeError, ValueError):
            pass
        self.get_look_around_objects_action.handle_detection(msg)
        self.get_visible_objects_action.handle_detection(msg)
        self.dock_action.handle_detection(msg)
        self.place_item_action.handle_detection(msg)

    def check_handoff_recipient(self, person=None, track_id=None, *, current_visible=False):
        error = self.k2_runtime.feedback_error(self.get_clock().now().nanoseconds / 1e9)
        if error:
            return None, error
        return self.recipient_evidence.check(now=self.get_clock().now().nanoseconds / 1e9,
                                             person=person, track_id=track_id, current_visible=current_visible)

    def _person_tracks_callback(self, msg: String):
        try:
            payload = json.loads(msg.data)
            tracks = payload.get('tracks')
            if not isinstance(tracks, list):
                return
        except (AttributeError, TypeError, json.JSONDecodeError):
            return
        self._latest_person_tracks = tracks

    def _gripper_detection_callback(self, msg: String):
        self.grab_item_action.handle_detection(msg)
        self.place_item_action.handle_gripper_detection(msg)

    def _handoff_gesture_callback(self, msg: String):
        self.take_from_hand_action.handle_gesture(msg)
        self.give_to_person_action.handle_gesture(msg)

    def _handoff_status_callback(self, msg: String):
        self.take_from_hand_action.handle_diagnostic(msg)
        self.give_to_person_action.handle_diagnostic(msg)

    def _joint_callback(self, msg: JointState):
        self.k2_runtime.update_joints(msg)
        self._latest_joint_positions = {name: float(msg.position[i]) for i, name in enumerate(msg.name) if
                                        i < len(msg.position)}
        self.grab_item_action.handle_joint_state(msg)

    def _battery_callback(self, msg):
        stamp=float(msg.header.stamp.sec)+float(msg.header.stamp.nanosec)*1e-9
        if 0<=self.get_clock().now().nanoseconds/1e9-stamp<=3.:
            self.k2_runtime.update_battery(msg)

    def _driver_state_callback(self, msg):
        try:
            self.driver_state.update(json.loads(msg.data))
        except (ValueError, TypeError):
            self.log.warning('Invalid driver-state snapshot ignored')

    def _global_stop_callback(self, msg: GlobalStop):
        with self._stop_ack_lock:
            self.latest_global_stop = msg
            self._last_stop_time = max(self._last_stop_time, stop_timestamp(msg))
            self._action_stop_evt.set()
        self.log.warning(f'Global stop received, stopping all actions: {self.describe_global_stop(msg)}')
        self.stop_current_action()

    def stop_current_action(self):
        # Request the driver halt first; individual worker joins must not delay it.
        with self._stop_ack_lock:
            self._driver_stop_generation += 1
            generation = self._driver_stop_generation
            self._driver_stop_confirmed = False
            self._driver_stop_pending = True
        try:
            if self.driver_stop_client.service_is_ready():
                future = self.driver_stop_client.call_async(Trigger.Request())
                future.add_done_callback(lambda result: self._driver_stop_result(result, generation))
            else:
                self.log.error('Driver stop service is unavailable; operator must use hardware stop')
                with self._stop_ack_lock:
                    if generation == self._driver_stop_generation:
                        self._driver_stop_pending = False
        except Exception as exc:
            self.log.error(f'Could not request driver halt: {exc}')
            with self._stop_ack_lock:
                if generation == self._driver_stop_generation:
                    self._driver_stop_pending = False
        self._motion_helper.cancel_active_trajectories()
        for action_name, action, active_attr in self._stoppable_actions:
            try:
                if hasattr(action, 'motion'):
                    action.motion.cancel_active_trajectories()
                if hasattr(action, 'turn_motion'):
                    action.turn_motion.cancel_active_trajectories()
                if getattr(action, active_attr, False):
                    self.log.info(f'Stopping {action_name} action')
                    action.stop()
            except Exception as e:
                self.log.warning(f'Failed to stop {action_name}: {e}')

    def _driver_stop_result(self, future, generation=None):
        try:
            response = future.result()
            confirmed = getattr(response, 'success', None) is True
        except Exception:
            confirmed = False
        with self._stop_ack_lock:
            if generation is not None and generation != self._driver_stop_generation:
                return
            self._driver_stop_confirmed = confirmed
            self._driver_stop_pending = False
        if not confirmed:
            self.log.error('Driver stop was not acknowledged; use operator/hardware stop')

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

    def _handle_move_head(self, request, response):
        try:
            params = json.loads(request.parameters) if request.parameters else {}
        except Exception:
            return self.set_failure(response, 'move_head: invalid JSON parameters')
        pan = params.get('pan')
        tilt = params.get('tilt')
        duration = float(params.get('duration', 1.5))
        if pan is None and tilt is None:
            return self.set_failure(response, 'move_head: at least one of pan or tilt must be specified')
        ok = self._motion_helper.move_head(pan=float(pan) if pan is not None else None, tilt=float(tilt) if tilt is not None else None, duration=duration, wait=True, )
        if not ok:
            return self.set_failure(response, 'move_head: trajectory execution failed')
        return self.set_success(response, f'Head moved: pan={pan}, tilt={tilt}')

    def _handle_get_tracked_people(self, request, response):
        if self._latest_person_tracks is None:
            return self.set_failure(response, 'Person tracker has not published any tracks yet')
        try:
            params = json.loads(request.parameters) if request.parameters else {}
            if not isinstance(params, dict):
                raise ValueError('parameters must be a JSON object')
        except (TypeError, ValueError, json.JSONDecodeError):
            return self.set_failure(response, 'get_tracked_people: invalid JSON parameters')
        wanted_name = ' '.join(str(params.get('person') or '').split()).casefold()
        navigation_only = bool(params.get('navigation_only', False))
        include_anonymous = bool(params.get('include_anonymous', True))
        tracks = []
        for track in self._latest_person_tracks:
            if not isinstance(track, dict):
                continue
            face_name = str(track.get('face_name') or '').strip()
            if wanted_name and ' '.join(face_name.split()).casefold() != wanted_name:
                continue
            if not include_anonymous and not face_name:
                continue
            if navigation_only and not bool(track.get('navigation_eligible')):
                continue
            tracks.append(track)
        return self.set_success(response, f'Returned {len(tracks)} tracked person(s)', json.dumps(tracks))

    @staticmethod
    def set_status(response, status_code: int, message: str, result_data: str = ''):
        response.status.status = status_code
        response.status.message = message
        response.result_data = result_data
        return response

    def set_success(self, response, message: str, result_data: str = ''):
        return self.set_status(response, RosToolStatus.STATUS_SUCCESS, message, result_data)

    def set_failure(self, response, message: str, result_data: str = ''):
        return self.set_status(response, RosToolStatus.STATUS_FAILURE, message, result_data)

    def set_stopped(self, response, fallback: str = 'Stopped by global stop', result_data: str = ''):
        return self.set_status(response, RosToolStatus.STATUS_STOPPED, self.format_global_stop_message(fallback), result_data)

    def set_timeout(self, response, message: str, result_data: str = ''):
        return self.set_status(response, RosToolStatus.STATUS_TIMEOUT, message, result_data)


def main(args=None):
    rclpy.init(args=args)
    node = ActionServiceNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except SystemExit:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.destroy_node()
        except Exception:
            pass
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()

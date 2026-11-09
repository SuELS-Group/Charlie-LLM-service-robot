import json
import math
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import Trigger
from global_stop.msg import GlobalStop
from workstation_common import get_logger
from workstation_common.config import load
from workstation_common.language import supported_language
from workstation_common.interaction import monitor_message, gripper_possession_report
from workstation_common.runtime_messages import runtime_message
from workstation_common.runtime_messages import battery_notification
from workstation_common.battery import BatteryReturnPolicy
from workstation_common.battery_announcements import ANNOUNCEMENT_TOPIC, DEFAULT_SETTINGS_FILE, BatteryAnnouncementSettings

from planner_live_view import PlannerLiveView

config = load(Path(__file__).resolve().parent / 'config' / 'llm.yaml', profile_key='model_profiles')
log = get_logger('llm.communication')


class LLMNode(Node):

    def __init__(self, communication_publisher, function_call_publisher, subscriber, robot_info_subscriber, response_subscriber, planner_live_view:
    Optional[PlannerLiveView] = None, ):
        super().__init__('communication_node')
        self.log = log
        self.communication_publisher = self.create_publisher(String, communication_publisher, 10)
        self.function_call_publisher = self.create_publisher(String, function_call_publisher, 10)
        self.led_publisher = self.create_publisher(String, config.ros_led_topic, 10)
        self.subscription = self.create_subscription(String, subscriber, self.robot_message_callback, 10)
        self.robot_info_subscription = self.create_subscription(String, robot_info_subscriber, self.robot_info_callback, 10, )
        self.response_subscription = self.create_subscription(String, response_subscriber, self.response_callback, 10, )
        self.planner_live_view = planner_live_view
        self.user_context = None
        self._message_lock = threading.Lock()
        self.stop_generation = 0
        self._last_stop_time = 0.0
        self._last_dequeued_generation = None
        self._active_context = None
        self._waiting_context = None
        self.clarification_publisher = self.create_publisher(String,
            getattr(config, 'ros_clarification_topic', '/llm/clarification_window'), 10)
        self._clarification_timer = self.create_timer(.5, self.expire_clarification_window)
        self._request_generations = {}
        self.stop_subscription = self.create_subscription(GlobalStop, '/global_stop', self.global_stop_callback, 10)
        self.stop_publisher = self.create_publisher(GlobalStop, '/global_stop', 10)
        self._seen_stops = deque(maxlen=32)
        self.last_stop_ack_generation = -1
        self.request_active = False
        self._last_input_completed_at = 0.0
        self._last_possession_input_at = 0.0
        self._last_busy_notice_at = -math.inf
        self.recovery_service = self.create_service(Trigger, '/llm_agent/acknowledge_recovery', self.acknowledge_recovery)
        self.last_received_message = ''
        self.pending_user_messages = deque()
        self.last_robot_feedback = None
        self.pending_feedback = False
        self.new_user_message_available = False
        self.last_action_completion = None
        self.pending_action_completion = False
        self._user_message_event = threading.Event()
        self.battery_policy = BatteryReturnPolicy(
            delay=float(getattr(config,'battery_return_delay_seconds',10.)),
            state_path=getattr(config,'battery_return_state_file',None))
        self.battery_policy.set_announcement_mode(BatteryAnnouncementSettings(
            getattr(config, 'battery_announcement_settings_file', DEFAULT_SETTINGS_FILE)).mode)
        self._legacy_battery_level = None
        self.battery_subscription = self.create_subscription(String,'/battery_node/events',self.battery_event_callback,1)
        self.battery_announcement_subscription = self.create_subscription(String, ANNOUNCEMENT_TOPIC,
            self.battery_announcement_mode_callback, QoSProfile(depth=1,
                reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.battery_timer = self.create_timer(.25,self.battery_tick)
        self.system_status_service = self.create_service(Trigger,'/llm_agent/system_status',self.read_system_status)
        self.log.info(f'Communication node ready: user_in={subscriber}, user_out={communication_publisher}, '
                      f'function_calls={function_call_publisher}, robot_feedback={robot_info_subscriber}, '
                      f'server_responses={response_subscriber}')

    def publish_message(self, message, language, generation=None, issued_at=None):
        with self._message_lock:
            if generation is None and self._active_context is not None:
                generation = self._active_context.stop_generation
            if generation is not None and generation != self.stop_generation:
                return
            payload = {'text': message, 'lang': supported_language(language),
                       'issued_at': time.time() if issued_at is None else issued_at}
            self.communication_publisher.publish(String(data=json.dumps(payload)))
        self.log.info(f'Published message in {language}: {message}')

    def begin_request(self, context, expected_generation=None):
        with self._message_lock:
            generation = self._last_dequeued_generation
            self._last_dequeued_generation = None
            if expected_generation is not None:
                generation = expected_generation
            elif context.followups:
                generation = context.stop_generation
            context.stop_generation = self.stop_generation if generation is None else generation
            if context.stop_generation != self.stop_generation:
                context.cancel_event.set()
            elif context.request_id:
                self._request_generations[context.request_id] = context.stop_generation
                if len(self._request_generations) > 32:
                    self._request_generations.pop(next(iter(self._request_generations)))
            self._active_context = context
            self.request_active = True

    def end_request(self, context):
        with self._message_lock:
            if self._active_context is context:
                self._active_context = None
                self.request_active = False
                self._last_input_completed_at = time.time()
                # Requests spoken during another task must not later become
                # fresh movement commands. Keep monitor events and explicit
                # possession corrections, which cannot initiate any motion.
                self.pending_user_messages = deque((message, generation)
                    for message, generation in self.pending_user_messages
                    if self._input_metadata(message)[2] or gripper_possession_report(
                        self._input_metadata(message)[0].get('text', message)) is not None)
                self.new_user_message_available = bool(self.pending_user_messages)
                if not self.pending_user_messages:
                    self._user_message_event.clear()
                else:
                    self._user_message_event.set()

    def set_clarification_window(self, context, speaker, timeout_s, allow_without_wake_word=False):
        with self._message_lock:
            if context.is_cancelled() or context.stop_generation != self.stop_generation:
                return False
            self._waiting_context = context
            now = time.time()
            self.clarification_publisher.publish(String(data=json.dumps({
                'active': True, 'request_id': context.request_id, 'speaker': speaker,
                'allow_without_wake_word': allow_without_wake_word, 'issued_at': context.issued_at,
                'opened_at': now, 'expires_at': now + min(30.0, timeout_s)})))
            return True

    def clear_clarification_window(self, context):
        with self._message_lock:
            if self._waiting_context is context:
                self._waiting_context = None
                self.clarification_publisher.publish(String(data=json.dumps({
                    'active': False, 'request_id': context.request_id})))

    def consume_clarification_window(self, context):
        with self._message_lock:
            if (self._waiting_context is not context or context.is_cancelled() or
                    time.monotonic() >= context.clarification_deadline):
                return False
            self._waiting_context = None
            self.clarification_publisher.publish(String(data=json.dumps({
                'active': False, 'request_id': context.request_id})))
            return True

    def expire_clarification_window(self):
        with self._message_lock:
            context = self._waiting_context
            if context is None or time.monotonic() < context.clarification_deadline:
                return
            self._waiting_context = None
            self.clarification_publisher.publish(String(data=json.dumps({
                'active': False, 'request_id': context.request_id})))
        self.complete_planner_session(context, 'timeout', response_text=runtime_message(
            'clarification_expired', getattr(context, 'user_language', 'en')))

    def global_stop_callback(self, message, announcement=None):
        with self._message_lock:
            identity = (message.stamp.sec, message.stamp.nanosec, message.reason)
            if identity[0] or identity[1]:
                if identity in self._seen_stops:
                    return
                self._seen_stops.append(identity)
            self.battery_policy.suppress()
            self.stop_generation += 1
            stamp = message.stamp.sec + message.stamp.nanosec / 1e9
            self._last_stop_time = max(self._last_stop_time, stamp or time.time())
            active = self._active_context is not None or self._waiting_context is not None
            waiting_context = self._waiting_context
            language = getattr(self._active_context or self._waiting_context, 'user_language',
                self.user_context.user_language if self.user_context else 'en')
            if self._active_context is not None:
                self._active_context.cancel_event.set()
            if self._waiting_context is not None:
                self._waiting_context.cancel_event.set()
                self.clarification_publisher.publish(String(data=json.dumps({
                    'active': False, 'request_id': self._waiting_context.request_id})))
                self._waiting_context = None
            self.pending_user_messages.clear()
            self._request_generations.clear()
            self.new_user_message_available = False
            self._user_message_event.clear()
        self.log.warning('Global stop invalidated the current request and queued input')
        if waiting_context is not None:
            self.complete_planner_session(waiting_context, 'stopped', response_text=runtime_message('cancelled', language))
        if active:
            text = announcement or runtime_message('cancelled', language)
            if not announcement and message.reason.casefold() in {'charging cable is connected','charging_cable_connected'}:
                text=('Ich habe angehalten, weil das Ladekabel angeschlossen ist.' if language=='de' else
                    'I stopped because the charging cable is connected.')
            if not announcement and message.reason == 'uncertain_completion':
                text += ' ' + runtime_message('recovery_required', language)
            self.publish_message(text, language, generation=self.stop_generation)
            self.last_stop_ack_generation = self.stop_generation

    def request_stop(self, reason, announcement=None):
        message = GlobalStop()
        now = time.time()
        message.stamp.sec = int(now)
        message.stamp.nanosec = int((now % 1) * 1e9)
        message.reason = reason
        self.global_stop_callback(message, announcement)
        self.stop_publisher.publish(message)

    def acknowledge_recovery(self, _request, response):
        if self.request_active or self.pending_action_completion or self.user_context is None:
            response.success = False
            response.message = 'Wait for the active request/action to end before acknowledging recovery.'
            return response
        response.success = self.user_context.executor.acknowledge_recovery()
        response.message = ('Operator acknowledgement accepted. A new user request may move the robot.'
                            if response.success else 'Could not clear the recovery marker; motion remains blocked.')
        return response

    def publish_led_status(self, status: str):
        self.led_publisher.publish(String(data=json.dumps({'status': status})))
        self.log.debug(f'Published LED status: {status}')

    def start_planner_session(self, context, user_language: str = ''):
        if self.planner_live_view is not None:
            self.planner_live_view.start_session(context, user_language=user_language)

    def update_planner_tools(self, context, tools):
        if self.planner_live_view is not None:
            self.planner_live_view.update_selected_tools(context, tools)

    def update_planner_phase(self, context, phase: str, detail: str = ''):
        if self.planner_live_view is not None:
            self.planner_live_view.update_phase(context, phase, detail)

    def record_plan(self, context, plan):
        if self.planner_live_view is not None:
            self.planner_live_view.record_plan(context, plan)

    def update_planner_progress(self, context):
        if self.planner_live_view is not None:
            self.planner_live_view.update_task_progress(context)

    def record_planning_attempt(self, context, attempt):
        if self.planner_live_view is not None:
            self.planner_live_view.record_planning_attempt(context, attempt)

    def reject_planner_decision(self, context, reason):
        if self.planner_live_view is not None:
            self.planner_live_view.reject_decision(context, reason)

    def mark_lookup_reused(self, context, index, source, result):
        if self.planner_live_view is not None:
            self.planner_live_view.mark_lookup_reused(context, index, source, result)

    def mark_plan_step_started(self, context, step_index: int, step):
        if self.planner_live_view is not None:
            self.planner_live_view.mark_step_started(context, step_index, step)

    def mark_plan_step_completed(self, context, step_index: int, result):
        if self.planner_live_view is not None:
            self.planner_live_view.mark_step_completed(context, step_index, result)

    def complete_planner_session(self, context, status: str, response_text: str = '', error_text: str = ''):
        if self.planner_live_view is not None:
            self.planner_live_view.complete_session(context, status, response_text=response_text, error_text=error_text)

    def record_clarification_rejection(self, context, text: str, reason: str):
        if self.planner_live_view is not None:
            self.planner_live_view.record_clarification_rejection(context, text, reason)

    def shutdown_planner_live_view(self):
        if self.planner_live_view is not None:
            self.planner_live_view.stop()

    def response_callback(self, msg: String):
        try:
            response = json.loads(msg.data)
        except json.JSONDecodeError:
            self.log.warning(f'Failed to parse server response: {msg.data}')
            return
        if not isinstance(response, dict):
            return
        response_type = response.get('type')
        if response_type == 'tts_request':
            text = response.get('text', '')
            request_id = response.get('request_id')
            issued_at = response.get('issued_at')
            with self._message_lock:
                generation = self._request_generations.get(request_id) if isinstance(request_id, str) else None
                valid = (generation == self.stop_generation and
                         isinstance(issued_at, (int, float)) and math.isfinite(issued_at) and
                         issued_at > self._last_stop_time)
            if text and valid:
                try:
                    self.publish_message(text, response.get('lang'), generation=generation, issued_at=issued_at)
                    self.log.info(f'Forwarded server TTS request: {text}')
                except Exception as exc:
                    self.log.error(f'Failed to publish TTS: {exc}')
            else:
                self.log.warning('Ignored uncorrelated or stale server TTS request')
        else:
            self.log.debug(f'Ignoring unknown server response type: {response_type}')

    def flush_response_callbacks(self):
        time.sleep(float(config.response_flush_sleep_s))

    def wait_for_user_message(self):
        while True:
            self._user_message_event.wait(timeout=0.1)
            with self._message_lock:
                if not self.pending_user_messages:
                    self._user_message_event.clear()
                    if not self.request_active and self._waiting_context is None:
                        claim=self.battery_policy.claim()
                        if claim:
                            self.request_active=True
                            self._last_dequeued_generation=self.stop_generation
                            return {'type':'automatic_charging','claim_id':claim,
                                'stop_generation':self.stop_generation}
                    continue
                message, generation = self.pending_user_messages.popleft()
                self.request_active = True
                self._last_dequeued_generation = generation
                self.last_received_message = message
                self.new_user_message_available = bool(self.pending_user_messages)
                if not self.pending_user_messages:
                    self._user_message_event.clear()
            self.log.info(f'Dequeued user message queue_size={len(self.pending_user_messages)}')
            return message

    def battery_event_callback(self, message):
        try:
            if self.battery_policy.update(json.loads(message.data)):
                self._user_message_event.set()
        except (ValueError,TypeError,AttributeError):
            return

    def battery_announcement_mode_callback(self, message):
        try:
            self.battery_policy.set_announcement_mode(message.data)
        except (ValueError, TypeError, AttributeError):
            self.log.warning('Ignored invalid battery announcement mode')

    def system_status_snapshot(self):
        snapshot=self.battery_policy.snapshot()
        if self._legacy_battery_level and self.battery_policy.latest is None:
            snapshot['battery']={'level':self._legacy_battery_level,'fresh':False,'source':'legacy_notification'}
        return snapshot

    def read_system_status(self,_request,response):
        response.success=True
        response.message=json.dumps(self.system_status_snapshot(),allow_nan=False)
        return response

    def battery_tick(self):
        reason=self.battery_policy.cancellation_reason()
        if (reason and self._active_context is not None and
                getattr(self._active_context,'request_source','')=='automatic_charging' and
                not self._active_context.is_cancelled()):
            language=getattr(self._active_context,'user_language','en')
            messages={'battery_data_stale':('Die Batteriedaten sind nicht mehr aktuell. Ich halte die Ladefahrt an.',
                'Battery data is no longer current. I am stopping the charging return.'),
                'battery_critical':('Der Akku ist kritisch niedrig. Ich halte die Ladefahrt an.',
                'The battery is critically low. I am stopping the charging return.')}
            pair=messages.get(reason)
            self.request_stop(reason,announcement=pair[0 if language=='de' else 1] if pair else None)
            return
        with self._message_lock:
            idle=not self.request_active and self._waiting_context is None and not self.pending_user_messages
        if idle and not self.battery_policy.eligible():
            level=self.battery_policy.take_notice()
            if level:
                language=getattr(self.user_context,'user_language','de')
                self.publish_message(battery_notification('Battery status '+level,language),language)

    @staticmethod
    def _input_metadata(raw):
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        issued_at = payload.get('issued_at', 0.0)
        valid = isinstance(issued_at, (int, float)) and math.isfinite(issued_at)
        # Keep notifications from an already running, older battery node during
        # deployment. ASR/UI envelopes do not receive this legacy classification.
        return payload, issued_at if valid else None, monitor_message(raw) is not None

    def robot_message_callback(self, msg: String):
        notification=monitor_message(msg.data)
        if notification is not None:
            # Compatibility with an older publisher during deployment. Legacy
            # text has no verified cable/sample evidence and can never move us.
            self._legacy_battery_level=next((level for level in ('green','yellow','orange','red')
                if level in notification.casefold()),'unknown')
            self.log.info('Buffered legacy battery notification outside user input')
            return
        notice = None
        with self._message_lock:
            payload, issued_at, monitor_event = self._input_metadata(msg.data)
            if issued_at is None or (issued_at and issued_at <= self._last_stop_time):
                return
            reason = None
            possession_report = (gripper_possession_report(payload.get('text', msg.data))
                if not monitor_event else None)
            busy = (self.request_active or self._active_context is not None or
                any(not self._input_metadata(message)[2] for message, _ in self.pending_user_messages))
            if not monitor_event:
                if possession_report is not None and issued_at and (
                        issued_at <= self._last_possession_input_at or
                        not -.5 <= time.time() - issued_at <= 30.):
                    reason = 'input_expired'
                elif possession_report is None and issued_at and issued_at <= self._last_input_completed_at:
                    reason = 'input_expired'
                elif busy and possession_report is None:
                    reason = 'request_busy'
            if reason:
                now = time.monotonic()
                if now - self._last_busy_notice_at >= 10.0:
                    self._last_busy_notice_at = now
                    active_language = getattr(self._active_context, 'user_language', 'en')
                    language = supported_language(payload.get('lang'), active_language)
                    notice = (runtime_message(reason, language), language, self.stop_generation)
            else:
                self.last_received_message = msg.data
                if possession_report is not None:
                    # A report is a state correction, never a queued motion.
                    # The latest correction runs next, after any active request.
                    retained = [(raw, generation) for raw, generation in self.pending_user_messages
                        if self._input_metadata(raw)[2] or
                        gripper_possession_report(self._input_metadata(raw)[0].get('text', raw)) is None]
                    self.pending_user_messages.clear()
                    self.pending_user_messages.extend(retained)
                    self.pending_user_messages.appendleft((msg.data, self.stop_generation))
                    self._last_possession_input_at = issued_at or time.time()
                    if busy:
                        active_language = getattr(self._active_context, 'user_language', 'en')
                        language = supported_language(payload.get('lang'), active_language)
                        notice = (runtime_message('object_report_queued', language), language, self.stop_generation)
                else:
                    self.pending_user_messages.append((msg.data, self.stop_generation))
                self.new_user_message_available = True
                self._user_message_event.set()
        if reason:
            self.log.info(f'Ignored interactive input: {reason}; it will not be queued for later execution')
            if notice:
                self.publish_message(notice[0], notice[1], generation=notice[2])
            return
        if notice:
            self.publish_message(notice[0], notice[1], generation=notice[2])
        self.log.info(f'Queued user message from ROS topic: {msg.data} queue_size={len(self.pending_user_messages)}')

    def robot_info_callback(self, msg: String):
        try:
            feedback = json.loads(msg.data)
        except json.JSONDecodeError:
            feedback = {'raw': msg.data}
        feedback_type = feedback.get('type')
        if feedback_type == 'action_completed':
            action = feedback.get('action')
            status = feedback.get('status')
            message = feedback.get('message')
            self.log.info(f'Action completed: {action} status={status} message={message}')
            self.last_action_completion = feedback
            self.pending_action_completion = False
        elif feedback_type == 'action_started':
            action = feedback.get('action')
            status = feedback.get('status')
            self.log.info(f'Action started: {action} status={status}')
            self.pending_action_completion = True
            self.last_action_completion = None
        elif feedback_type == 'global_stop':
            self.last_robot_feedback = feedback
            self.pending_feedback = False
            self.log.warning(f'Robot reported global stop: {feedback.get("message")}')
        else:
            self.last_robot_feedback = feedback
            self.pending_feedback = False
            self.log.info(f'Robot feedback received: {feedback}')

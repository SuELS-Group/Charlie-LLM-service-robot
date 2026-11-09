#!/usr/bin/env python3
import asyncio
import threading
from pathlib import Path

import rclpy
from rclpy.executors import MultiThreadedExecutor
from workstation_common import get_logger, load, log_summary
from workstation_common.runtime_messages import runtime_message

from communication_node import LLMNode
from mcp_client import MCPClient
from planner_live_view import PlannerLiveView
from user_context import UserContext
from automatic_charging import run_automatic_charging

config = load(Path(__file__).resolve().parent / 'config' / 'llm.yaml', profile_key='model_profiles')
log = get_logger()


def process_and_respond(loop: asyncio.AbstractEventLoop, user_context: UserContext, llm_node: LLMNode, user_message: str) -> None:
    llm_node.request_active = True
    user_context.mcp.pause_health_checks()
    try:
        if isinstance(user_message,dict) and user_message.get('type')=='automatic_charging':
            loop.run_until_complete(run_automatic_charging(user_context,user_message))
            return
        response = loop.run_until_complete(user_context.process(user_message))
        if response and response.strip():
            llm_node.publish_message(response, user_context.user_language, generation=user_context.response_generation)
    except Exception as exc:
        log.exception(f'Failed to process user message: {exc}')
        error_message = runtime_message('failed', user_context.user_language)
        try:
            llm_node.publish_message(error_message, user_context.user_language or 'en', generation=user_context.response_generation)
        except Exception as publish_exc:
            log.error(f'Failed to publish fallback error message: {publish_exc}')
        finally:
            llm_node.pending_feedback = False
            llm_node.pending_action_completion = False
    finally:
        llm_node.request_active = False
        user_context.mcp.resume_health_checks()


def main() -> None:
    log_summary(config, log, 'llm_model_fast', 'llm_model_fast_ctx', 'llm_model_smart', 'llm_model_smart_ctx',
        'llm_planning_timeout_s', 'llm_request_timeout_s', 'llm_planning_max_tokens',
        'llm_text_max_tokens', 'llm_planning_think')
    log.info('Starting LLM side')
    rclpy.init()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    mcp_client = MCPClient()
    llm_node = None
    executor = None
    executor_thread = None
    planner_live_view = None
    user_context = None
    try:
        _, _, _, llm_tools = loop.run_until_complete(mcp_client.start())
        planner_live_view = PlannerLiveView.from_config()
        planner_live_view.start()
        llm_node = LLMNode(communication_publisher=config.ros_communication_publisher, function_call_publisher=config.ros_function_calling_publisher, subscriber=config.ros_subscriber, robot_info_subscriber=config.ros_robot_to_llm_subscriber, response_subscriber=config.ros_response_subscriber, planner_live_view=planner_live_view, )
        executor = MultiThreadedExecutor(num_threads=3)
        executor.add_node(llm_node)
        executor_thread = threading.Thread(target=executor.spin, name='llm-ros-executor', daemon=True)
        executor_thread.start()
        user_context = UserContext(mcp_client=mcp_client, llm_node=llm_node)
        llm_node.user_context = user_context
        log.info(f'LLM side ready: tools={len(llm_tools)}, user_in={config.ros_subscriber}, '
                 f'user_out={config.ros_communication_publisher}')
        while True:
            user_message = llm_node.wait_for_user_message()
            if isinstance(user_message,str) and not user_message.strip():
                continue
            log.info(f'User message received: {user_message}')
            process_and_respond(loop, user_context, llm_node, user_message)
            log.info('Waiting for next user message')
    except KeyboardInterrupt:
        log.info('Shutdown requested')
    finally:
        log.info('Stopping LLM side')
        if user_context is not None:
            user_context.agent.close()
        loop.run_until_complete(mcp_client.stop())
        if executor is not None:
            executor.shutdown()
        if executor_thread is not None:
            executor_thread.join(timeout=1.0)
        if llm_node is not None:
            llm_node.shutdown_planner_live_view()
            llm_node.destroy_node()
        elif planner_live_view is not None:
            planner_live_view.stop()
        if rclpy.ok():
            rclpy.shutdown()
        loop.close()


if __name__ == '__main__':
    main()

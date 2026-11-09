import json
import os
import time
from pathlib import Path

import rclpy
from rclpy.node import Node
from robot_common import get_config, get_logger
from slam_toolbox.srv import Pause, SerializePoseGraph
from std_srvs.srv import Trigger
from tf2_ros import Buffer, TransformListener

config = get_config('command_node', Path(__file__).stem)


class SaveMapTimer(Node):

    def __init__(self):
        super().__init__('save_map_timer')
        self.log = get_logger()
        self.map_path = os.path.expanduser(config.paths.map)
        self.pose_path = os.path.expanduser(config.paths.pose)
        try:
            os.makedirs(os.path.dirname(self.map_path), exist_ok=True)
            os.makedirs(os.path.dirname(self.pose_path), exist_ok=True)
            self.log.info(f'Map path: {self.map_path}')
            self.log.info(f'Pose path: {self.pose_path}')
        except Exception as e:
            self.log.error(f'Failed to create directories: {e}')
        self.serialize_client = self.create_client(SerializePoseGraph, config.services.serialize_map)
        self.pause_client = self.create_client(Pause, config.services.pause_measurements)
        self.get_mode_client = self.create_client(Trigger, config.services.get_current_mode)
        self.stop_mapping_service = self.create_service(Trigger, config.services.stop_mapping, self.stop_mapping_callback)
        self.start_mapping_service = self.create_service(Trigger, config.services.start_mapping, self.start_mapping_callback)
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.pose_timer = self.create_timer(config.timers.pose_save_period_seconds, self.save_pose_callback)
        self.timer = self.create_timer(config.timers.map_save_period_seconds, self.save_map_callback)
        self.mapping_active = True
        self.save_in_progress = False
        self.cached_slam_mode = config.modes.unknown
        self.mode_check_timer = self.create_timer(config.timers.mode_check_period_seconds, self._update_cached_mode)
        self.log.info(f'Save map timer ready. Pose save every {config.timers.pose_save_period_seconds}s, '
                      f'map save every {config.timers.map_save_period_seconds}s.')

    def stop_mapping_callback(self, request, response):
        if self.cached_slam_mode == config.modes.localization:
            self.log.info('In localization mode - stop_mapping has no effect on SLAM')
            response.success = True
            response.message = 'In localization mode - SLAM continues processing scans.'
            return response
        if not self.mapping_active:
            response.success = True
            response.message = 'Mapping was already stopped.'
            return response
        self.log.info('Received request to stop mapping. Saving final state...')
        self.save_map_callback()
        if self.cached_slam_mode == config.modes.mapping:
            if self.pause_client.wait_for_service(timeout_sec=config.services.pause_wait_seconds):
                pause_req = Pause.Request()
                self.pause_client.call_async(pause_req)
                self.log.info('Paused SLAM measurements.')
            else:
                self.log.warning('pause_new_measurements service not available')
        self.timer.cancel()
        self.mapping_active = False
        response.success = True
        response.message = 'Mapping stopped. Final map saved to disk.'
        return response

    def start_mapping_callback(self, request, response):
        if self.cached_slam_mode == config.modes.localization:
            self.log.info('In localization mode - start_mapping has no effect')
            response.success = True
            response.message = 'In localization mode - SLAM is already processing scans.'
            return response
        if self.mapping_active:
            response.success = True
            response.message = 'Mapping is already active.'
            return response
        self.log.info('Received request to start mapping.')
        if self.cached_slam_mode == config.modes.mapping:
            if self.pause_client.wait_for_service(timeout_sec=config.services.pause_wait_seconds):
                pause_req = Pause.Request()
                self.pause_client.call_async(pause_req)
                self.log.info('Resumed SLAM measurements.')
            else:
                self.log.warning('pause_new_measurements service not available')
        self.timer = self.create_timer(config.timers.map_save_period_seconds, self.save_map_callback)
        self.mapping_active = True
        response.success = True
        response.message = 'Mapping started. Timer resumed.'
        return response

    def get_current_pose(self):
        try:
            t = self.tf_buffer.lookup_transform(config.frames.map, config.frames.base, rclpy.time.Time())
            return {
                'x': t.transform.translation.x,
                'y': t.transform.translation.y,
                'z': t.transform.rotation.z,
                'w': t.transform.rotation.w}
        except Exception as e:
            self.log.warning(f'Could not get robot pose: {e}')
            return None

    def save_pose_callback(self):
        pose = self.get_current_pose()
        if pose:
            try:
                os.makedirs(os.path.dirname(self.pose_path), exist_ok=True)
                with open(self.pose_path, 'w') as f:
                    json.dump(pose, f)
                if not hasattr(self, '_last_pose_log_time'):
                    self._last_pose_log_time = 0
                current_time = self.get_clock().now().nanoseconds / 1e9
                if current_time - self._last_pose_log_time > config.timers.pose_log_period_seconds:
                    self.log.info(f'Pose saved: x={pose["x"]:.2f}, y={pose["y"]:.2f}')
                    self._last_pose_log_time = current_time
            except Exception as e:
                self.log.error(f'Failed to save pose: {e}')

    def _update_cached_mode(self):
        if not self.get_mode_client.service_is_ready():
            return
        future = self.get_mode_client.call_async(Trigger.Request())
        future.add_done_callback(self._on_mode_check_complete)

    def _on_mode_check_complete(self, future):
        try:
            result = future.result()
            self.cached_slam_mode = result.message
        except Exception as e:
            self.log.debug(f'Could not check SLAM mode: {e}')

    def save_map_callback(self):
        if self.cached_slam_mode != config.modes.mapping:
            return
        try:
            os.makedirs(os.path.dirname(self.map_path), exist_ok=True)
        except OSError:
            pass
        if self.save_in_progress:
            self.log.warning('Map save already in progress, skipping...')
            return
        if self.serialize_client.wait_for_service(timeout_sec=config.services.serialize_wait_seconds):
            self.save_in_progress = True
            req = SerializePoseGraph.Request()
            req.filename = self.map_path
            future = self.serialize_client.call_async(req)
            future.add_done_callback(self._on_save_complete)
            self.log.info('Map save started...')
        else:
            self.log.warning('serialize_map service not available')

    def _on_save_complete(self, future):
        self.save_in_progress = False
        try:
            result = future.result()
            self.log.info('Saved SLAM Map successfully.')
        except Exception as e:
            self.log.error(f'Map save failed: {e}')

    def destroy_node(self):
        self.log.info('Shutting down save_map_timer...')
        wait_count = 0
        max_waits = int(config.shutdown.max_wait_seconds / config.shutdown.poll_period_seconds)
        while self.save_in_progress and wait_count < max_waits:
            self.log.info('Waiting for map save to complete...')
            time.sleep(config.shutdown.poll_period_seconds)
            wait_count += 1
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = SaveMapTimer()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.log.info('Ctrl+C received, saving map before exit...')
    finally:
        node.destroy_node()
        try:
            rclpy.shutdown()
        except Exception:
            pass

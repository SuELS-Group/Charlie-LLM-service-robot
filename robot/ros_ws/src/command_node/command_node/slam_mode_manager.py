import json
import math
import os
import subprocess
import time
from pathlib import Path
from typing import Tuple

import psutil
import rclpy
from ament_index_python.packages import get_package_share_directory
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from robot_common import get_config, get_logger
from slam_toolbox.srv import SerializePoseGraph
from std_srvs.srv import Trigger
from tf2_ros import Buffer, TransformListener

config = get_config('command_node', Path(__file__).stem)


class SlamModeManager(Node):

    def __init__(self):
        super().__init__('slam_mode_manager')
        self.log = get_logger()
        self.callback_group = ReentrantCallbackGroup()
        self.mode_localization = config.modes.localization
        self.mode_mapping = config.modes.mapping
        self.current_mode: str = self.mode_localization
        self.mode_switch_in_progress: bool = False
        self.map_path = os.path.expanduser(config.paths.map)
        self.pose_path = os.path.expanduser(config.paths.pose)
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.serialize_client = self.create_client(SerializePoseGraph, config.services.serialize_map, callback_group=self.callback_group)
        self.switch_to_mapping_srv = self.create_service(Trigger, config.services.switch_to_mapping, self.switch_to_mapping_callback, callback_group=self.callback_group)
        self.switch_to_localization_srv = self.create_service(Trigger, config.services.switch_to_localization, self.switch_to_localization_callback, callback_group=self.callback_group)
        self.get_mode_srv = self.create_service(Trigger, config.services.get_current_mode, self.get_current_mode_callback, callback_group=self.callback_group)
        requested_mode = config.modes.initial
        map_file_exists = os.path.exists(self.map_path + '.posegraph')
        if requested_mode == self.mode_localization and not map_file_exists:
            self.current_mode = self.mode_mapping
            self.log.warning(f'Requested mode was {requested_mode}, but no map exists. '
                             f'Actual mode is {self.mode_mapping}.')
        else:
            self.current_mode = requested_mode
        self.log.info(f'SLAM mode manager ready. Mode={self.current_mode}, '
                      f'map_exists={map_file_exists}, map_path={self.map_path}.posegraph')

    def get_current_mode_callback(self, request, response):
        response.success = True
        response.message = self.current_mode
        return response

    def switch_to_mapping_callback(self, request, response):
        if self.mode_switch_in_progress:
            response.success = False
            response.message = 'Mode switch already in progress. Please wait.'
            return response
        if self.current_mode == self.mode_mapping:
            response.success = True
            response.message = 'Already in mapping mode.'
            return response
        self.log.info('Switching to MAPPING mode...')
        success, message = self._switch_mode(self.mode_mapping)
        response.success = success
        response.message = message
        return response

    def switch_to_localization_callback(self, request, response):
        if self.mode_switch_in_progress:
            response.success = False
            response.message = 'Mode switch already in progress. Please wait.'
            return response
        if self.current_mode == self.mode_localization:
            response.success = True
            response.message = 'Already in localization mode.'
            return response
        self.log.info('Switching to LOCALIZATION mode...')
        success, message = self._switch_mode(self.mode_localization)
        response.success = success
        response.message = message
        return response

    def _switch_mode(self, target_mode: str) -> Tuple[bool, str]:
        self.mode_switch_in_progress = True
        try:
            self.log.info('Step 1: Saving current map state...')
            save_success = self._save_map()
            if not save_success:
                self.log.warning('Could not save map (service may not be available yet)')
            else:
                time.sleep(1.0)
            self.log.info('Step 2: Stopping all slam_toolbox processes...')
            if not self._kill_slam_toolbox():
                self.mode_switch_in_progress = False
                return False, 'Failed to stop slam_toolbox'
            self.log.info('Step 3: Waiting for TF cache to clear (5s)...')
            time.sleep(5.0)
            remaining = self._count_slam_processes()
            if remaining > 0:
                self.log.warning(f'Still found {remaining} slam_toolbox process(es), killing again...')
                self._kill_slam_toolbox()
                time.sleep(2.0)
            self.log.info(f'Step 4: Starting slam_toolbox in {target_mode} mode...')
            if not self._start_slam_toolbox(target_mode):
                self.mode_switch_in_progress = False
                return False, f'Failed to start slam_toolbox in {target_mode} mode'
            self.log.info('Step 5: Waiting for slam_toolbox to be ready...')
            if not self._wait_for_slam_ready(timeout=20.0):
                self.log.warning('slam_toolbox may not be fully ready, but continuing...')
            self.current_mode = target_mode
            self.mode_switch_in_progress = False
            self.log.info(f'Successfully switched to {target_mode} mode!')
            return True, f'Successfully switched to {target_mode} mode'
        except Exception as e:
            self.mode_switch_in_progress = False
            self.log.error(f'Mode switch failed: {e}')
            return False, f'Mode switch failed: {str(e)}'

    def _save_map(self) -> bool:
        if not self.serialize_client.wait_for_service(timeout_sec=config.services.serialize_wait_seconds):
            self.log.warning('serialize_map service not available')
            return False
        try:
            request = SerializePoseGraph.Request()
            request.filename = self.map_path
            future = self.serialize_client.call_async(request)
            start_time = time.time()
            while not future.done() and (time.time() - start_time) < config.save.timeout_seconds:
                time.sleep(config.save.poll_period_seconds)
            if future.done():
                result = future.result()
                self.log.info(f'Map saved to {self.map_path}')
                return True
            else:
                self.log.warning('Map save timed out')
                return False
        except Exception as e:
            self.log.error(f'Failed to save map: {e}')
            return False

    def _load_saved_pose(self) -> list:
        default_pose = [config.pose.default_x, config.pose.default_y, config.pose.default_theta]
        if not os.path.exists(self.pose_path):
            self.log.info(f'No saved pose found at {self.pose_path}, using default')
            return default_pose
        try:
            with open(self.pose_path, 'r') as f:
                pose = json.load(f)
            theta = 2.0 * math.atan2(pose['z'], pose['w'])
            result = [pose['x'], pose['y'], theta]
            self.log.info(f'Loaded saved pose: x={pose["x"]:.3f}, y={pose["y"]:.3f}, theta={theta:.3f}')
            return result
        except Exception as e:
            self.log.warning(f'Failed to load saved pose: {e}, using default')
            return default_pose

    def _kill_slam_toolbox(self) -> bool:
        try:
            killed_count = 0
            try:
                result = subprocess.run([config.process.kill_command,
                                         '-9',
                                         '-f',
                                         config.process.match_name], capture_output=True, timeout=config.process.kill_timeout_seconds)
                if result.returncode == 0:
                    self.log.info('Killed slam_toolbox processes via pkill')
                    killed_count += 1
            except Exception as e:
                self.log.debug(f'pkill failed: {e}')
            processes_to_kill = []
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    cmdline = proc.info.get('cmdline', [])
                    name = proc.info.get('name', '')
                    if cmdline and any(config.process.match_name in str(arg) for arg in cmdline):
                        processes_to_kill.append(proc)
                    elif config.process.match_name in name or config.process.mapping_executable in name:
                        processes_to_kill.append(proc)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            for proc in processes_to_kill:
                try:
                    self.log.info(f'Found remaining slam_toolbox process (PID: {proc.pid}), force killing...')
                    proc.kill()
                    killed_count += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            for proc in processes_to_kill:
                try:
                    proc.wait(timeout=config.process.wait_timeout_seconds)
                except (psutil.TimeoutExpired, psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            if killed_count > 0:
                self.log.info(f'Killed {killed_count} slam_toolbox process(es)')
            else:
                self.log.info('No running slam_toolbox process found')
            return True
        except Exception as e:
            self.log.error(f'Error killing slam_toolbox: {e}')
            return False

    def _count_slam_processes(self) -> int:
        count = 0
        for proc in psutil.process_iter(['pid', 'cmdline']):
            try:
                cmdline = proc.info.get('cmdline', [])
                if cmdline and any(config.process.match_name in str(arg) for arg in cmdline):
                    count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return count

    def _start_slam_toolbox(self, mode: str) -> bool:
        try:
            map_path = self.map_path
            map_file_exists = os.path.exists(map_path + '.posegraph')
            if mode == self.mode_localization and not map_file_exists:
                self.log.error('Cannot start localization mode without an existing map!')
                self.log.error(f'Expected map at: {map_path}.posegraph')
                self.log.error('Please create a map first using mapping mode.')
                return False
            start_pose = self._load_saved_pose()
            if mode == self.mode_localization:
                slam_executable = config.process.localization_executable
            else:
                slam_executable = config.process.mapping_executable
            cmd = [config.process.command,
                   'run',
                   config.process.package,
                   slam_executable,
                   '--ros-args',
                   '-r',
                   f'__node:={config.process.node_name}',
                   '--params-file',
                   os.path.join(get_package_share_directory(config.process.params_package), config.process.params_directory, f'{config.process.params_prefix}_{mode}.yaml', ), ]
            if mode == self.mode_mapping:
                cmd.extend(['-p', f'mode:={config.modes.mapping}'])
            if map_file_exists:
                cmd.extend(['-p', f'map_file_name:={map_path}'])
                self.log.info(f'Loading existing map from {map_path} (mode: {mode})')
            else:
                self.log.info(f'Starting fresh mapping (no existing map)')
            pose_str = f'[{start_pose[0]},{start_pose[1]},{start_pose[2]}]'
            cmd.extend(['-p', f'map_start_pose:={pose_str}'])
            self.log.info(f'Using initial pose estimate: {pose_str}')
            self.log.info(f'Starting slam_toolbox with command: {" ".join(cmd)}')
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            time.sleep(config.process.startup_grace_seconds)
            if process.poll() is not None:
                _, stderr = process.communicate(timeout=1)
                self.log.error(f'slam_toolbox failed to start! Exit code: {process.returncode}')
                self.log.error(f'stderr: {stderr.decode() if stderr else "empty"}')
                return False
            self.log.info(f'slam_toolbox started (PID: {process.pid})')
            return True
        except Exception as e:
            self.log.error(f'Failed to start slam_toolbox: {e}')
            return False

    def _wait_for_slam_ready(self, timeout: float = 15.0) -> bool:
        start_time = time.time()
        time.sleep(config.ready.start_delay_seconds)
        self.serialize_client = self.create_client(SerializePoseGraph, config.services.serialize_map, callback_group=self.callback_group)
        while (time.time() - start_time) < timeout:
            if self.serialize_client.wait_for_service(timeout_sec=config.ready.service_poll_seconds):
                try:
                    self.tf_buffer.lookup_transform(config.frames.map, config.frames.odom, rclpy.time.Time(), timeout=rclpy.duration.Duration(seconds=config.ready.tf_timeout_seconds), )
                    self.log.info('slam_toolbox is ready! (service available + TF publishing)')
                    return True
                except Exception as e:
                    self.log.debug(f'TF not ready yet: {e}')
            time.sleep(config.ready.service_poll_seconds)
        self.log.warning(f'Timeout waiting for slam_toolbox (waited {timeout}s)')
        return False


def main(args=None):
    rclpy.init(args=args)
    node = SlamModeManager()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        node.log.info('Shutting down SLAM Mode Manager...')
    finally:
        node.destroy_node()
        try:
            rclpy.shutdown()
        except Exception:
            pass


if __name__ == '__main__':
    main()

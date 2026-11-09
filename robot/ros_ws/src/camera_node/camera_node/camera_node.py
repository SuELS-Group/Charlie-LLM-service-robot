import os
import signal
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np
import rclpy
from cv_bridge import CvBridge
from rcl_interfaces.msg import Parameter, ParameterType, ParameterValue
from rcl_interfaces.srv import SetParameters
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from robot_common import get_config, get_logger
from sensor_msgs.msg import CompressedImage, Image

config = get_config('camera_node', Path(__file__).stem)


class CameraNode(Node):

    def __init__(self):
        super().__init__('camera_node')
        self.log = get_logger()
        self.enable_nav = config.nav.enabled
        self.enable_head = config.head.enabled
        self.enable_gripper = config.gripper.enabled
        qos_profile = QoSProfile(reliability=QoSReliabilityPolicy.BEST_EFFORT, history=QoSHistoryPolicy.KEEP_LAST, depth=config.qos.sensor_depth, )
        self.bridge = CvBridge()
        self.cap = None
        self.nav_timer = None
        self.child_process_timer = None
        self.child_process_failed = False
        self._last_head_feedback = 0.0
        self._last_gripper_feedback = 0.0
        self._source_counts = {'head': 0, 'gripper': 0}
        self._source_previous = {'head': 0, 'gripper': 0}
        self._source_dimensions = {'head': None, 'gripper': None}
        self._last_source_status = time.monotonic()
        self.source_status_timer = None
        self.nav_exposure = config.nav.exposure
        if self.enable_nav:
            self.nav_port = config.nav.port
            self.nav_width = config.nav.width
            self.nav_height = config.nav.height
            self.nav_fps = config.nav.fps
            self.nav_pub_compressed = self.create_publisher(CompressedImage, config.nav.topic, qos_profile)
            self.cap = cv2.VideoCapture(self.nav_port)
            if self.cap.isOpened():
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.nav_width)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.nav_height)
                self.cap.set(cv2.CAP_PROP_FPS, self.nav_fps)
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                self.nav_timer = self.create_timer(1.0 / float(self.nav_fps), self.nav_camera_callback)
                self.log.info(f'Navigation camera ready on {config.nav.topic} '
                              f'({self.nav_width}x{self.nav_height} @ {self.nav_fps} FPS)')
            else:
                self.log.warning(f'Failed to open navigation camera on port {self.nav_port}')
        self.head_process = None
        self.head_exposure = config.head.exposure
        self.head_gain = config.head.gain
        if self.enable_head:
            self.head_client = self.create_client(SetParameters, config.head.set_parameters_service)
            self.head_sub = self.create_subscription(Image, config.head.image_topic, self.head_feedback_callback, qos_profile, )
            self.head_process = self.launch_realsense(camera_config=config.head)
            time.sleep(config.realsense.startup_delay_seconds)
            self.log.info(f'Head camera starting on {config.head.image_topic}')
        self.gripper_process = None
        self.gripper_exposure = config.gripper.exposure
        self.gripper_gain = config.gripper.gain
        if self.enable_gripper:
            self.gripper_client = self.create_client(SetParameters, config.gripper.set_parameters_service)
            self.gripper_sub = self.create_subscription(Image, config.gripper.image_topic, self.gripper_feedback_callback, qos_profile, )
            self.gripper_process = self.launch_realsense(camera_config=config.gripper)
            self.log.info(f'Gripper camera starting on {config.gripper.image_topic}')
        if self.head_process is not None or self.gripper_process is not None:
            self.child_process_timer = self.create_timer(1.0, self.check_child_processes)
            self.source_status_timer = self.create_timer(float(config.diagnostics.status_period_seconds), self.source_status_callback, )
        enabled_cameras = [name for name, enabled in
                           (('nav', self.enable_nav), ('head', self.enable_head), ('gripper', self.enable_gripper),) if
                           enabled]
        self.log.info(f'Camera node ready. Enabled cameras: {", ".join(enabled_cameras) or "none"}')

    def launch_realsense(self, camera_config):
        color_width = int(getattr(camera_config, 'color_width', camera_config.width))
        color_height = int(getattr(camera_config, 'color_height', camera_config.height))
        depth_width = int(getattr(camera_config, 'depth_width', camera_config.width))
        depth_height = int(getattr(camera_config, 'depth_height', camera_config.height))
        cmd = [config.realsense.command,
               'launch',
               config.realsense.launch_package,
               config.realsense.launch_file,
               f'camera_name:={camera_config.name}',
               f'camera_namespace:={config.realsense.camera_namespace}',
               f'device_type:={camera_config.device_type}',
               f'initial_reset:={str(camera_config.initial_reset).lower()}',
               f'enable_color:={str(config.realsense.enable_color).lower()}',
               f'enable_depth:={str(config.realsense.enable_depth).lower()}',
               f'rgb_camera.color_profile:={color_width}x{color_height}x{camera_config.color_fps}',
               f'depth_module.depth_profile:={depth_width}x{depth_height}x{camera_config.depth_fps}',
               f'depth_module.infra_profile:={depth_width}x{depth_height}x{camera_config.depth_fps}',
               f'depth_module.color_profile:={color_width}x{color_height}x{camera_config.color_fps}',
               f'align_depth.enable:={str(config.realsense.align_depth).lower()}',
               f'pointcloud.enable:={str(config.realsense.pointcloud).lower()}',
               f'enable_gyro:={str(config.realsense.enable_gyro).lower()}',
               f'enable_accel:={str(config.realsense.enable_accel).lower()}',
               f'enable_infra1:={str(config.realsense.enable_infra1).lower()}',
               f'enable_infra2:={str(config.realsense.enable_infra2).lower()}',
               f'color_qos:={config.realsense.color_qos}',
               f'depth_qos:={config.realsense.depth_qos}',
               f'pointcloud.pointcloud_qos:={config.realsense.pointcloud_qos}',
               f'pointcloud.allow_no_texture_points:={str(config.realsense.allow_no_texture_points).lower()}',
               f'{camera_config.parameter_prefix}.enable_auto_exposure:={str(camera_config.enable_auto_exposure).lower()}',
               f'{camera_config.parameter_prefix}.exposure:={camera_config.exposure}',
               f'{camera_config.parameter_prefix}.gain:={camera_config.gain}',
               f'{camera_config.parameter_prefix}.contrast:={camera_config.contrast}',
               f'{camera_config.parameter_prefix}.saturation:={camera_config.saturation}',
               f'{camera_config.parameter_prefix}.sharpness:={camera_config.sharpness}',
               f'rgb_camera.enable_auto_white_balance:={str(config.realsense.enable_auto_white_balance).lower()}', ]
        jpeg_quality = getattr(camera_config, 'jpeg_quality', None)
        if jpeg_quality is not None:
            cmd.append(f'color.image_raw.jpeg_quality:={int(jpeg_quality)}')
        self.log.info(f'Launching {camera_config.name} realsense pipeline')
        return subprocess.Popen(cmd, preexec_fn=os.setsid if hasattr(os, 'setsid') else None)

    def check_child_processes(self):
        for name, proc in (('head', self.head_process), ('gripper', self.gripper_process)):
            if proc is None:
                continue
            return_code = proc.poll()
            if return_code is None:
                continue
            self.child_process_failed = True
            self.log.error(f'{name} RealSense process exited unexpectedly with code {return_code}')
            rclpy.shutdown()
            return

    def update_hardware_params(self, client, prefix, exposure, gain):
        if not client.service_is_ready(): return
        req = SetParameters.Request()
        req.parameters = [Parameter(name=f'{prefix}.exposure', value=ParameterValue(type=ParameterType.PARAMETER_INTEGER, integer_value=int(exposure))),
                          Parameter(name=f'{prefix}.gain', value=ParameterValue(type=ParameterType.PARAMETER_INTEGER, integer_value=int(gain)))]
        client.call_async(req)

    def get_auto_params(self, frame, current_exp, current_gain, exp_range=None, gain_range=None):
        exp_range = exp_range or (config.auto_exposure.min_exposure, config.auto_exposure.max_exposure)
        gain_range = gain_range or (config.auto_exposure.min_gain, config.auto_exposure.max_gain)
        brightness = np.mean(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        target = config.auto_exposure.target_brightness
        if brightness < target - config.auto_exposure.tolerance:
            if current_exp < exp_range[1]:
                current_exp = min(exp_range[
                                      1], int(current_exp * config.auto_exposure.exposure_scale_up) + config.auto_exposure.exposure_step, )
            elif current_gain < gain_range[1]:
                current_gain = min(gain_range[1], current_gain + config.auto_exposure.gain_step)
        elif brightness > target + config.auto_exposure.tolerance:
            if current_gain > gain_range[0]:
                current_gain = max(gain_range[0], current_gain - config.auto_exposure.gain_step)
            elif current_exp > exp_range[0]:
                current_exp = max(exp_range[
                                      0], int(current_exp * config.auto_exposure.exposure_scale_down) - config.auto_exposure.exposure_step, )
        return current_exp, current_gain

    @staticmethod
    def _feedback_due(last_feedback):
        feedback_hz = max(0.1, float(config.auto_exposure.feedback_hz))
        now = time.monotonic()
        return now, now - last_feedback >= 1.0 / feedback_hz

    def _feedback_frame(self, msg):
        frame = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
        height, width = frame.shape[:2]
        max_dimension = max(1, int(config.auto_exposure.sample_max_dimension))
        longest = max(height, width)
        if longest > max_dimension:
            scale = float(max_dimension) / float(longest)
            frame = cv2.resize(frame, (max(1, int(width * scale)),
                                       max(1, int(height * scale))), interpolation=cv2.INTER_AREA)
        return frame

    def _record_source_frame(self, camera, msg):
        self._source_counts[camera] += 1
        self._source_dimensions[camera] = (int(msg.width), int(msg.height))

    def source_status_callback(self):
        now = time.monotonic()
        elapsed = now - self._last_source_status
        if elapsed <= 0:
            return
        status = []
        for camera, enabled in (('head', self.enable_head), ('gripper', self.enable_gripper),):
            if not enabled:
                continue
            count = self._source_counts[camera]
            fps = (count - self._source_previous[camera]) / elapsed
            self._source_previous[camera] = count
            dimensions = self._source_dimensions[camera]
            resolution = (f'{dimensions[0]}x{dimensions[1]}' if dimensions is not None else 'waiting')
            status.append(f'{camera}: F:{fps:.1f} R:{resolution}')
        self._last_source_status = now
        if status:
            self.log.info('Camera source | ' + ' | '.join(status))

    def head_feedback_callback(self, msg):
        self._record_source_frame('head', msg)
        now, due = self._feedback_due(self._last_head_feedback)
        if not due:
            return
        self._last_head_feedback = now
        frame = self._feedback_frame(msg)
        ne, ng = self.get_auto_params(frame, self.head_exposure, self.head_gain)
        if ne != self.head_exposure or ng != self.head_gain:
            self.head_exposure, self.head_gain = ne, ng
            self.update_hardware_params(self.head_client, 'rgb_camera', ne, ng)

    def gripper_feedback_callback(self, msg):
        self._record_source_frame('gripper', msg)
        now, due = self._feedback_due(self._last_gripper_feedback)
        if not due:
            return
        self._last_gripper_feedback = now
        frame = self._feedback_frame(msg)
        ne, ng = self.get_auto_params(frame, self.gripper_exposure, self.gripper_gain)
        if ne != self.gripper_exposure or ng != self.gripper_gain:
            self.gripper_exposure, self.gripper_gain = ne, ng
            self.update_hardware_params(self.gripper_client, 'depth_module', ne, ng)

    def nav_camera_callback(self):
        if not self.cap:
            return
        ret, frame = self.cap.read()
        if not ret:
            return
        brightness = np.mean(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        if brightness < config.nav.brightness_low:
            self.nav_exposure = min(config.nav.max_exposure, self.nav_exposure + config.nav.exposure_step)
        elif brightness > config.nav.brightness_high:
            self.nav_exposure = max(config.nav.min_exposure, self.nav_exposure - config.nav.exposure_step)
        self.cap.set(cv2.CAP_PROP_EXPOSURE, self.nav_exposure)
        ts = self.get_clock().now().to_msg()
        ok, enc = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, config.nav.jpeg_quality])
        if ok:
            c_msg = CompressedImage()
            c_msg.header.stamp, c_msg.header.frame_id, c_msg.format, c_msg.data = ts, config.nav.frame_id, 'jpeg', np.array(enc).tobytes()
            self.nav_pub_compressed.publish(c_msg)

    def destroy_node(self):
        self.log.info('Shutting down camera node')
        if self.cap:
            self.cap.release()
        for proc in [self.head_process, self.gripper_process]:
            if proc:
                try:
                    if proc.poll() is not None:
                        continue
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                    time.sleep(config.shutdown.process_grace_seconds)
                    os.killpg(pgid, signal.SIGKILL)
                    proc.wait(timeout=config.shutdown.process_wait_seconds)
                except Exception:
                    if proc.poll() is None:
                        proc.kill()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = CameraNode()

    def exit_handler(sig, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, exit_handler)
    signal.signal(signal.SIGTERM, exit_handler)
    signal.signal(signal.SIGHUP, exit_handler)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    if node.child_process_failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()

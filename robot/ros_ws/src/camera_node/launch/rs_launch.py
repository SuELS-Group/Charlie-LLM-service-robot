import os

import launch_ros.actions
import yaml
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration


def parameter(name, default, description):
    return {'name': name, 'default': default, 'description': description, }


configurable_parameters = [parameter('camera_name', 'camera', 'camera unique name'),
                           parameter('camera_namespace', 'camera', 'namespace for camera'),
                           parameter('serial_no', "''", 'choose device by serial number'),
                           parameter('usb_port_id', "''", 'choose device by usb port id'),
                           parameter('device_type', "''", 'choose device by type'),
                           parameter('config_file', "''", 'yaml config file'),
                           parameter('json_file_path', "''", 'allows advanced configuration'),
                           parameter('initial_reset', 'false', "''"),
                           parameter('accelerate_gpu_with_glsl', 'false', 'enable GPU acceleration with GLSL'),
                           parameter('rosbag_filename', "''", 'A realsense bagfile to run from as a device'),
                           parameter('log_level', 'info', 'debug log level [DEBUG|INFO|WARN|ERROR|FATAL]'),
                           parameter('output', 'screen', 'pipe node output [screen|log]'),
                           parameter('enable_color', 'true', 'enable color stream'),
                           parameter('rgb_camera.color_profile', '0,0,0', 'color stream profile'),
                           parameter('rgb_camera.color_format', 'RGB8', 'color stream format'),
                           parameter('rgb_camera.enable_auto_exposure', 'true', 'enable/disable auto exposure for color image'),
                           parameter('rgb_camera.exposure', '-1', 'Manual exposure time in microseconds'),
                           parameter('rgb_camera.gain', '-1', 'Manual gain'),
                           parameter('rgb_camera.contrast', '-1', 'Contrast'),
                           parameter('rgb_camera.saturation', '-1', 'Saturation'),
                           parameter('rgb_camera.sharpness', '-1', 'Sharpness'),
                           parameter('color_qos', 'SENSOR_DATA', 'color stream QoS preset'),
                           parameter('color.image_raw.jpeg_quality', '95', 'JPEG quality for the compressed color transport'),
                           parameter('enable_depth', 'true', 'enable depth stream'),
                           parameter('enable_infra', 'false', 'enable infra0 stream'),
                           parameter('enable_infra1', 'false', 'enable infra1 stream'),
                           parameter('enable_infra2', 'false', 'enable infra2 stream'),
                           parameter('depth_module.depth_profile', '0,0,0', 'depth stream profile'),
                           parameter('depth_module.color_profile', '0,0,0', 'color stream profile for depth alignment'),
                           parameter('depth_module.depth_format', 'Z16', 'depth stream format'),
                           parameter('depth_module.infra_profile', '0,0,0', 'infra streams (0/1/2) profile'),
                           parameter('depth_module.infra_format', 'RGB8', 'infra0 stream format'),
                           parameter('depth_module.infra1_format', 'Y8', 'infra1 stream format'),
                           parameter('depth_module.infra2_format', 'Y8', 'infra2 stream format'),
                           parameter('depth_module.exposure', '8500', 'Depth module manual exposure value'),
                           parameter('depth_module.gain', '16', 'Depth module manual gain value'),
                           parameter('depth_module.contrast', '50', 'Depth module manual contrast value'),
                           parameter('depth_module.saturation', '50', 'Depth module manual saturation value'),
                           parameter('depth_module.sharpness', '50', 'Depth module manual sharpness value'),
                           parameter('depth_module.hdr_enabled', 'false', 'Depth module hdr enablement flag. Used for hdr_merge filter', ),
                           parameter('depth_module.enable_auto_exposure', 'true', 'enable/disable auto exposure for depth image'),
                           parameter('depth_qos', 'SENSOR_DATA', 'depth stream QoS preset'),
                           parameter('depth_module.exposure.1', '7500', 'Depth module first exposure value. Used for hdr_merge filter', ),
                           parameter('depth_module.gain.1', '16', 'Depth module first gain value. Used for hdr_merge filter', ),
                           parameter('depth_module.exposure.2', '1', 'Depth module second exposure value. Used for hdr_merge filter', ),
                           parameter('depth_module.gain.2', '16', 'Depth module second gain value. Used for hdr_merge filter', ),
                           parameter('enable_sync', 'false', "'enable sync mode'"),
                           parameter('enable_rgbd', 'false', "'enable rgbd topic'"),
                           parameter('enable_gyro', 'false', "'enable gyro stream'"),
                           parameter('enable_accel', 'false', "'enable accel stream'"),
                           parameter('gyro_fps', '0', "''"),
                           parameter('accel_fps', '0', "''"),
                           parameter('unite_imu_method', '0', '[0-None, 1-copy, 2-linear_interpolation]'),
                           parameter('clip_distance', '-2.', "''"),
                           parameter('angular_velocity_cov', '0.01', "''"),
                           parameter('linear_accel_cov', '0.01', "''"),
                           parameter('diagnostics_period', '0.0', 'Rate of publishing diagnostics. 0=Disabled'),
                           parameter('publish_tf', 'true', '[bool] enable/disable publishing static & dynamic TF'),
                           parameter('tf_publish_rate', '0.0', '[double] rate in Hz for publishing dynamic TF'),
                           parameter('pointcloud.enable', 'true', ''),
                           parameter('pointcloud.stream_filter', '2', 'texture stream for pointcloud'),
                           parameter('pointcloud.stream_index_filter', '0', 'texture stream index for pointcloud'),
                           parameter('pointcloud.ordered_pc', 'false', ''),
                           parameter('pointcloud.pointcloud_qos', 'SENSOR_DATA', 'point cloud QoS preset'),
                           parameter('pointcloud.allow_no_texture_points', 'false', "''"),
                           parameter('align_depth.enable', 'false', 'enable align depth filter'),
                           parameter('colorizer.enable', 'false', 'enable colorizer filter'),
                           parameter('decimation_filter.enable', 'false', 'enable_decimation_filter'),
                           parameter('spatial_filter.enable', 'false', 'enable_spatial_filter'),
                           parameter('temporal_filter.enable', 'false', 'enable_temporal_filter'),
                           parameter('disparity_filter.enable', 'false', 'enable_disparity_filter'),
                           parameter('hole_filling_filter.enable', 'false', 'enable_hole_filling_filter'),
                           parameter('hdr_merge.enable', 'false', 'hdr_merge filter enablement flag'),
                           parameter('wait_for_device_timeout', '-1.', 'Timeout for waiting for device to connect (Seconds)'),
                           parameter('reconnect_timeout', '6.', 'Timeout(seconds) between consequtive reconnection attempts'), ]


def declare_configurable_parameters(parameters):
    return [DeclareLaunchArgument(param['name'], default_value=param['default'], description=param['description'], ) for
            param in parameters]


def set_configurable_parameters(parameters):
    return {param['name']: LaunchConfiguration(param['name']) for param in parameters}


def yaml_to_dict(path_to_yaml):
    with open(path_to_yaml, 'r') as f:
        return yaml.load(f, Loader=yaml.SafeLoader)


def launch_setup(context, params, param_name_suffix=''):
    _config_file = LaunchConfiguration('config_file' + param_name_suffix).perform(context)
    params_from_file = {} if _config_file == "''" else yaml_to_dict(_config_file)
    _output = LaunchConfiguration('output' + param_name_suffix)
    if os.getenv('ROS_DISTRO') == 'foxy':
        _output = context.perform_substitution(_output)
    return [launch_ros.actions.Node(package='realsense2_camera', namespace=LaunchConfiguration('camera_namespace' + param_name_suffix), name=LaunchConfiguration('camera_name' + param_name_suffix), executable='realsense2_camera_node', parameters=[
        params,
        params_from_file], output=_output, arguments=['--ros-args',
                                                      '--log-level',
                                                      LaunchConfiguration('log_level' + param_name_suffix), ], emulate_tty=True, )]


def generate_launch_description():
    return LaunchDescription(declare_configurable_parameters(configurable_parameters) + [OpaqueFunction(function=launch_setup, kwargs={
        'params': set_configurable_parameters(configurable_parameters)}, )])

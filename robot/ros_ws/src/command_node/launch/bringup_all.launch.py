import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    stretch_nav2_path = get_package_share_directory('stretch_nav2')
    command_node_path = get_package_share_directory('command_node')
    minimal_rviz_config = os.path.join(command_node_path, 'config', 'minimal_nav.rviz')
    launch_rviz_arg = LaunchConfiguration('launch_rviz')
    declare_launch_rviz = DeclareLaunchArgument('launch_rviz', default_value='false', choices=['true',
                                                                                               'false'], description='Whether to launch RViz (default: false for robot)')
    return LaunchDescription([declare_launch_rviz, # Start the /map readiness subscriber before
                              # SLAM loads and rasterizes its saved pose graph.
                              Node(package='command_node', executable='main', output='screen'),
                              IncludeLaunchDescription(PythonLaunchDescriptionSource(os.path.join(stretch_nav2_path, 'launch/navigation_ws.launch.py')), launch_arguments={
                                  'use_slam': 'True',
                                  'slam_mode': 'localization',
                                  'use_rviz': 'false'}.items()),
                              Node(condition=IfCondition(launch_rviz_arg), package='rviz2', executable='rviz2', name='rviz2', arguments=[
                                  '-d',
                                  minimal_rviz_config], output='screen'),
                              Node(package='waypoint_node', executable='main', output='screen', parameters=[]),
                              Node(package='command_node', executable='save_map_timer', output='screen'),
                              Node(package='command_node', executable='slam_mode_manager', output='screen', ), ])

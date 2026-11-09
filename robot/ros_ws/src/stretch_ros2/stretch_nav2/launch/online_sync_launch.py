import json
import math
import os
import subprocess

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.launch_context import LaunchContext
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def kill_existing_slam_toolbox():
    """Kill any existing slam_toolbox processes before starting a new one."""
    try:
        # Find and kill all slam_toolbox processes
        result = subprocess.run(['pkill', '-9', '-f', 'slam_toolbox'], capture_output=True, timeout=5)
        if result.returncode == 0:
            print("[SLAM Launch] Killed existing slam_toolbox process(es)")
            # Give time for the process to fully terminate and TF buffer to clear stale transforms
            # This is critical for proper SLAM initialization - matches the wait in slam_mode_manager
            import time

            time.sleep(3.0)
        else:
            print("[SLAM Launch] No existing slam_toolbox processes found")
    except Exception as e:
        print(f"[SLAM Launch] Warning: Could not check for existing slam_toolbox: {e}")


def launch_setup(context: LaunchContext):
    """
    OpaqueFunction to set up the SLAM node with the correct mode and parameters.
    This is called at launch time when we can resolve the LaunchConfiguration values.
    """
    # CRITICAL: Kill any existing slam_toolbox processes before starting
    # This prevents conflicts with leftover processes from previous runs
    kill_existing_slam_toolbox()
    # Dynamic path for the user
    home_dir = os.path.expanduser("~")
    last_pose_path = os.path.join(home_dir, ".stretch_nav/state/last_pose.json")
    map_path = os.path.join(home_dir, ".stretch_nav/maps/my_slam_map")
    # Ensure directories exist
    os.makedirs(os.path.dirname(last_pose_path), exist_ok=True)
    os.makedirs(os.path.dirname(map_path), exist_ok=True)
    default_pose = [0.0, 0.0, 0.0]
    if os.path.exists(last_pose_path):
        try:
            with open(last_pose_path, "r") as f:
                pose = json.load(f)
            theta = 2.0 * math.atan2(pose["z"], pose["w"])
            map_start_pose = [pose["x"], pose["y"], theta]
        except Exception as e:
            print("Failed to load saved pose:", e)
            map_start_pose = default_pose
    else:
        map_start_pose = default_pose
    # Get the slam_mode from launch configuration
    requested_mode = LaunchConfiguration('slam_mode').perform(context)
    use_sim_time = LaunchConfiguration('use_sim_time').perform(context)
    # Check if map exists
    map_exists = os.path.exists(map_path + ".posegraph")
    # AUTO-SWITCH LOGIC:
    # If localization was requested but no map exists, automatically switch to mapping mode
    if requested_mode == 'localization' and not map_exists:
        print("[SLAM Launch] " + "=" * 60)
        print("[SLAM Launch] WARNING: Localization mode requested but NO MAP EXISTS!")
        print(f"[SLAM Launch] Expected map at: {map_path}.posegraph")
        print("[SLAM Launch] AUTOMATICALLY SWITCHING TO MAPPING MODE")
        print("[SLAM Launch] A new map will be created. Once done, restart to use localization.")
        print("[SLAM Launch] " + "=" * 60)
        slam_mode = 'mapping'
    else:
        slam_mode = requested_mode
    # Select the appropriate params file based on effective mode
    stretch_nav2_dir = get_package_share_directory("stretch_nav2")
    if slam_mode == 'localization':
        params_file = os.path.join(stretch_nav2_dir, 'config', 'mapper_params_localization.yaml')
    else:
        params_file = os.path.join(stretch_nav2_dir, 'config', 'mapper_params_mapping.yaml')
    print(f"[SLAM Launch] Requested mode: {requested_mode}")
    print(f"[SLAM Launch] Effective mode: {slam_mode}")
    print(f"[SLAM Launch] Map exists: {map_exists}")
    print(f"[SLAM Launch] Params file: {params_file}")
    # Prepare parameters list
    # The YAML file has parameters under slam_toolbox.ros__parameters namespace
    # Additional parameters passed directly must also use the correct format
    slam_node_params = [params_file, {'use_sim_time': use_sim_time == 'true'}, ]
    # When loading an existing map, we need to set map_file_name and map_start_pose
    # These parameters need to be in the correct format for slam_toolbox to read them
    if map_exists:
        print(f"[SLAM Launch] Loading existing map: {map_path}")
        slam_node_params.append({"map_file_name": map_path})
        # Use the saved pose as initial estimate for localization
        # slam_toolbox will refine this position using scan matching against the loaded map
        print(f"[SLAM Launch] Using saved pose as initial estimate: {map_start_pose}")
        slam_node_params.append({"map_start_pose": map_start_pose})
        if slam_mode == 'localization':
            print("[SLAM Launch] Mode: LOCALIZATION - map is frozen, only pose tracking")
            print("[SLAM Launch] Using localization_slam_toolbox_node for /initialpose support")
        else:
            print("[SLAM Launch] Mode: MAPPING - continuing to add to existing map")
            slam_node_params.append({"mode": slam_mode})
    else:
        # No map exists - we've already switched to mapping mode above if needed
        print(f"[SLAM Launch] Starting fresh map (no existing map found)")
        print(f"[SLAM Launch] Using start pose: {map_start_pose}")
        slam_node_params.append({"map_start_pose": map_start_pose})
    # CRITICAL: Use the correct executable based on mode
    # - localization_slam_toolbox_node: Supports /initialpose, frozen map, rolling buffer
    # - sync_slam_toolbox_node: For mapping, can add nodes to the pose graph
    # 
    # NOTE: sync_slam_toolbox_node with mode:localization does NOT support /initialpose!
    # The mode parameter only affects Ceres optimization, not the node's behavior.
    if slam_mode == 'localization':
        slam_executable = 'localization_slam_toolbox_node'
    else:
        slam_executable = 'sync_slam_toolbox_node'
    print(f"[SLAM Launch] Using executable: {slam_executable}")
    return [Node(parameters=slam_node_params, package='slam_toolbox', executable=slam_executable, name='slam_toolbox', output='screen')]


def generate_launch_description():
    declare_use_sim_time_argument = DeclareLaunchArgument('use_sim_time', default_value='false', description='Use simulation/Gazebo clock')
    # SLAM mode argument - localization is default for stable operation
    declare_slam_mode_argument = DeclareLaunchArgument('slam_mode', default_value='localization', choices=[
        'localization',
        'mapping'], description='SLAM mode: "localization" for stable pose tracking with existing map, '
                                '"mapping" for creating/updating the map')
    # Keep the old parameter for backwards compatibility
    declare_slam_params_file_cmd = DeclareLaunchArgument('slam_params_file', default_value='',
                                                         # No longer used directly, kept for compatibility
                                                         description='[DEPRECATED] Use slam_mode instead. This parameter is ignored.')
    ld = LaunchDescription()
    ld.add_action(declare_use_sim_time_argument)
    ld.add_action(declare_slam_mode_argument)
    ld.add_action(declare_slam_params_file_cmd)
    ld.add_action(OpaqueFunction(function=launch_setup))
    return ld

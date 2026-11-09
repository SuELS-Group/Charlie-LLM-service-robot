"""A late ROS subscriber receives the saved mode in an isolated local domain."""
import os
import subprocess
import sys
import textwrap


def test_saved_mode_is_republished_after_bridge_restart(tmp_path):
    env = os.environ.copy()
    for name in ('ROS_DISCOVERY_SERVER', 'ROS_SUPER_CLIENT', 'FASTRTPS_DEFAULT_PROFILES_FILE',
                 'FASTDDS_DEFAULT_PROFILES_FILE'):
        env.pop(name, None)
    env.update(ROS_DOMAIN_ID='211', ROS_LOCALHOST_ONLY='1', ROS_LOG_DIR=str(tmp_path / 'logs'))
    code = textwrap.dedent('''
        import sys
        import time
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
        from std_msgs.msg import String
        from workstation_common.battery_announcements import ANNOUNCEMENT_TOPIC
        from web_bridge_node.web_bridge_node import WebBridgeNode

        rclpy.init(args=['--ros-args', '-p', 'battery_announcement_settings_file:=' + sys.argv[1]])
        bridge = WebBridgeNode()
        assert bridge._serialize_battery()['announcement_mode'] == 'reduced'
        bridge._set_battery_announcements({'mode': 'off'})
        bridge.destroy_node()
        bridge = WebBridgeNode()
        assert bridge._serialize_battery()['announcement_mode'] == 'off'
        listener = Node('battery_announcement_test_listener')
        received = []
        subscription = listener.create_subscription(String, ANNOUNCEMENT_TOPIC,
            lambda message: received.append(message.data), QoSProfile(depth=1,
                reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        try:
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and not received:
                rclpy.spin_once(listener, timeout_sec=.1)
            assert received == ['off'], received
            bridge._set_battery_announcements({'mode': 'reduced'})
            while time.monotonic() < deadline and received[-1] != 'reduced':
                rclpy.spin_once(listener, timeout_sec=.1)
            assert received == ['off', 'reduced'], received
        finally:
            listener.destroy_node()
            bridge.destroy_node()
            rclpy.shutdown()
    ''')
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path / 'settings.json')],
                            env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr

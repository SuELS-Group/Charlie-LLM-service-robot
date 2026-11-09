"""ROS message stubs for planner tests that run without a ROS workspace."""
import sys
from pathlib import Path


_workspace = Path(__file__).resolve().parents[2]
_llm_node = _workspace / "llm_node"
_mcp_server = _workspace / "mcp_server"
for p in [str(_llm_node), str(_mcp_server), str(_workspace / "testing" / "llm")]:
    if p not in sys.path:
        sys.path.insert(0, p)
import rclpy_mock
import std_msgs_mock

sys.modules['rclpy'] = rclpy_mock
sys.modules['rclpy.node'] = rclpy_mock.node
sys.modules['rclpy.qos'] = rclpy_mock.qos
sys.modules['std_msgs'] = std_msgs_mock
sys.modules['std_msgs.msg'] = std_msgs_mock.msg
from types import ModuleType
try:
    from global_stop.msg import GlobalStop
except ModuleNotFoundError:
    # Offline agent tests need a message value, never a ROS type-support library.
    from types import SimpleNamespace
    _global_stop = ModuleType('global_stop')
    _global_stop_msg = ModuleType('global_stop.msg')
    class _MockGlobalStop:
        def __init__(self, reason='', stamp=None):
            self.reason = reason
            self.stamp = stamp or SimpleNamespace(sec=0, nanosec=0)
    _global_stop_msg.GlobalStop = _MockGlobalStop
    _global_stop.msg = _global_stop_msg
    sys.modules['global_stop'] = _global_stop
    sys.modules['global_stop.msg'] = _global_stop_msg

if 'std_srvs' not in sys.modules:
    _std_srvs = ModuleType('std_srvs')
    _std_srvs_srv = ModuleType('std_srvs.srv')
    sys.modules['std_srvs'] = _std_srvs
    sys.modules['std_srvs.srv'] = _std_srvs_srv
    class _MockTrigger:
        class Request: pass
        class Response: pass
    _std_srvs_srv.Trigger = _MockTrigger
if 'action_service' not in sys.modules:
    _action_service = ModuleType('action_service')
    _action_service_srv = ModuleType('action_service.srv')


    class _MockExecuteAction:
        class Request:

            def __init__(self):
                self.action_name = ""
                self.action_json = ""


    _action_service_srv.ExecuteAction = _MockExecuteAction
    _action_service.srv = _action_service_srv
    sys.modules['action_service'] = _action_service
    sys.modules['action_service.srv'] = _action_service_srv

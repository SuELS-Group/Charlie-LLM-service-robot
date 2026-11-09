from unittest.mock import Mock

import pytest
from waypoint_node.waypoint_node import WaypointNode

from tool_status.msg import ToolStatus as RosToolStatus
from waypoint_interfaces.srv import SaveWaypointAtPose


def _node_for_callback():
    node = object.__new__(WaypointNode)
    node.waypoints = {}
    node._save_waypoints = Mock(return_value=True)
    node.publish_saved_waypoints = Mock()
    node.log = Mock()
    return node


def test_save_waypoint_at_pose_persists_map_pose():
    node = _node_for_callback()
    request = SaveWaypointAtPose.Request()
    request.waypoint.name = 'Pointed Table'
    request.waypoint.x = 1.5
    request.waypoint.y = -0.25
    request.waypoint.orientation = 90.0
    request.waypoint.qz = 2 ** -0.5
    request.waypoint.qw = 2 ** -0.5
    request.waypoint.frame_id = 'map'
    response = node.save_waypoint_at_pose_callback(request, SaveWaypointAtPose.Response())
    assert response.status.status == RosToolStatus.STATUS_SUCCESS
    assert response.waypoint.name == 'pointed table'
    assert response.waypoint.x == pytest.approx(1.5)
    assert node.waypoints['pointed table']['orientation'] == pytest.approx(90.0)
    node._save_waypoints.assert_called_once_with()


def test_save_waypoint_at_pose_rejects_non_map_frame():
    node = _node_for_callback()
    request = SaveWaypointAtPose.Request()
    request.waypoint.name = 'bad'
    request.waypoint.frame_id = 'odom'
    response = node.save_waypoint_at_pose_callback(request, SaveWaypointAtPose.Response())
    assert response.status.status == RosToolStatus.STATUS_FAILURE
    assert node.waypoints == {}

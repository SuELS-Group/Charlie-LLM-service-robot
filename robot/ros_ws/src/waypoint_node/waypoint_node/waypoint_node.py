import json
import math
import os
from pathlib import Path

import rclpy
import tf2_ros
from rclpy.duration import Duration
from rclpy.node import Node
from robot_common import get_config, get_logger
from std_msgs.msg import ColorRGBA
from tf2_ros import TransformException
from visualization_msgs.msg import Marker, MarkerArray

from tool_status.msg import ToolStatus as RosToolStatus
from waypoint_interfaces.msg import Waypoint
from waypoint_interfaces.srv import (DeleteWaypoint, GetWaypoint, ListWaypoints, SaveWaypoint, SaveWaypointAtPose, )

config = get_config('waypoint_node', Path(__file__).stem)


class WaypointNode(Node):

    def __init__(self):
        super().__init__('waypoint_node')
        self.log = get_logger()
        self.waypoints_file = os.path.expanduser(config.storage.file)
        os.makedirs(os.path.dirname(self.waypoints_file), exist_ok=True)
        self.waypoints = self._load_waypoints()
        self.tf_buffer = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)
        self.save_service = self.create_service(SaveWaypoint, config.services.save, self.save_waypoint_callback)
        self.save_pose_service = self.create_service(SaveWaypointAtPose, config.services.save_pose, self.save_waypoint_at_pose_callback, )
        self.get_service = self.create_service(GetWaypoint, config.services.get, self.get_waypoint_callback)
        self.list_service = self.create_service(ListWaypoints, config.services.list, self.list_waypoints_callback)
        self.delete_service = self.create_service(DeleteWaypoint, config.services.delete, self.delete_waypoint_callback)
        self.waypoints_marker_pub = self.create_publisher(MarkerArray, config.topics.markers, config.qos.depth)
        self.create_timer(config.marker.publish_period_seconds, self.publish_saved_waypoints)
        self.log.info(f'Waypoint node ready. Loaded {len(self.waypoints)} waypoint(s) from {self.waypoints_file}')

    def _load_waypoints(self):
        if not os.path.exists(self.waypoints_file):
            return {}
        try:
            with open(self.waypoints_file, 'r', encoding='utf-8') as file:
                loaded = json.load(file)
        except Exception as exc:
            self.log.error(f'Failed to load waypoints from {self.waypoints_file}: {exc}')
            return {}
        return {name.lower(): self._normalize_waypoint(name.lower(), data) for name, data in loaded.items() if
                isinstance(data, dict)}

    def _normalize_waypoint(self, name, data):
        quaternion = data.get('quaternion') or {}
        return {
            'name': name,
            'x': float(data.get('x', 0.0)),
            'y': float(data.get('y', 0.0)),
            'orientation': float(data.get('orientation', 0.0)),
            'qx': float(data.get('qx', quaternion.get('x', 0.0))),
            'qy': float(data.get('qy', quaternion.get('y', 0.0))),
            'qz': float(data.get('qz', quaternion.get('z', 0.0))),
            'qw': float(data.get('qw', quaternion.get('w', 1.0))),
            'frame_id': str(data.get('frame_id', config.frames.map)), }

    def _save_waypoints(self):
        try:
            with open(self.waypoints_file, 'w', encoding='utf-8') as file:
                json.dump(self.waypoints, file, indent=2)
            return True
        except Exception as exc:
            self.log.error(f'Failed to save waypoints to {self.waypoints_file}: {exc}')
            return False

    def _build_waypoint_msg(self, waypoint_data):
        waypoint = Waypoint()
        waypoint.name = waypoint_data['name']
        waypoint.x = waypoint_data['x']
        waypoint.y = waypoint_data['y']
        waypoint.orientation = waypoint_data['orientation']
        waypoint.qx = waypoint_data['qx']
        waypoint.qy = waypoint_data['qy']
        waypoint.qz = waypoint_data['qz']
        waypoint.qw = waypoint_data['qw']
        waypoint.frame_id = waypoint_data['frame_id']
        return waypoint

    def _lookup_current_waypoint(self, name):
        transform = self.tf_buffer.lookup_transform(config.frames.map, config.frames.base, rclpy.time.Time())
        rotation = transform.transform.rotation
        yaw = math.atan2(2.0 * (rotation.w * rotation.z + rotation.x * rotation.y), 1.0 - 2.0 * (
                rotation.y * rotation.y + rotation.z * rotation.z), )
        return {
            'name': name,
            'x': float(transform.transform.translation.x),
            'y': float(transform.transform.translation.y),
            'orientation': math.degrees(yaw),
            'qx': float(rotation.x),
            'qy': float(rotation.y),
            'qz': float(rotation.z),
            'qw': float(rotation.w),
            'frame_id': config.frames.map, }

    def publish_saved_waypoints(self):
        markers = MarkerArray()
        stamp = self.get_clock().now().to_msg()
        for marker_id, waypoint in enumerate(self.waypoints.values()):
            marker = Marker()
            marker.header.frame_id = config.marker.frame_id
            marker.header.stamp = stamp
            marker.ns = config.marker.namespace
            marker.id = marker_id
            marker.type = Marker.CYLINDER
            marker.action = Marker.ADD
            marker.pose.position.x = waypoint['x']
            marker.pose.position.y = waypoint['y']
            marker.pose.position.z = 0.0
            marker.scale.x = config.marker.cylinder.scale.x
            marker.scale.y = config.marker.cylinder.scale.y
            marker.scale.z = config.marker.cylinder.scale.z
            marker.color = ColorRGBA(r=config.marker.cylinder.color.r, g=config.marker.cylinder.color.g, b=config.marker.cylinder.color.b, a=config.marker.cylinder.color.a, )
            marker.lifetime = Duration(seconds=config.marker.lifetime_seconds).to_msg()
            markers.markers.append(marker)
            text = Marker()
            text.header.frame_id = config.marker.frame_id
            text.header.stamp = stamp
            text.ns = config.marker.text.namespace
            text.id = marker_id
            text.type = Marker.TEXT_VIEW_FACING
            text.action = Marker.ADD
            text.pose.position.x = waypoint['x']
            text.pose.position.y = waypoint['y']
            text.pose.position.z = config.marker.text.position_z
            text.scale.z = config.marker.text.scale_z
            text.color = ColorRGBA(r=config.marker.text.color.r, g=config.marker.text.color.g, b=config.marker.text.color.b, a=config.marker.text.color.a, )
            text.text = waypoint['name']
            text.lifetime = Duration(seconds=config.marker.lifetime_seconds).to_msg()
            markers.markers.append(text)
        self.waypoints_marker_pub.publish(markers)

    def save_waypoint_callback(self, request, response):
        name = request.name.strip().lower()
        if not name:
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, 'Waypoint name is required')
        try:
            waypoint = self._lookup_current_waypoint(name)
        except TransformException as exc:
            self.log.warning(f'Failed to save waypoint {name!r}: {exc}')
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, f'Current pose unavailable: {exc}')
        self.waypoints[name] = waypoint
        if not self._save_waypoints():
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, 'Failed to persist waypoint')
        self._set_status(response, RosToolStatus.STATUS_SUCCESS, f'Saved waypoint {name}')
        response.waypoint = self._build_waypoint_msg(waypoint)
        self.publish_saved_waypoints()
        self.log.info(f'Saved waypoint {name!r}')
        return response

    def save_waypoint_at_pose_callback(self, request, response):
        """Persist a validated map-frame waypoint supplied by a trusted ROS node."""
        supplied = request.waypoint
        name = supplied.name.strip().lower()
        if not name:
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, 'Waypoint name is required')
        frame_id = supplied.frame_id.strip() or config.frames.map
        if frame_id != config.frames.map:
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, f'Waypoint pose must use frame {config.frames.map!r}')
        numeric_values = (supplied.x,
                          supplied.y,
                          supplied.orientation,
                          supplied.qx,
                          supplied.qy,
                          supplied.qz,
                          supplied.qw,)
        if not all(math.isfinite(float(value)) for value in numeric_values):
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, 'Waypoint pose contains a non-finite value')
        yaw_radians = math.radians(float(supplied.orientation))
        quaternion_norm = math.sqrt(float(supplied.qx) ** 2 + float(supplied.qy) ** 2 + float(supplied.qz) ** 2 + float(supplied.qw) ** 2)
        if quaternion_norm < 1e-6:
            qx = qy = 0.0
            qz = math.sin(yaw_radians / 2.0)
            qw = math.cos(yaw_radians / 2.0)
        else:
            qx = float(supplied.qx) / quaternion_norm
            qy = float(supplied.qy) / quaternion_norm
            qz = float(supplied.qz) / quaternion_norm
            qw = float(supplied.qw) / quaternion_norm
        waypoint = {
            'name': name,
            'x': float(supplied.x),
            'y': float(supplied.y),
            'orientation': float(supplied.orientation) % 360.0,
            'qx': qx,
            'qy': qy,
            'qz': qz,
            'qw': qw,
            'frame_id': frame_id, }
        self.waypoints[name] = waypoint
        if not self._save_waypoints():
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, 'Failed to persist waypoint')
        self._set_status(response, RosToolStatus.STATUS_SUCCESS, f'Saved waypoint {name}')
        response.waypoint = self._build_waypoint_msg(waypoint)
        self.publish_saved_waypoints()
        self.log.info(f'Saved supplied waypoint {name!r} at '
                      f'({waypoint["x"]:.2f}, {waypoint["y"]:.2f})')
        return response

    def get_waypoint_callback(self, request, response):
        name = request.name.strip().lower()
        if not name:
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, 'Waypoint name is required')
        waypoint = self.waypoints.get(name)
        if waypoint is None:
            self.log.warning(f'Waypoint {name!r} was not found')
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, f'Waypoint {name!r} not found')
        self._set_status(response, RosToolStatus.STATUS_SUCCESS, f'Returned waypoint {name}')
        response.waypoint = self._build_waypoint_msg(waypoint)
        self.log.info(f'Returned waypoint {name!r}')
        return response

    def list_waypoints_callback(self, _request, response):
        self._set_status(response, RosToolStatus.STATUS_SUCCESS, f'Returned {len(self.waypoints)} waypoint(s)')
        response.waypoints = [self._build_waypoint_msg(waypoint) for waypoint in self.waypoints.values()]
        self.log.info(response.status.message)
        return response

    def delete_waypoint_callback(self, request, response):
        name = request.name.strip().lower()
        if not name:
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, 'Waypoint name is required')
        if name not in self.waypoints:
            self.log.warning(f'Cannot delete missing waypoint {name!r}')
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, f'Waypoint {name!r} not found')
        del self.waypoints[name]
        if not self._save_waypoints():
            return self._set_status(response, RosToolStatus.STATUS_FAILURE, 'Failed to persist waypoint deletion')
        self._set_status(response, RosToolStatus.STATUS_SUCCESS, f'Deleted waypoint {name}')
        response.name = name
        self.publish_saved_waypoints()
        self.log.info(f'Deleted waypoint {name!r}')
        return response

    @staticmethod
    def _set_status(response, status_code: int, message: str):
        response.status.status = status_code
        response.status.message = message
        return response


def main(args=None):
    rclpy.init(args=args)
    node = WaypointNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        try:
            rclpy.shutdown()
        except Exception:
            pass


if __name__ == '__main__':
    main()

import json
import math
import time

import rclpy
from geometry_msgs.msg import PointStamped
from person_tracking_node.tracker import Observation, PersonTracker, TrackerSettings
from rclpy.duration import Duration
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from robot_common import get_config, get_logger
from std_msgs.msg import String
from tf2_geometry_msgs import do_transform_point
from tf2_ros import Buffer, TransformListener
from visualization_msgs.msg import Marker, MarkerArray

config = get_config('person_tracking_node')


def _person_name(detection):
    explicit = str(detection.get('face_name') or '').strip()
    if explicit and explicit.casefold() != 'unknown':
        return explicit
    label = str(detection.get('label') or '').strip()
    if label.casefold().startswith('person (') and label.endswith(')'):
        return label[len('person ('):-1].strip()
    return ''


class PersonTrackingNode(Node):

    def __init__(self):
        super().__init__('person_tracking_node')
        self.log = get_logger()
        self.tracking_frame = str(config.frames.tracking)
        self.output_frame = str(config.frames.output)
        self.tracker = PersonTracker(TrackerSettings(visible_seconds=float(config.timing.visible_seconds), source_binding_seconds=float(config.timing.source_binding_seconds), spatial_reacquire_seconds=float(config.timing.spatial_reacquire_seconds), navigation_max_age_seconds=float(config.timing.navigation_max_age_seconds), track_expiry_seconds=float(config.timing.track_expiry_seconds), anonymous_track_expiry_seconds=float(config.timing.anonymous_track_expiry_seconds), base_gate_m=float(config.association.base_gate_m), gate_growth_m_per_second=float(config.association.gate_growth_m_per_second), max_gate_m=float(config.association.max_gate_m), ambiguity_margin_m=float(config.association.ambiguity_margin_m), direct_track_max_jump_m=float(config.association.direct_track_max_jump_m), measurement_sigma_m=float(config.filter.measurement_sigma_m), acceleration_sigma_mps2=float(config.filter.acceleration_sigma_mps2), navigation_max_stddev_m=float(config.filter.navigation_max_stddev_m), anonymous_observations=int(config.confirmation.anonymous_observations), ))
        self.show_anonymous_visible = bool(config.visualization.show_anonymous_visible)
        self.marker_radius_m = float(config.visualization.marker_radius_m)
        self.text_height_m = float(config.visualization.text_height_m)
        self.marker_lifetime = Duration(seconds=float(config.visualization.marker_lifetime_seconds)).to_msg()
        self.tf_buffer = Buffer(cache_time=Duration(seconds=30.0))
        self.tf_listener = TransformListener(self.tf_buffer, self)
        detection_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST)
        output_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.track_pub = self.create_publisher(String, str(config.topics.tracks), output_qos)
        self.marker_pub = self.create_publisher(MarkerArray, str(config.topics.markers), output_qos)
        self.detection_sub = self.create_subscription(String, str(config.topics.detections), self._detections_callback, detection_qos)
        self.timer = self.create_timer(float(config.timing.publish_period_seconds), self._publish)
        self._published_marker_ids = set()
        self._clear_markers_on_next_publish = True
        self._last_tf_warning = 0.0
        self.log.info(f'Person tracking ready: {config.topics.detections} -> '
                      f'{config.topics.tracks} ({self.tracking_frame} -> {self.output_frame})')

    def _detections_callback(self, message: String):
        try:
            envelope = json.loads(message.data)
            header = envelope.get('header') or {}
            stamp = float(header.get('stamp', 0.0))
            frame_id = str(header.get('frame_id') or 'base_link')
            detections = envelope.get('detections') or []
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if stamp <= 0.0:
            return
        try:
            transform = self.tf_buffer.lookup_transform(self.tracking_frame, frame_id, rclpy.time.Time(nanoseconds=int(stamp * 1e9)), timeout=Duration(seconds=0.15))
        except Exception as exc:
            self._warn_tf(f'{frame_id} -> {self.tracking_frame}: {exc}')
            return
        observations = []
        for detection in detections:
            label = str(detection.get('label') or '').strip()
            if not label.casefold().startswith('person'):
                continue
            source_track_id = detection.get('track_id')
            position = detection.get('position_3d') or {}
            if source_track_id is None:
                continue
            point = PointStamped()
            point.header.frame_id = frame_id
            point.header.stamp = rclpy.time.Time(nanoseconds=int(stamp * 1e9)).to_msg()
            try:
                point.point.x = float(position['x'])
                point.point.y = float(position['y'])
                point.point.z = float(position.get('z', 0.0))
                transformed = do_transform_point(point, transform)
            except (KeyError, TypeError, ValueError):
                continue
            face_distance = detection.get('face_distance', detection.get('face_confidence'))
            try:
                face_distance = float(face_distance) if face_distance is not None else None
            except (TypeError, ValueError):
                face_distance = None
            observations.append(Observation(timestamp=stamp, source='head_camera', source_track_id=int(source_track_id), x=float(transformed.point.x), y=float(transformed.point.y), z=float(transformed.point.z), confidence=float(detection.get('confidence', 0.0)), face_name=_person_name(detection), face_distance=face_distance, ))
        if observations:
            self.tracker.update(observations, stamp)

    def _publish(self):
        now_msg = self.get_clock().now()
        now = now_msg.nanoseconds / 1e9
        snapshots = self.tracker.snapshots(now)
        try:
            transform = self.tf_buffer.lookup_transform(self.output_frame, self.tracking_frame, rclpy.time.Time(), timeout=Duration(seconds=0.1))
        except Exception as exc:
            self._warn_tf(f'{self.tracking_frame} -> {self.output_frame}: {exc}')
            return
        yaw = self._yaw_from_quaternion(transform.transform.rotation)
        cos_yaw = math.cos(yaw)
        sin_yaw = math.sin(yaw)
        output_tracks = []
        for snapshot in snapshots:
            point = PointStamped()
            point.header.frame_id = self.tracking_frame
            point.point.x = snapshot.x
            point.point.y = snapshot.y
            point.point.z = snapshot.z
            mapped = do_transform_point(point, transform)
            velocity_x = snapshot.velocity_x * cos_yaw - snapshot.velocity_y * sin_yaw
            velocity_y = snapshot.velocity_x * sin_yaw + snapshot.velocity_y * cos_yaw
            output_tracks.append({
                'track_id': snapshot.track_id,
                'item_name': f'person_{snapshot.track_id}',
                'face_name': snapshot.face_name or None,
                'position': {'x': float(mapped.point.x), 'y': float(mapped.point.y), 'z': float(mapped.point.z), },
                'velocity': {'x': float(velocity_x), 'y': float(velocity_y)},
                'last_seen': snapshot.last_seen,
                'age_seconds': round(snapshot.age_seconds, 3),
                'state': snapshot.state,
                'source': snapshot.source,
                'face_distance': snapshot.face_distance,
                'position_stddev': round(snapshot.position_stddev, 3),
                'navigation_eligible': snapshot.navigation_eligible, })
        track_message = String()
        track_message.data = json.dumps({
            'header': {'stamp': now, 'frame_id': self.output_frame},
            'tracks': output_tracks, })
        self.track_pub.publish(track_message)
        self.marker_pub.publish(self._markers(output_tracks, now_msg.to_msg()))

    def _markers(self, tracks, stamp):
        markers = MarkerArray()
        current_ids = set()
        if self._clear_markers_on_next_publish:
            clear = Marker()
            clear.header.frame_id = self.output_frame
            clear.header.stamp = stamp
            clear.action = Marker.DELETEALL
            markers.markers.append(clear)
            self._clear_markers_on_next_publish = False
        for track in tracks:
            named = bool(track.get('face_name'))
            if not named and not self.show_anonymous_visible:
                continue
            base_id = int(track['track_id']) * 3
            if named:
                current_ids.update((base_id, base_id + 1, base_id + 2))
            else:
                current_ids.add(base_id + 1)
            position = track['position']
            color = self._state_color(track['state'])
            uncertainty = max(0.2, float(track['position_stddev']) * 2.0)
            if named:
                area = Marker()
                area.header.frame_id = self.output_frame
                area.header.stamp = stamp
                area.ns = 'tracked_people_uncertainty'
                area.id = base_id
                area.type = Marker.CYLINDER
                area.action = Marker.ADD
                area.pose.position.x = float(position['x'])
                area.pose.position.y = float(position['y'])
                area.pose.position.z = 0.01
                area.pose.orientation.w = 1.0
                area.scale.x = uncertainty
                area.scale.y = uncertainty
                area.scale.z = 0.02
                area.color.r, area.color.g, area.color.b = color
                area.color.a = 0.2
                area.lifetime = self.marker_lifetime
                markers.markers.append(area)
            body = Marker()
            body.header.frame_id = self.output_frame
            body.header.stamp = stamp
            body.ns = 'tracked_people'
            body.id = base_id + 1
            body.type = Marker.SPHERE
            body.action = Marker.ADD
            body.pose.position.x = float(position['x'])
            body.pose.position.y = float(position['y'])
            body.pose.position.z = self.marker_radius_m
            body.pose.orientation.w = 1.0
            diameter = self.marker_radius_m * 2.0
            body.scale.x = diameter
            body.scale.y = diameter
            body.scale.z = diameter
            body.color.r, body.color.g, body.color.b = color
            body.color.a = 0.9
            body.lifetime = self.marker_lifetime
            markers.markers.append(body)
            if named:
                text = Marker()
                text.header.frame_id = self.output_frame
                text.header.stamp = stamp
                text.ns = 'tracked_people_text'
                text.id = base_id + 2
                text.type = Marker.TEXT_VIEW_FACING
                text.action = Marker.ADD
                text.pose.position.x = float(position['x'])
                text.pose.position.y = float(position['y'])
                text.pose.position.z = (2.0 * self.marker_radius_m + 0.75 * self.text_height_m)
                text.pose.orientation.w = 1.0
                text.scale.z = self.text_height_m
                # RViz centers the complete string on the marker. The former
                # "name · state · age" label therefore put the name far to the
                # left. Keep the centered map label to the identity; marker
                # color already communicates whether it is visible/last-known.
                text.text = str(track['face_name'])
                text.color.r = 0.25
                text.color.g = 0.05
                text.color.b = 0.75
                text.color.a = 1.0
                text.lifetime = self.marker_lifetime
                markers.markers.append(text)
        for marker_id in sorted(self._published_marker_ids - current_ids):
            marker = Marker()
            marker.header.frame_id = self.output_frame
            marker.header.stamp = stamp
            marker.id = marker_id
            marker.action = Marker.DELETE
            marker.ns = (
                'tracked_people_uncertainty' if marker_id % 3 == 0 else 'tracked_people' if marker_id % 3 == 1 else 'tracked_people_text')
            markers.markers.append(marker)
        self._published_marker_ids = current_ids
        return markers

    @staticmethod
    def _state_color(state):
        if state == 'visible':
            return 0.1, 0.9, 0.2
        if state == 'ambiguous':
            return 0.55, 0.55, 0.55
        return 1.0, 0.55, 0.05

    @staticmethod
    def _yaw_from_quaternion(quaternion):
        return math.atan2(2.0 * (quaternion.w * quaternion.z + quaternion.x * quaternion.y), 1.0 - 2.0 * (
                    quaternion.y * quaternion.y + quaternion.z * quaternion.z))

    def _warn_tf(self, message):
        now = time.monotonic()
        if now - self._last_tf_warning >= 2.0:
            self.log.warning(f'Person tracking TF unavailable: {message}')
            self._last_tf_warning = now


def main(args=None):
    rclpy.init(args=args)
    node = PersonTrackingNode()
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.remove_node(node)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()

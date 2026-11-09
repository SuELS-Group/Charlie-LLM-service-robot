import numpy as np
import rclpy
from geometry_msgs.msg import PointStamped
from rclpy.duration import Duration
from tf2_geometry_msgs import do_transform_point


def transform_point(node, xyz, from_frame: str, to_frame: str, timeout_s: float = 0.3):
    pt = PointStamped()
    pt.header.frame_id = from_frame
    pt.header.stamp = node.get_clock().now().to_msg()
    pt.point.x = float(xyz[0])
    pt.point.y = float(xyz[1])
    pt.point.z = float(xyz[2])
    try:
        transform = node.tf_buffer.lookup_transform(to_frame, from_frame, rclpy.time.Time(), timeout=Duration(seconds=float(timeout_s)), )
        out = do_transform_point(pt, transform)
    except Exception:
        return None
    return np.array([out.point.x, out.point.y, out.point.z], dtype=np.float32)

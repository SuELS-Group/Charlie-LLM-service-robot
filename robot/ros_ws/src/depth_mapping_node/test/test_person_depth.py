import json
import threading
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

import depth_mapping_node.depth_mapping_node as node_module
import numpy as np
import pytest
from depth_mapping_node.depth_mapping_node import DepthMappingNode
from geometry_msgs.msg import TransformStamped
from sensor_msgs.msg import Image
from std_msgs.msg import String


def test_camera_info_refreshes_intrinsics_after_profile_change():
    node = object.__new__(DepthMappingNode)
    node.lock = threading.Lock()
    node.buffers = {'head': {'info': None}}
    node.cameras = {'head': {'frame_override': 'head_color_optical_frame'}}
    node.frames = {}
    node.intrinsics = {'head': {'fx': 1.0, 'fy': 1.0, 'ppx': 1.0, 'ppy': 1.0, 'width': 640, 'height': 480}, }
    node.log = Mock()
    msg = SimpleNamespace(width=1280, height=720, k=[900.0,
                                                     0.0,
                                                     640.0,
                                                     0.0,
                                                     900.0,
                                                     360.0,
                                                     0.0,
                                                     0.0,
                                                     1.0], header=SimpleNamespace(frame_id='camera_frame'), )

    node.on_info('head', msg)

    assert node.intrinsics['head']['width'] == 1280
    assert node.intrinsics['head']['height'] == 720
    assert node.intrinsics['head']['fx'] == 900.0
    node.log.info.assert_called_once()


def test_person_depth_uses_segmentation_instead_of_background():
    depth = np.full((20, 20), 5000, dtype=np.uint16)
    depth[5:15, 5:15] = 2000
    intrinsics = {'fx': 100.0, 'fy': 100.0, 'ppx': 10.0, 'ppy': 10.0, }
    segmentation = {'points': [[5, 5], [14, 5], [14, 14], [5, 14]]}

    point = DepthMappingNode._segmentation_camera_point(depth, intrinsics, segmentation, 0.05, 10.0)

    assert point is not None
    assert point[2] == pytest.approx(2.0)
    assert abs(point[0]) < 0.05
    assert abs(point[1]) < 0.05


def test_person_depth_rejects_values_outside_sensor_range():
    depth = np.full((10, 10), 9000, dtype=np.uint16)
    intrinsics = {'fx': 100.0, 'fy': 100.0, 'ppx': 5.0, 'ppy': 5.0}
    segmentation = {'points': [[1, 1], [8, 1], [8, 8], [1, 8]]}

    point = DepthMappingNode._segmentation_camera_point(depth, intrinsics, segmentation, 0.05, 3.5)

    assert point is None


def test_pointing_depth_sampling_prefers_foreground_arm(monkeypatch):
    monkeypatch.setattr(node_module.config, 'pointing_depth', SimpleNamespace(sample_radius_pixels=5, min_depth_m=0.2, max_depth_m=8.0, ), )
    depth = np.full((15, 15), 5000, dtype=np.uint16)
    depth[4:11, 4:11] = 2000

    sampled = DepthMappingNode._sample_pointing_depth(depth, 7.0, 7.0)

    assert sampled == pytest.approx(2.0)


def test_pointing_depth_request_returns_only_small_map_points(monkeypatch):
    monkeypatch.setattr(node_module.config, 'pointing_depth', SimpleNamespace(camera='head', target_frame='map', slop_seconds=0.25, sample_radius_pixels=1, min_depth_m=0.2, max_depth_m=8.0, ), )
    depth_array = np.full((4, 4), 2000, dtype=np.uint16)
    depth = Image()
    depth.header.stamp.sec = 10
    depth.header.frame_id = 'head_color_optical_frame'
    depth.height = 4
    depth.width = 4
    depth.encoding = '16UC1'
    depth.step = 8
    depth.data = depth_array.tobytes()

    transform = TransformStamped()
    transform.header.frame_id = 'map'
    transform.child_frame_id = 'head_color_optical_frame'
    transform.transform.rotation.w = 1.0

    node = object.__new__(DepthMappingNode)
    node.lock = threading.Lock()
    node.buffers = {'head': {'depth': deque([(10.0, depth)]), 'info': None, }, }
    node.intrinsics = {'head': {'fx': 100.0, 'fy': 100.0, 'ppx': 2.0, 'ppy': 2.0, 'width': 4, 'height': 4, }, }
    node.frames = {'head': 'head_color_optical_frame'}
    node.tf_buffer = Mock()
    node.tf_buffer.lookup_transform.return_value = transform
    node.pointing_depth_pub = Mock()

    request = String()
    request.data = json.dumps({
        'request_id': 42,
        'header': {'stamp': 10.0},
        'image': {'width': 4, 'height': 4},
        'points': {'elbow': {'x': 2.0, 'y': 2.0}}, })

    node._on_pointing_depth_request(request)

    response_message = (node.pointing_depth_pub.publish.call_args.args[0])
    response = json.loads(response_message.data)
    assert response['request_id'] == 42
    assert response['outcome'] == 'success'
    assert response['frame_id'] == 'map'
    assert response['points']['elbow'] == pytest.approx({'x': 0.0, 'y': 0.0, 'z': 2.0, })

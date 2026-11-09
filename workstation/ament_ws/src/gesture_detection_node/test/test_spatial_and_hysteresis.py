import threading
from types import SimpleNamespace
from unittest.mock import Mock

import gesture_detection_node.gesture_detection_node as node_module
import numpy as np
import pytest
from gesture_detection_node.gesture_detection_node import (GestureDetectionNode, OccupancyMap, PointingSample, )
from gesture_detection_node.gesture_processor import PointingObservation
from nav_msgs.msg import OccupancyGrid
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String

from waypoint_interfaces.srv import SavePointedWaypoint


def _map_config(clearance=0.1):
    return SimpleNamespace(frames=SimpleNamespace(map='map'), map=SimpleNamespace(occupied_threshold=65, waypoint_clearance_m=clearance, ), )


def _occupancy_message(*, width=12, height=10, resolution=0.5):
    message = OccupancyGrid()
    message.header.frame_id = 'map'
    message.info.width = width
    message.info.height = height
    message.info.resolution = resolution
    message.info.origin.orientation.w = 1.0
    occupancy = np.zeros((height, width), dtype=np.int8)
    occupancy[[0, -1], :] = 100
    occupancy[:, [0, -1]] = 100
    message.data = occupancy.reshape(-1).tolist()
    return message, occupancy


def test_ray_is_capped_before_mapped_wall():
    message, occupancy = _occupancy_message()
    occupancy[1:-1, 7] = 100
    message.data = occupancy.reshape(-1).tolist()
    map_model = OccupancyMap(message, _map_config())
    bounded, wall_hit = map_model.bound_ray(np.asarray([1.25, 1.25]), np.asarray([5.0,
                                                                                  1.25]), wall_clearance_m=0.25, origin_ignore_m=0.1, )
    assert wall_hit is True
    assert bounded[0] < 3.5
    assert bounded[1] == pytest.approx(1.25)


def test_waypoint_is_selected_from_robot_connected_free_space():
    message, _ = _occupancy_message()
    map_model = OccupancyMap(message, _map_config())
    waypoint = map_model.reachable_waypoint(np.asarray([4.25, 2.25]), np.asarray([1.25,
                                                                                  1.25]), start_search_m=0.6, target_search_m=0.6, )
    assert waypoint is not None
    assert waypoint == pytest.approx([4.25, 2.25], abs=0.36)


def _service_only_node(sample, *, person_count, detection_time):
    node = object.__new__(GestureDetectionNode)
    node.log = Mock()
    node.state_lock = threading.RLock()
    node.last_pointing_sample = sample
    node.person_count = person_count
    node.person_bbox = None
    node.last_person_detection_monotonic = detection_time
    node.occupancy_map = None
    return node


def test_color_reader_survives_gating_changes_without_destruction(monkeypatch, ):
    monkeypatch.setattr(node_module.time, 'monotonic', lambda: 10.0)
    node = object.__new__(GestureDetectionNode)
    node.state_lock = threading.RLock()
    node.gesture_armed = False
    node.person_count = 0
    node.person_bbox = None
    node.last_person_detection_monotonic = 0.0
    node.image_subscription = None
    node.image_qos = object()
    node.processing_group = object()
    subscriptions = [object()]
    node.create_subscription = Mock(side_effect=subscriptions)
    node.destroy_subscription = Mock()
    node._refresh_sensor_subscriptions()
    assert node.create_subscription.call_count == 1
    node.person_count = 1
    node.person_bbox = {'center_x': 100.0, 'center_y': 100.0, 'width': 50.0, 'height': 100.0, }
    node.last_person_detection_monotonic = 9.9
    node._refresh_sensor_subscriptions()
    assert node.image_subscription is subscriptions[0]
    assert (node.create_subscription.call_args_list[0].kwargs['callback_group'] is node.processing_group)
    node.person_count = 2
    node._refresh_sensor_subscriptions()
    assert node.image_subscription is subscriptions[0]
    assert node.create_subscription.call_count == 1
    node.destroy_subscription.assert_not_called()


def test_pointing_requests_only_landmark_depth_from_robot():
    node = object.__new__(GestureDetectionNode)
    node.state_lock = threading.RLock()
    color = CompressedImage()
    color.header.stamp.sec = 11
    color.header.frame_id = 'head_color_optical_frame'
    node._pointing_gate = Mock(return_value=True)
    observation = PointingObservation(True, 0.9, {
        'shoulder': (300.0, 200.0),
        'elbow': (350.0, 250.0),
        'wrist': (400.0, 300.0),
        'index': (420.0, 320.0), }, )
    node.processor = SimpleNamespace(process_pointing=Mock(return_value=observation))
    node.pending_pointing_request = None
    node.next_pointing_request_id = 7
    node.depth_request_publisher = Mock()
    node._reject_pointing = Mock()
    node._process_pointing(np.zeros((480, 640, 3), dtype=np.uint8), {
        'center_x': 320.0,
        'center_y': 240.0,
        'width': 200.0,
        'height': 400.0, }, color, 10.0, )
    published = node.depth_request_publisher.publish.call_args.args[0]
    payload = node_module.json.loads(published.data)
    assert payload['request_id'] == 7
    assert payload['header']['stamp'] == pytest.approx(11.0)
    assert payload['image'] == {'width': 640, 'height': 480}
    assert payload['points']['wrist'] == {'x': 400.0, 'y': 300.0}
    assert node.pending_pointing_request.observation is observation


def test_robot_depth_response_populates_stable_pointing_sample():
    node = object.__new__(GestureDetectionNode)
    node.state_lock = threading.RLock()
    observation = PointingObservation(True, 0.9, {
        'shoulder': (300.0, 200.0),
        'elbow': (350.0, 250.0),
        'wrist': (400.0, 300.0),
        'index': (420.0, 320.0), }, )
    node.pending_pointing_request = node_module.PendingPointingRequest(request_id=9, requested_monotonic=9.9, observation=observation, )
    node.occupancy_map = object()
    node.pointing_stable_count = 1
    node.last_pointing_diagnostic_monotonic = 10.0
    node.last_pointing_sample = None
    node.log = Mock()
    node._pointing_gate = Mock(return_value=True)
    node._publish_markers = Mock()
    ground = np.asarray([2.0, 1.0, 0.0])
    bounded = np.asarray([1.8, 1.0, 0.0])
    direction = np.asarray([0.8, 0.0, -0.6])
    node._project_right_arm = Mock(return_value=(ground, bounded, direction, True))
    response = String()
    response.data = node_module.json.dumps({
        'request_id': 9,
        'outcome': 'success',
        'frame_id': 'map',
        'points': {'elbow': {'x': 1.0, 'y': 1.0, 'z': 1.0}, 'wrist': {'x': 1.5, 'y': 1.0, 'z': 0.5}, }, })
    node.pointing_depth_response_callback(response)
    assert node.pending_pointing_request is None
    assert node.last_pointing_sample is not None
    assert node.last_pointing_sample.wall_hit is True
    assert node.last_pointing_sample.bounded_intersection == pytest.approx(bounded)


def test_save_request_reports_no_person_without_recorded_point(monkeypatch):
    monkeypatch.setattr(node_module.time, 'monotonic', lambda: 10.0)
    node = _service_only_node(None, person_count=0, detection_time=9.9)
    request = SavePointedWaypoint.Request()
    request.name = 'there'
    request.max_pointing_age_seconds = 2.0
    response = node.save_pointed_waypoint_callback(request, SavePointedWaypoint.Response())
    assert response.outcome == 'no_person'


def test_save_request_rejects_point_older_than_two_seconds(monkeypatch):
    monkeypatch.setattr(node_module.time, 'monotonic', lambda: 10.0)
    sample = PointingSample(received_monotonic=7.9, ground_intersection=np.asarray([2.0,
                                                                                    2.0,
                                                                                    0.0]), bounded_intersection=np.asarray([2.0,
                                                                                                                            2.0,
                                                                                                                            0.0]), direction=np.asarray([1.0,
                                                                                                                                                         0.0,
                                                                                                                                                         -0.2]), arm_points={}, wall_hit=False, confidence=0.9, )
    node = _service_only_node(sample, person_count=1, detection_time=9.9)
    request = SavePointedWaypoint.Request()
    request.name = 'there'
    request.max_pointing_age_seconds = 10.0
    response = node.save_pointed_waypoint_callback(request, SavePointedWaypoint.Response())
    assert response.outcome == 'expired_hysteresis'
    assert response.pointing_age_seconds == pytest.approx(2.1)


def test_cached_point_is_not_used_when_multiple_people_are_visible(monkeypatch, ):
    monkeypatch.setattr(node_module.time, 'monotonic', lambda: 10.0)
    sample = PointingSample(received_monotonic=9.5, ground_intersection=np.asarray([2.0,
                                                                                    2.0,
                                                                                    0.0]), bounded_intersection=np.asarray([2.0,
                                                                                                                            2.0,
                                                                                                                            0.0]), direction=np.asarray([1.0,
                                                                                                                                                         0.0,
                                                                                                                                                         -0.2]), arm_points={}, wall_hit=False, confidence=0.9, )
    node = _service_only_node(sample, person_count=2, detection_time=9.9)
    request = SavePointedWaypoint.Request()
    request.name = 'there'
    request.max_pointing_age_seconds = 2.0
    response = node.save_pointed_waypoint_callback(request, SavePointedWaypoint.Response())
    assert response.outcome == 'multiple_people'

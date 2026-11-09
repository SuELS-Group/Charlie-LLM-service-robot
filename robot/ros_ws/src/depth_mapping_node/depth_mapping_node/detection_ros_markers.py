#!/usr/bin/env python3
import cv2
import numpy as np
from rclpy.duration import Duration
from visualization_msgs.msg import Marker, MarkerArray


class DetectionBoxMarker:

    def __init__(self, detection_box_id, marker_base_name):
        self.detection_box_id = detection_box_id
        colormap = cv2.COLORMAP_HSV
        offset = 0
        i = (offset + (self.detection_box_id * 29)) % 255
        image = np.uint8([[[i]]])
        id_color_image = cv2.applyColorMap(image, colormap)
        bgr = id_color_image[0, 0]
        self.id_color = [bgr[2], bgr[1], bgr[0]]
        self.frame_id = 'camera_color_optical_frame'
        self.name = marker_base_name
        self.marker = Marker()
        self.marker.type = self.marker.CUBE
        self.marker.action = self.marker.ADD
        self.lifetime_s = 2.0
        duration = Duration(seconds=self.lifetime_s)
        self.marker.lifetime = duration.to_msg()
        self.marker.text = self.name
        self.frame_number = None
        self.timestamp = None
        self.ready = False
        self.box_3d = None
        self.detection_box_width_m = None
        self.detection_box_height_m = None

    def update(self, detection_3d, timestamp, frame_number):
        self.timestamp = timestamp
        self.frame_number = frame_number
        self.box_3d = detection_3d['box_3d']
        if self.box_3d is not None:
            self.marker_position = self.box_3d['center_xyz']
            self.marker_quaternion = self.box_3d['quaternion']
            self.detection_box_width_m = self.box_3d['width_m']
            self.detection_box_height_m = self.box_3d['height_m']
        self.ready = True

    def get_ros_marker(self):
        if (not self.ready) or (self.box_3d is None):
            return None
        self.marker.header.frame_id = self.frame_id
        self.marker.header.stamp = self.timestamp
        self.marker.id = self.detection_box_id
        self.marker.scale.x = self.detection_box_width_m
        self.marker.scale.y = self.detection_box_height_m
        self.marker.scale.z = 0.01
        den = float(np.max(self.id_color))
        self.marker.color.r = self.id_color[2] / den
        self.marker.color.g = self.id_color[1] / den
        self.marker.color.b = self.id_color[0] / den
        self.marker.color.a = 0.5
        self.marker.pose.position.x = float(self.marker_position[0])
        self.marker.pose.position.y = float(self.marker_position[1])
        self.marker.pose.position.z = float(self.marker_position[2])
        q = self.marker_quaternion
        self.marker.pose.orientation.x = q[0]
        self.marker.pose.orientation.y = q[1]
        self.marker.pose.orientation.z = q[2]
        self.marker.pose.orientation.w = q[3]
        return self.marker


class DetectionBoxMarkerCollection:

    def __init__(self, default_marker_base_name='detection_box'):
        self.collection = {}
        self.frame_number = 0
        self.default_marker_base_name = default_marker_base_name

    def __iter__(self):
        keys = self.collection.keys()
        for k in keys:
            marker = self.collection[k]
            if marker.frame_number == self.frame_number:
                yield marker

    def update(self, detections_3d, timestamp=None):
        self.frame_number += 1
        self.timestamp = timestamp
        self.collection.clear()
        self.detection_box_id = 0
        for detection_3d in detections_3d:
            box_3d = detection_3d['box_3d']
            label = detection_3d['label']
            if box_3d is not None:
                self.detection_box_id += 1
                if label is None:
                    marker_label = self.default_marker_base_name
                else:
                    marker_label = label
                new_marker = DetectionBoxMarker(self.detection_box_id, marker_label)
                self.collection[self.detection_box_id] = new_marker
                self.collection[self.detection_box_id].update(detection_3d, self.timestamp, self.frame_number)

    def get_ros_marker_array(self):
        marker_array = MarkerArray()
        for key in self.collection:
            marker = self.collection[key]
            if marker.frame_number == self.frame_number:
                ros_marker = marker.get_ros_marker()
                if ros_marker is not None:
                    marker_array.markers.append(ros_marker)
        return marker_array

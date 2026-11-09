#!/usr/bin/env python3
import numpy as np
from scipy.spatial.transform import Rotation

from .numba_image_to_pointcloud import numba_image_to_pointcloud


def filter_points(points_array, camera_matrix, box_2d, min_box_side_m, max_box_side_m):
    f_x = camera_matrix[0, 0]
    f_y = camera_matrix[1, 1]
    x0, y0, x1, y1 = box_2d
    detection_box_width_pix = y1 - y0
    detection_box_height_pix = x1 - x0
    if detection_box_width_pix == 0:
        detection_box_width_pix = 1
    if detection_box_height_pix == 0:
        detection_box_height_pix = 1
    z_min = 0.0
    z_max = 10.0
    if min_box_side_m and max_box_side_m:
        z_min = min_box_side_m * min(f_x / detection_box_width_pix, f_y / detection_box_height_pix)
        z_max = max_box_side_m * max(f_x / detection_box_width_pix, f_y / detection_box_height_pix)
    z = points_array[:, 2]
    mask_z = (z > z_min) & (z < z_max)
    remaining_z = z[mask_z]
    out_points = np.empty((0, 3), dtype=np.float32)
    if len(remaining_z) > 0:
        if max_box_side_m:
            median_z = np.median(remaining_z)
            min_z = median_z - max_box_side_m
            max_z = median_z + max_box_side_m
            mask_z = (z > min_z) & (z < max_z)
            remaining_z = z[mask_z]
        if len(remaining_z) > 0:
            out_points = points_array[mask_z]
    return out_points


def bounding_box_2d_to_3d(points_array, box_2d, camera_matrix):
    x0, y0, x1, y1 = box_2d
    f_x = camera_matrix[0, 0]
    c_x = camera_matrix[0, 2]
    f_y = camera_matrix[1, 1]
    c_y = camera_matrix[1, 2]
    detection_box_width_pix = y1 - y0
    detection_box_height_pix = x1 - x0
    num_points = points_array.shape[0]
    if num_points >= 1:
        box_depth = np.median(points_array, axis=0)[2]
    else:
        return None
    center_x_pix = (x0 + x1) / 2.0
    center_y_pix = (y0 + y1) / 2.0
    center_z = box_depth
    center_x = ((center_x_pix - c_x) / f_x) * center_z
    center_y = ((center_y_pix - c_y) / f_y) * center_z
    detection_box_width_m = (detection_box_width_pix / f_x) * box_depth
    detection_box_height_m = (detection_box_height_pix / f_y) * box_depth
    rotvec = np.array([0.0, 0.0, 1.0]) * (-np.pi / 2.0)
    R = Rotation.from_rotvec(rotvec).as_matrix()
    quaternion = Rotation.from_matrix(R).as_quat()
    box_3d = {
        'center_xyz': (center_x, center_y, center_z),
        'quaternion': quaternion,
        'width_m': detection_box_width_m,
        'height_m': detection_box_height_m, }
    return box_3d


def detections_2d_to_3d(detections_2d, camera_info, depth_image, min_box_side_m=None, max_box_side_m=None):
    orig_h, orig_w = depth_image.shape

    def clip_xy(x_in, y_in):
        x_out = max(0, min(orig_w - 1, x_in))
        y_out = max(0, min(orig_h - 1, y_in))
        return x_out, y_out

    camera_matrix = np.reshape(camera_info.k, (3, 3))
    detections_3d = []
    for h in detections_2d:
        box_3d = None
        box_2d = h.get('box')
        label = h.get('label')
        if box_2d is not None:
            x0, y0, x1, y1 = box_2d
            x0, y0 = clip_xy(x0, y0)
            x1, y1 = clip_xy(x1, y1)
            if x0 >= x1 or y0 >= y1:
                box_2d = None
            else:
                box_2d = (x0, y0, x1, y1)
        if box_2d is not None:
            points_3d = numba_image_to_pointcloud(depth_image, box_2d, camera_matrix)
            if (min_box_side_m is not None) and (max_box_side_m is not None):
                points_3d = filter_points(points_3d, camera_matrix, box_2d, min_box_side_m, max_box_side_m)
            box_3d = bounding_box_2d_to_3d(points_3d, box_2d, camera_matrix)
        detections_3d.append({'box_3d': box_3d, 'box_2d': box_2d, 'label': label})
    return detections_3d

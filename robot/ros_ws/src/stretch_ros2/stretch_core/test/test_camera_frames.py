"""Check camera connectivity without changing the robot's calibration."""
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from stretch_core.camera_frames import normalize_camera_frames


def test_calibrated_model_keeps_geometry_and_connects_project_cameras():
    path = Path(__file__).resolve().parents[2] / 'stretch_description/urdf/stretch.urdf'
    source = ET.parse(path).getroot()
    result = ET.fromstring(normalize_camera_frames(path.read_text()))
    parents = {j.find('child').get('link'): j.find('parent').get('link')
               for j in result.findall('joint')}
    for frame in ('head_color_optical_frame', 'gripper_color_optical_frame'):
        visited = set()
        while frame != 'base_link':
            assert frame not in visited
            visited.add(frame)
            frame = parents[frame]
    # All elements, numerical calibration values, joint names and assets survive.
    for before, after in zip(source.iter(), result.iter(), strict=True):
        assert before.tag == after.tag
        expected = dict(before.attrib)
        attr = 'name' if before.tag == 'link' else 'link' if before.tag in ('parent', 'child') else None
        if attr:
            value = expected[attr]
            if value.startswith('gripper_camera_'):
                expected[attr] = 'gripper_' + value[len('gripper_camera_'):]
            elif value.startswith('camera_'):
                expected[attr] = 'head_' + value[len('camera_'):]
        assert after.attrib == expected
    normalized = ET.tostring(result, encoding='unicode')
    assert normalize_camera_frames(normalized) == normalized


def test_rejects_ambiguous_duplicate_frames():
    with pytest.raises(ValueError, match='collide'):
        normalize_camera_frames('<robot><link name="camera_link"/><link name="head_link"/></robot>')

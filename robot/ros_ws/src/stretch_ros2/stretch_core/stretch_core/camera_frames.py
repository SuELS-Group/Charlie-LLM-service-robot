"""Adapt calibrated upstream camera links to this project's camera names."""

import xml.etree.ElementTree as ET


def normalize_camera_frames(description: str) -> str:
    """Rename links only, preserving calibration, joint names and mesh paths."""
    root = ET.fromstring(description)
    names = {link.attrib['name'] for link in root.findall('link')}
    mapping = {}
    for name in names:
        if name.startswith('gripper_camera_'):
            mapping[name] = 'gripper_' + name[len('gripper_camera_'):]
        elif name.startswith('camera_'):
            mapping[name] = 'head_' + name[len('camera_'):]
    for old, new in mapping.items():
        if new in names:
            raise ValueError(f'Camera frame rename would collide: {old} -> {new}')
    for link in root.findall('link'):
        link.set('name', mapping.get(link.attrib['name'], link.attrib['name']))
    for joint in root.findall('joint'):
        for tag in ('parent', 'child'):
            element = joint.find(tag)
            if element is not None:
                name = element.attrib['link']
                element.set('link', mapping.get(name, name))
    return ET.tostring(root, encoding='unicode')

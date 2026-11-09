from glob import glob

from setuptools import setup

package_name = 'camera_node'
setup(name=package_name, version='0.0.0', packages=[package_name], data_files=[(
    'share/ament_index/resource_index/packages',
    ['resource/' + package_name]),
    ('share/' + package_name, ['package.xml']),
    ('share/' + package_name + '/config', glob('config/*.yaml')),
    ('share/' + package_name + '/launch',
     glob('launch/*.py')), ], install_requires=['setuptools'], zip_safe=True, maintainer='pg-llm-service-robot', maintainer_email='pg-llm-service-robot@uni-oldenburg.de', description='Camera node providing image and camera info topics from the robot cameras.', license='AGPL-3.0-only', entry_points={
    'console_scripts': ['camera_node = camera_node.camera_node:main', ], }, )

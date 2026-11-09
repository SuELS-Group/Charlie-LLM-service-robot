from glob import glob

from setuptools import setup

package_name = 'vision_node'
setup(name=package_name, version='0.0.0', packages=[package_name], data_files=[(
    'share/ament_index/resource_index/packages',
    ['resource/' + package_name]),
    ('share/' + package_name, ['package.xml']),
    ('share/' + package_name + '/config',
     glob('config/*.yaml')), ], install_requires=['setuptools'], zip_safe=True, maintainer='pg-llm-service-robot', maintainer_email='pg-llm-service-robot@uni-oldenburg.de', description='Vision node for object detection, ArUco markers, and door handles.', license='AGPL-3.0-only', entry_points={
    'console_scripts': ['vision_node = vision_node.vision_node:main',
                        'greeting_mode_node = vision_node.greeting_mode_node:main', ], }, )

from glob import glob

from setuptools import setup

package_name = 'command_node'
setup(name=package_name, version='0.0.0', packages=[package_name], data_files=[(
    'share/ament_index/resource_index/packages',
    ['resource/' + package_name]),
    ('share/' + package_name, ['package.xml']),
    ('share/' + package_name + '/config', glob('config/*.yaml')),
    ('share/' + package_name + '/launch', glob('launch/*.launch.py')),
    ('share/' + package_name + '/config',
     glob('config/*.rviz')), ], install_requires=['setuptools'], tests_require=[
    'pytest'], zip_safe=True, maintainer='pg-llm-service-robot', maintainer_email='pg-llm-service-robot@uni-oldenburg.de', description='Node that translates LLM commands into robot actions and reports robot status.', license='AGPL-3.0-only', entry_points={
    'console_scripts': ['main = command_node.command_node:main',
                        'save_map_timer = command_node.save_map_timer:main',
                        'slam_mode_manager = command_node.slam_mode_manager:main', ], }, )

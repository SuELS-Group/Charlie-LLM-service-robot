from glob import glob

from setuptools import find_packages, setup

package_name = 'action_service'
setup(name=package_name, version='0.0.0', packages=find_packages(), data_files=[(
    'share/ament_index/resource_index/packages',
    ['resource/' + package_name]),
    ('share/' + package_name, ['package.xml']),
    ('share/' + package_name + '/config', glob('config/*.yaml')), ], install_requires=['setuptools'], tests_require=[
    'pytest'], zip_safe=True, maintainer='pg-llm-service-robot', maintainer_email='pg-llm-service-robot@uni-oldenburg.de', description='High-level action service providing commands like find, pick and deliver for the robot.', license='AGPL-3.0-only', entry_points={
    'console_scripts': ['action_service_node = action_service.action_service_node:main', ], }, )

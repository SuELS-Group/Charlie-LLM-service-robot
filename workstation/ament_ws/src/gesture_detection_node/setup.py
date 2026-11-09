from glob import glob

from setuptools import setup

package_name = 'gesture_detection_node'
setup(name=package_name, version='0.1.0', packages=[package_name], data_files=[(
    'share/ament_index/resource_index/packages',
    ['resource/' + package_name],),
    ('share/' + package_name, ['package.xml']),
    ('share/' + package_name + '/config', glob('config/*.yaml')),
    ('share/' + package_name + '/config', glob('config/*.task')), ], install_requires=['setuptools'], tests_require=[
    'pytest'], zip_safe=True, maintainer='pg-llm-service-robot', maintainer_email='pg-llm-service-robot@uni-oldenburg.de', description=(
    'Unified workstation MediaPipe hand and right-arm pointing detector.'), license='AGPL-3.0-only', entry_points={
    'console_scripts': [('gesture_detection_node = '
                         'gesture_detection_node.gesture_detection_node:main'), ], }, )

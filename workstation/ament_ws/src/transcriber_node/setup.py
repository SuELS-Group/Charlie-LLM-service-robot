from glob import glob

from setuptools import setup

package_name = 'transcriber_node'
setup(name=package_name, version='0.0.0', packages=[package_name], data_files=[(
    'share/ament_index/resource_index/packages',
    ['resource/' + package_name]),
    ('share/' + package_name, ['package.xml']),
    ('share/' + package_name + '/config',
     glob('config/*.yaml')), ], install_requires=['setuptools'], zip_safe=True, maintainer='pg-llm-service-robot', maintainer_email='pg-llm-service-robot@uni-oldenburg.de', description='Node for converting audio streams into text transcripts for higher-level processing.', license='AGPL-3.0-only', entry_points={
    'console_scripts': ['transcriber_node = transcriber_node.transcriber_node:main', ], }, )

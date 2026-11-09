from glob import glob

from setuptools import find_packages, setup

package_name = 'stretch_driver'
setup(name=package_name, version='0.0.0', packages=find_packages(exclude=['test']), data_files=[(
    'share/ament_index/resource_index/packages',
    ['resource/' + package_name]),
    ('share/' + package_name + '/launch', glob('launch/*.py')),
    ('share/' + package_name,
     ['package.xml']), ], install_requires=['setuptools'], zip_safe=True, maintainer='pg-llm-service-robot', maintainer_email='pg-llm-service-robot@uni-oldenburg.de', description='Launch and driver helpers for the Stretch Robot hardware integration.', license='AGPL-3.0-only', )

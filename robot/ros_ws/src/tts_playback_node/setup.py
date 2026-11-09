from glob import glob

from setuptools import setup

package_name = 'tts_playback_node'
setup(name=package_name, version='0.0.0', packages=[package_name], data_files=[(
    'share/ament_index/resource_index/packages',
    ['resource/' + package_name]),
    ('share/' + package_name, ['package.xml']),
    ('share/' + package_name, ['feedback-sound.mp3']),
    ('share/' + package_name, ['thinking-sound.mp3']),
    ('share/' + package_name + '/config', glob('config/*.yaml')), ], install_requires=['setuptools',
                                                                                       'soundfile'], zip_safe=True, maintainer='pg-llm-service-robot', maintainer_email='pg-llm-service-robot@uni-oldenburg.de', description='Plays back TTS audio on the robot speaker hardware.', license='AGPL-3.0-only', entry_points={
    'console_scripts': ['tts_playback_node = tts_playback_node.tts_playback_node:main', ], }, )

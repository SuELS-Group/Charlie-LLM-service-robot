import sys
from pathlib import Path

root = Path(__file__).resolve().parents[3]
for path in [root / 'workstation', root / 'robot',
             root / 'workstation/ament_ws/src/tts_synthesis_node',
             root / 'workstation/ament_ws/src/transcriber_node',
             root / 'robot/ros_ws/src/tts_playback_node']:
    sys.path.insert(0, str(path))

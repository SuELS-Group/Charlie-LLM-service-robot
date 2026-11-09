import sys
from pathlib import Path

robot_root = Path(__file__).resolve().parents[4]
if str(robot_root) not in sys.path:
    sys.path.insert(0, str(robot_root))

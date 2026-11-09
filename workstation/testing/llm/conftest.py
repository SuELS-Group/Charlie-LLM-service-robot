import sys
import pytest
from pathlib import Path
from unittest.mock import patch

for directory in (Path(__file__).resolve().parents[2], Path(__file__).resolve().parents[2] / 'llm_node'):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

@pytest.fixture(autouse=True)
def isolated_battery_return_marker(monkeypatch,tmp_path):
    import communication_node
    monkeypatch.setattr(communication_node.config,'battery_return_state_file',str(tmp_path/'battery-return.json'))
    monkeypatch.setattr(communication_node.config,'battery_announcement_settings_file',str(tmp_path/'battery-announcements.json'),raising=False)


def pytest_configure(config):
    config.option.asyncio_mode = "auto"
    # Load application configuration with a fixed profile; tests fake inference.
    with patch('workstation_common.config.detect_gpus', return_value=[
            {'index': 0, 'name': 'Test GPU', 'vram_mb': 16384}]):
        import llm_test_helpers  # noqa: F401 - installs ROS message stubs
        import agent  # noqa: F401
        import communication_node  # noqa: F401
        import user_context  # noqa: F401

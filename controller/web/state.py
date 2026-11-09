from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from discovery_server_manager import DiscoveryServerManager
from models import MachineConfig, NodeConfig, Settings

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
STATIC_DIR = BASE_DIR / "static"
GENERATED_DIR = BASE_DIR / "generated_services"
DIAGRAMS_DIR = BASE_DIR / "diagrams"
PDF_PATH = DIAGRAMS_DIR / "graph.pdf"
ROBOT_CONTROL_WS_PORT = 8766
ROBOT_CONTROL_WS_PATH = "/ws/robot-control"
VOICE_ACTIVITY_WS_PATH = "/ws/voice-activity"
WEB_BRIDGE_HTTP_PORT = 8767
LLM_INPUT_HTTP_PATH = "/api/llm-input"
machines_config: Dict[str, MachineConfig] = {}
nodes_config: Dict[str, List[NodeConfig]] = {}
settings: Settings = Settings()
background_tasks: set = set()
discovery_manager = DiscoveryServerManager(CONFIG_DIR)

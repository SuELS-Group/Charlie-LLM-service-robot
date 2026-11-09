from pathlib import Path

from workstation_common.config import load

config = load(Path(__file__).resolve().parent / 'config' / 'mcp_server.yaml')

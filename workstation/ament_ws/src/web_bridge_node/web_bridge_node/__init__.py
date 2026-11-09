def main():
    # Keep telemetry calculations importable without starting ROS imports.
    from .web_bridge_node import main as run
    return run()

__all__ = ['main']

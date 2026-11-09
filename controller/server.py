from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from web import state
from web.app import app


def configure_logging(log_dir: Path | None = None) -> Path:
    """Keep timestamped application events after the controller terminal closes."""
    log_dir = log_dir or Path(__file__).parent / "config" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "controller-events.log"
    root = logging.getLogger()
    if not any(getattr(handler, "_controller_events", False) for handler in root.handlers):
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s",
                                      datefmt="%Y-%m-%dT%H:%M:%S%z")
        file_handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024,
                                          backupCount=3, encoding="utf-8")
        file_handler._controller_events = True
        # HTTP polling is already visible in the terminal; it must not crowd out
        # the connection and recovery events in the persistent log.
        file_handler.addFilter(lambda record: not record.name.startswith("uvicorn.access"))
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        if not any(isinstance(handler, logging.StreamHandler)
                   and not isinstance(handler, logging.FileHandler) for handler in root.handlers):
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            root.addHandler(console_handler)
    root.setLevel(logging.INFO)
    # AsyncSSH logs every status-poll command at INFO. Our connection observer
    # records loss reasons without retaining command output or auth material.
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    return log_file


if __name__ == "__main__":
    import uvicorn

    configure_logging()
    state.STATIC_DIR.mkdir(exist_ok=True)
    print("Starting ROS2 Robot Controller Server...")
    try:
        uvicorn.run(app, host="0.0.0.0", port=8080, reload=False, log_level="info")
    finally:
        state.discovery_manager.shutdown()

from __future__ import annotations

import logging
import os
import sys
from threading import Lock
from typing import Any

_LOCK = Lock()
_CONFIGURED = False
_CONFIG_STREAM = None
_LOGGER_CACHE: dict[str, 'WorkstationLogger'] = {}
_LEVEL_NAMES = {
    logging.DEBUG: 'DEBUG',
    logging.INFO: 'INFO',
    logging.WARNING: 'WARN',
    logging.ERROR: 'ERROR',
    logging.CRITICAL: 'FATAL', }


def _coerce_level(level: int | str | None) -> int:
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        value = getattr(logging, level.upper(), None)
        if isinstance(value, int):
            return value
    env_level = getattr(logging, os.getenv('WORKSTATION_LOG_LEVEL', 'INFO').upper(), None)
    return env_level if isinstance(env_level, int) else logging.INFO


def _clean_message(message: Any) -> str:
    return str(message).strip().replace('\n', ' | ')


def _coerce_stream(stream):
    if stream is None:
        return None
    if stream == 'stdout':
        return sys.stdout
    if stream == 'stderr':
        return sys.stderr
    return stream


class _Formatter(logging.Formatter):

    def format(self, record: logging.LogRecord) -> str:
        level = _LEVEL_NAMES.get(record.levelno, record.levelname.upper())
        component = str(getattr(record, 'component', '') or '').strip()
        message = _clean_message(record.getMessage())
        line = f'{level:<5} {message}' if not component else f'{level:<5} {component:<18} {message}'
        if record.exc_info:
            return f'{line}\n{self.formatException(record.exc_info)}'
        return line


class WorkstationLogger(logging.LoggerAdapter):

    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        extra = kwargs.setdefault('extra', {})
        extra.setdefault('component', self.extra['component'])
        return msg, kwargs

    def sub(self, component: str = '') -> 'WorkstationLogger':
        child = str(component or '').strip()
        if not child:
            return self
        base = str(self.extra.get('component', '') or '').strip()
        return get_logger(f'{base}.{child}' if base else child)


def configure_logging(level: int | str | None = None, stream=None) -> None:
    global _CONFIGURED, _CONFIG_STREAM
    with _LOCK:
        target_level = _coerce_level(level)
        root = logging.getLogger()
        target_stream = _coerce_stream(stream)
        if _CONFIGURED:
            root.setLevel(target_level)
            if target_stream is not None and target_stream is not _CONFIG_STREAM:
                for handler in root.handlers:
                    if isinstance(handler, logging.StreamHandler):
                        handler.setStream(target_stream)
                _CONFIG_STREAM = target_stream
            return
        handler = logging.StreamHandler(target_stream or sys.stdout)
        handler.setLevel(target_level)
        handler.setFormatter(_Formatter())
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(target_level)
        for logger_name in ('asyncio', 'httpcore', 'httpx', 'openai', 'urllib3'):
            logging.getLogger(logger_name).setLevel(logging.WARNING)
        _CONFIGURED = True
        _CONFIG_STREAM = target_stream or sys.stdout


def get_logger(component: str = '') -> WorkstationLogger:
    configure_logging()
    name = str(component or '').strip()
    logger = _LOGGER_CACHE.get(name)
    if logger is None:
        logger = WorkstationLogger(logging.getLogger('workstation'), {'component': name})
        _LOGGER_CACHE[name] = logger
    return logger

from pathlib import Path
from types import SimpleNamespace

import yaml


class _ConfigNamespace(SimpleNamespace):

    @property
    def meta(self):
        return self._meta


def _to_namespace(value):
    if isinstance(value, dict):
        items = {key: _to_namespace(item) for key, item in value.items()}
        if all(isinstance(key, str) and key.isidentifier() for key in items):
            return _ConfigNamespace(**items)
        return items
    if isinstance(value, list):
        return [_to_namespace(item) for item in value]
    return value


def load(path: str | Path):
    config_path = Path(path)
    with config_path.open('r', encoding='utf-8') as stream:
        data = yaml.safe_load(stream) or {}
    config = _to_namespace(data)
    object.__setattr__(config, '_meta', _ConfigNamespace())
    return config


def get_config(package_name: str, config_name: str | None = None):
    from ament_index_python.packages import get_package_share_directory

    file_name = f'{config_name or package_name}.yaml'
    path = Path(get_package_share_directory(package_name)) / 'config' / file_name
    return load(path)

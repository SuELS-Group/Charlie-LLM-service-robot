import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import yaml

from .logger import get_logger


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


class GPUDetectionError(RuntimeError):
    """GPU discovery failed; the available VRAM is unknown, not zero."""


def detect_gpus() -> list[dict]:
    executable = shutil.which('nvidia-smi')
    if executable is None:
        raise GPUDetectionError('GPU discovery failed: nvidia-smi was not found in PATH. '
                                'Check the NVIDIA driver installation and service PATH.')
    try:
        result = subprocess.run([executable,
                                 '--query-gpu=index,name,memory.total',
                                 '--format=csv,noheader,nounits', ], capture_output=True, check=True, text=True, timeout=5, )
    except subprocess.TimeoutExpired as exc:
        raise GPUDetectionError('GPU discovery failed: nvidia-smi did not respond within 5 seconds. '
                                'Check NVIDIA driver health before restarting the service.') from exc
    except subprocess.CalledProcessError as exc:
        details = '\n'.join(part.strip() for part in (exc.stdout, exc.stderr) if part and part.strip())
        hint = 'Check NVIDIA driver health and GPU device access.'
        if 'driver/library version mismatch' in details.lower():
            hint = ('The loaded NVIDIA kernel driver and installed libraries have different versions. '
                    'Save ongoing work and reboot the workstation to load the matching driver, '
                    'then verify nvidia-smi before restarting the service.')
        raise GPUDetectionError(f'GPU discovery failed: nvidia-smi exited with code {exc.returncode}. '
                                f'{details or "No diagnostic output."} {hint}') from exc
    except OSError as exc:
        raise GPUDetectionError(f'GPU discovery failed: cannot execute nvidia-smi: {exc}') from exc
    gpus = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(',')]
        if len(parts) < 3:
            continue
        try:
            gpus.append({'index': int(parts[0]), 'name': parts[1], 'vram_mb': int(float(parts[2])), })
        except ValueError:
            continue
    if not gpus:
        raise GPUDetectionError('GPU discovery failed: nvidia-smi returned no usable GPU memory information. '
                                'Check nvidia-smi output and NVIDIA device access.')
    return gpus


def total_vram_gb(gpus: list[dict]) -> float:
    return sum(gpu['vram_mb'] for gpu in gpus) / 1024.0


def select_profile(profiles: dict, total_vram: float) -> tuple[float | None, dict]:
    selected_threshold = None
    selected_profile = {}
    for threshold, profile in sorted((float(key), value) for key, value in profiles.items() if isinstance(value, dict)):
        if total_vram >= threshold:
            selected_threshold = threshold
            selected_profile = profile
        else:
            break
    return selected_threshold, selected_profile


def _deep_merge(base, override):
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override
    merged = dict(base)
    for key, value in override.items():
        if key in merged:
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load(path: str | Path, profile_key: str | None = None):
    config_path = Path(path)
    with config_path.open('r', encoding='utf-8') as stream:
        data = yaml.safe_load(stream) or {}
    meta = {}
    if profile_key and isinstance(data.get(profile_key), dict):
        try:
            gpus = detect_gpus()
        except GPUDetectionError as exc:
            raise GPUDetectionError(f'Cannot select GPU model profile for {config_path}: {exc}') from exc
        total_vram = total_vram_gb(gpus)
        thresholds = sorted(float(key) for key, value in data[profile_key].items() if isinstance(value, dict))
        minimum_threshold_gb = thresholds[0] if thresholds else None
        if minimum_threshold_gb is not None and total_vram < minimum_threshold_gb:
            raise RuntimeError(f'Config {config_path} requires at least {minimum_threshold_gb:.1f} GiB total VRAM, '
                               f'but detected {total_vram:.1f} GiB')
        selected_threshold, selected = select_profile(data[profile_key], total_vram)
        if selected_threshold is None:
            raise RuntimeError(f'Config {config_path} has no valid profile for {total_vram:.1f} GiB total VRAM')
        data = _deep_merge(data, selected)
        meta = {
            'gpus': gpus,
            'total_vram_gb': round(total_vram, 1),
            'selected_threshold_gb': selected_threshold,
            'minimum_threshold_gb': minimum_threshold_gb,
            'available_thresholds_gb': thresholds, }
    config = _to_namespace(data)
    object.__setattr__(config, '_meta', _to_namespace(meta))
    return config


def get_config(package_name: str, config_name: str | None = None, profile_key: str | None = None):
    from ament_index_python.packages import get_package_share_directory

    file_name = f'{config_name or package_name}.yaml'
    path = Path(get_package_share_directory(package_name)) / 'config' / file_name
    return load(path, profile_key=profile_key)


def log_summary(config, logger=None, *keys: str) -> None:
    if not vars(config.meta):
        return
    log = logger or get_logger('config')
    total_vram = getattr(config.meta, 'total_vram_gb', 0.0)
    minimum_threshold_gb = getattr(config.meta, 'minimum_threshold_gb', None)
    selected_threshold_gb = getattr(config.meta, 'selected_threshold_gb', None)
    available_thresholds_gb = getattr(config.meta, 'available_thresholds_gb', [])
    log.info(f'Total VRAM: {total_vram} GiB '
             f'(available profile mins: {available_thresholds_gb})')
    if minimum_threshold_gb is not None and total_vram < minimum_threshold_gb:
        log.warning(f'Detected {total_vram:.1f} GiB total VRAM, '
                    f'minimum profile threshold is {minimum_threshold_gb:.1f} GiB')
    if selected_threshold_gb is None:
        log.info('Selected profile: base')
    else:
        log.info(f'Selected profile: >= {selected_threshold_gb:.1f} GiB')
    for key in keys:
        value = config
        for part in key.split('.'):
            value = getattr(value, part)
        log.info(f'{key} = {value}')

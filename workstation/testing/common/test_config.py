"""GPU profile selection and discovery diagnostics without GPU/model access."""
import subprocess
from unittest.mock import Mock

import pytest

from workstation_common import config


@pytest.fixture
def smi(monkeypatch):
    monkeypatch.setattr(config.shutil, 'which', lambda _: '/usr/bin/nvidia-smi')
    run = Mock(return_value=subprocess.CompletedProcess([], 0, '0, Test GPU, 24576\n', ''))
    monkeypatch.setattr(config.subprocess, 'run', run)
    return run


def test_gpu_discovery_parses_multiple_devices_and_keeps_timeout(smi):
    smi.return_value.stdout = '0, GPU A, 24576\n1, GPU B, 24576\n'
    gpus = config.detect_gpus()
    assert [gpu['name'] for gpu in gpus] == ['GPU A', 'GPU B']
    assert config.total_vram_gb(gpus) == 48.0
    assert smi.call_args.kwargs['timeout'] == 5


def test_missing_nvidia_smi_is_not_reported_as_zero_vram(monkeypatch):
    monkeypatch.setattr(config.shutil, 'which', lambda _: None)
    with pytest.raises(config.GPUDetectionError, match='not found in PATH'):
        config.detect_gpus()


@pytest.mark.parametrize(('error', 'expected'), [
    (subprocess.CalledProcessError(9, 'nvidia-smi', stderr='Cannot communicate with driver'), 'Cannot communicate with driver'),
    (subprocess.TimeoutExpired('nvidia-smi', 5), 'within 5 seconds'),
])
def test_other_gpu_discovery_failures_are_actionable(smi, error, expected):
    smi.side_effect = error
    with pytest.raises(config.GPUDetectionError, match=expected):
        config.detect_gpus()


def test_real_low_memory_still_enforces_minimum_profile(smi, tmp_path):
    smi.return_value.stdout = '0, Small GPU, 8192\n'
    path = tmp_path / 'profile.yaml'
    path.write_text('model_profiles:\n  16:\n    model: test\n')
    with pytest.raises(RuntimeError, match='requires at least 16.0 GiB total VRAM, but detected 8.0 GiB'):
        config.load(path, profile_key='model_profiles')


def test_config_without_gpu_profiles_does_not_require_gpu(smi, tmp_path):
    smi.side_effect = AssertionError('GPU discovery should not run')
    path = tmp_path / 'cpu.yaml'
    path.write_text('enabled: true\n')
    assert config.load(path, profile_key='model_profiles').enabled is True
    smi.assert_not_called()

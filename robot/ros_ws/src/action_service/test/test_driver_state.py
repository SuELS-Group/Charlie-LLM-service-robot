import json
from types import SimpleNamespace

import pytest
from robot_common.driver_state import DriverState
from action_service.action_service_node import ActionServiceNode
from test_recovery_readiness import recovery_node, read


def sample(stamp=100., **kwargs):
    return dict(sampled_at=stamp, driver_started_at=90., mode='navigation',
        runstop_active=False, arm_service_active=False, streaming_position_active=False,
        motion_idle=True, **kwargs)


def test_initial_state_and_missing_new_samples_cannot_prove_stillness():
    cache = DriverState()
    assert cache.snapshot(now=100., monotonic=10.) == {'fresh': False, 'stationary_for_seconds': 0.}
    assert cache.update(sample(), now=100., monotonic=10.)
    # Wall-clock delay without new evidence doesn't count as observed idle time.
    assert cache.snapshot(now=100.8, monotonic=10.8)['stationary_for_seconds'] == 0.
    assert not cache.snapshot(now=102., monotonic=12.)['fresh']


def test_ordered_fresh_samples_establish_idle_duration():
    cache = DriverState()
    for delta in (0., .3, .6):
        assert cache.update(sample(100. + delta), now=100. + delta, monotonic=10. + delta)
    state = cache.snapshot(now=100.6, monotonic=10.6)
    assert state['fresh'] and state['stationary_for_seconds'] == pytest.approx(.6)
    assert not cache.update(sample(100.1), now=100.6, monotonic=10.6)
    assert cache.snapshot(now=100.6, monotonic=10.6) == state


@pytest.mark.parametrize('field,value', [('runstop_active', True), ('arm_service_active', True),
    ('streaming_position_active', True), ('motion_idle', False), ('mode', 'homing'),
    ('mode', 'gamepad'), ('driver_started_at', 100.2)])
def test_interruption_or_new_driver_resets_idle_duration(field, value):
    cache = DriverState()
    cache.update(sample(), now=100., monotonic=10.)
    value2 = sample(100.6)
    value2[field] = value
    cache.update(value2, now=100.6, monotonic=10.6)
    assert cache.snapshot(now=100.6, monotonic=10.6)['stationary_for_seconds'] == 0.


@pytest.mark.parametrize('fault', ['old', 'future', 'nan', 'bool_stamp', 'missing_field', 'not_bool'])
def test_invalid_snapshot_cannot_create_evidence(fault):
    cache = DriverState()
    value = sample()
    if fault == 'old': value['sampled_at'] = 95.
    if fault == 'future': value['sampled_at'] = 101.
    if fault == 'nan': value['driver_started_at'] = float('nan')
    if fault == 'bool_stamp': value['driver_started_at'] = True
    if fault == 'missing_field': value.pop('motion_idle')
    if fault == 'not_bool': value['runstop_active'] = 'false'
    assert not cache.update(value, now=100., monotonic=10.)
    assert not cache.snapshot(now=100., monotonic=10.)['fresh']


def test_action_service_callback_adds_diagnostics_to_read_without_changing_possession(monkeypatch):
    n = recovery_node()
    before = n.object_state.snapshot()
    import robot_common.driver_state as module
    monkeypatch.setattr(module.time, 'time', lambda: 100.)
    ActionServiceNode._driver_state_callback(n, SimpleNamespace(data=json.dumps(sample())))
    result = read(n)
    assert result['driver_state']['fresh'] and not result['driver_state']['runstop_active']
    assert n.object_state.snapshot() == before
    ActionServiceNode._driver_state_callback(n, SimpleNamespace(data='not JSON'))
    assert read(n)['driver_state'] == result['driver_state']


def test_large_telemetry_gap_and_monotonic_gap_do_not_count_as_stationary():
    for capture_gap, receive_gap in ((2., .6), (.6, 2.)):
        cache = DriverState()
        cache.update(sample(), now=100., monotonic=10.)
        cache.update(sample(100. + capture_gap), now=100. + capture_gap, monotonic=10. + receive_gap)
        assert cache.snapshot(now=100. + capture_gap, monotonic=10. + receive_gap)['stationary_for_seconds'] == 0.

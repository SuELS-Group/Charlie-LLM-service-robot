from types import SimpleNamespace

import mcp_server.tools.navigation as navigation
import pytest


def _status(code, message):
    return SimpleNamespace(status=code, message=message)


def test_save_pointed_waypoint_uses_two_second_hysteresis(monkeypatch):
    captured = {}

    def call_service(name, service_type, request, timeout_sec):
        captured.update({'name': name, 'service_type': service_type, 'request': request, 'timeout_sec': timeout_sec, })
        return SimpleNamespace(status=_status(navigation.STATUS_SUCCESS, 'saved'), outcome='wall_hit', message='Saved after clipping at a wall.', waypoint=SimpleNamespace(name='table', x=1.25, y=2.5, frame_id='map', ), wall_hit=True, used_hysteresis=True, pointing_age_seconds=1.8, )

    monkeypatch.setattr(navigation, 'call_service', call_service)
    result = navigation.save_pointed_waypoint('table')
    assert result['status'] == 'success'
    assert result['result']['pointing_status'] == 'wall_hit'
    assert result['result']['wall_hit'] is True
    assert captured['name'] == '/gesture_detection/save_pointed_waypoint'
    assert captured['request'].max_pointing_age_seconds == pytest.approx(2.0)


def test_save_pointed_waypoint_exposes_expired_status(monkeypatch):
    monkeypatch.setattr(navigation, 'call_service', lambda *_args, **_kwargs: SimpleNamespace(status=_status(navigation.STATUS_SUCCESS + 1, 'too old'), outcome='expired_hysteresis', message='The last pointing location is too old.', waypoint=SimpleNamespace(), wall_hit=False, used_hysteresis=False, pointing_age_seconds=2.1, ), )
    result = navigation.save_pointed_waypoint()
    assert result['status'] == 'failure'
    assert result['error']['code'] == 'EXPIRED_HYSTERESIS'
    assert result['error']['pointing_status'] == 'expired_hysteresis'

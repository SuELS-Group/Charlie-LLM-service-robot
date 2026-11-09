import json
from unittest.mock import patch

import pytest

from workstation_common.battery_announcements import BatteryAnnouncements, BatteryAnnouncementSettings


def test_default_reduced_warns_once_per_level_and_ignores_healthy_status():
    policy = BatteryAnnouncements()
    assert policy.mode == 'reduced'
    assert policy.choose('green') is None
    assert policy.choose('yellow') == 'yellow'
    assert policy.choose('green') is None
    assert policy.choose('yellow') is None
    assert policy.choose('orange') == 'orange'
    assert policy.choose('yellow') is None
    assert policy.choose('red') == 'red'
    assert policy.choose('red') is None
    assert policy.choose('unknown') is None


@pytest.mark.parametrize('mode', ['reduced', 'full', 'off'])
def test_charging_is_silent_and_rearms_next_discharge(mode):
    policy = BatteryAnnouncements()
    policy.choose('yellow')
    policy.set_mode(mode)
    for level in ['green', 'yellow', 'orange', 'red']:
        assert policy.choose(level, connected=True) is None
    policy.set_mode('reduced')
    assert policy.choose('yellow') == 'yellow'


def test_off_suppresses_every_level_and_full_retains_rate_limit():
    clock = [0.]
    policy = BatteryAnnouncements(clock=lambda: clock[0])
    policy.set_mode('off')
    for level in ['green', 'yellow', 'orange', 'red']:
        assert policy.choose(level) is None
    policy.set_mode('full')
    assert policy.choose('green') == 'green'
    assert policy.choose('yellow') is None
    clock[0] = 30.
    assert policy.choose('yellow') == 'yellow'


@pytest.mark.parametrize('mode', ['reduced', 'full', 'off'])
def test_setting_survives_recreation(tmp_path, mode):
    path = tmp_path / 'settings.json'
    settings = BatteryAnnouncementSettings(path)
    assert settings.mode == 'reduced'
    settings.save(mode)
    assert BatteryAnnouncementSettings(path).mode == mode
    assert json.loads(path.read_text()) == {'mode': mode}


@pytest.mark.parametrize('invalid', ['silent', '', None, True, [], {}])
def test_invalid_mode_cannot_replace_saved_setting(tmp_path, invalid):
    settings = BatteryAnnouncementSettings(tmp_path / 'settings.json')
    settings.save('off')
    with pytest.raises(ValueError):
        settings.save(invalid)
    with pytest.raises(ValueError):
        BatteryAnnouncements().set_mode(invalid)
    assert settings.mode == BatteryAnnouncementSettings(settings.path).mode == 'off'


def test_failed_save_keeps_previous_mode_and_file(tmp_path):
    settings = BatteryAnnouncementSettings(tmp_path / 'settings.json')
    settings.save('off')
    with patch('workstation_common.battery_announcements.os.replace', side_effect=OSError('read only')):
        with pytest.raises(OSError):
            settings.save('full')
    assert settings.mode == BatteryAnnouncementSettings(settings.path).mode == 'off'
    assert list(tmp_path.iterdir()) == [settings.path]


@pytest.mark.parametrize('contents', ['broken', 'null', '[]', '{}', '{"mode": "bad"}', '{"mode": []}'])
def test_unreadable_setting_defaults_to_reduced(tmp_path, contents):
    path = tmp_path / 'settings.json'
    path.write_text(contents)
    assert BatteryAnnouncementSettings(path).mode == 'reduced'

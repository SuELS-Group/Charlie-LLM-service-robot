import asyncio
import json
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi import Response
from models import MachineConfig, MachineType
from web import state
from web.routes import battery


class BatteryApiTests(unittest.TestCase):
    def read_response(self, payload):
        source = MagicMock()
        source.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with patch.object(battery, 'urlopen', return_value=source):
            return battery._read_battery('192.0.2.1')

    def test_source_timestamp_controls_freshness(self):
        for stamp in (time.time() - 10, time.time() + 60, True, None):
            with self.subTest(stamp=stamp):
                result = self.read_response({'type': 'battery', 'available': True, 'sampled_at': stamp, 'percentage': 80})
                self.assertFalse(result['available'])
                self.assertNotIn('percentage', result)


    def test_unknown_percentage_during_charging_is_preserved(self):
        payload = {'type': 'battery', 'available': True, 'sampled_at': time.time(),
                   'percentage': None, 'charger_connected': True, 'charging_state': 'charging'}
        self.assertEqual(self.read_response(payload), payload)

    def test_nonfinite_and_invalid_percentages_are_rejected(self):
        for percentage in (float('nan'), float('inf'), -1, 101, True, '80'):
            with self.subTest(percentage=percentage), self.assertRaises(ValueError):
                self.read_response({'type': 'battery', 'available': True, 'sampled_at': time.time(), 'percentage': percentage})
        with self.assertRaises(ValueError):
            self.read_response({'type': 'battery', 'available': True, 'sampled_at': time.time(), 'history': [float('nan')]})

    def test_only_matching_ros_domain_is_used(self):
        machines = {
            'robot': MachineConfig(name='Robot', host='192.0.2.1', type=MachineType.ROBOT, ros_domain_id=42),
            'other': MachineConfig(name='Other', host='192.0.2.2', type=MachineType.WORKSTATION, ros_domain_id=7),
            'workstation': MachineConfig(name='Workstation', host='192.0.2.3', type=MachineType.WORKSTATION, ros_domain_id=42)}
        with patch.dict(state.machines_config, machines, clear=True), patch.object(battery, '_read_battery', return_value={'type': 'battery', 'available': True}) as read:
            response = Response()
            result = asyncio.run(battery.get_battery('robot', response))
        read.assert_called_once_with('192.0.2.3')
        self.assertEqual(result['source_machine_id'], 'workstation')
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_ambiguous_or_missing_source_never_falls_back_to_another_robot(self):
        robot = MachineConfig(name='Robot', host='192.0.2.1', type=MachineType.ROBOT, ros_domain_id=42)
        workstation = MachineConfig(name='Workstation', host='192.0.2.3', type=MachineType.WORKSTATION, ros_domain_id=7)
        for sources in ({'robot': robot, 'workstation': workstation},
                        {'robot': robot, 'workstation': robot.model_copy(update={'type': MachineType.WORKSTATION}),
                         'second': robot.model_copy(update={'type': MachineType.WORKSTATION})}):
            with patch.dict(state.machines_config, sources, clear=True), patch.object(battery, '_read_battery') as read:
                result = asyncio.run(battery.get_battery('robot', Response()))
                self.assertFalse(result['available'])
                read.assert_not_called()

    def test_network_failure_returns_offline_without_stale_values(self):
        config = MachineConfig(name='Workstation', host='192.0.2.1', type=MachineType.WORKSTATION, ros_domain_id=42)
        with patch.dict(state.machines_config, {'workstation': config}, clear=True), patch.object(battery, '_read_battery', side_effect=TimeoutError):
            result = asyncio.run(battery.get_battery('workstation', Response()))
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'], 'bridge_unavailable')


    def test_mode_is_sent_to_bridge_and_confirmation_is_required(self):
        source = MagicMock()
        source.__enter__.return_value.read.return_value = b'{"mode":"off"}'
        with patch.object(battery, 'urlopen', return_value=source) as open_url:
            self.assertEqual(battery._write_announcement_mode('::1', 'off'), {'mode': 'off'})
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, 'http://[::1]:8767/api/battery/announcements')
        self.assertEqual(request.method, 'POST')
        self.assertEqual(json.loads(request.data), {'mode': 'off'})
        source.__enter__.return_value.read.return_value = b'{"mode":"reduced"}'
        with patch.object(battery, 'urlopen', return_value=source), self.assertRaises(ValueError):
            battery._write_announcement_mode('127.0.0.1', 'off')


if __name__ == '__main__':
    unittest.main()

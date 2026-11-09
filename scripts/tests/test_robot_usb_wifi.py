"""Exercise USB identity and network handover without touching host networking."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'configure_robot_usb_wifi.py'
spec = importlib.util.spec_from_file_location('robot_usb_wifi', SCRIPT)
wifi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wifi)
USB = 'wlx00e04c5d84d0'
MAC = '00:e0:4c:5d:84:d0'


class FakeNetwork:
    def __init__(self, dhcp_fails=False):
        self.dhcp_fails = dhcp_fails
        self.profiles = {
            'setup': {'connection.type': '802-3-ethernet', 'connection.interface-name': 'enp86s0',
                      'ipv4.method': 'manual', 'ipv4.addresses': '192.168.50.52/24'},
            'robot': {'connection.type': '802-11-wireless', 'connection.interface-name': USB,
                      'connection.autoconnect': 'no', '802-11-wireless.powersave': 'default'},
        }
        self.active = {'enp86s0': '192.168.50.52'}
        self.wifi_activations = 0

    def __call__(self, *command, timeout=30):
        if command[0] == 'nmcli':
            if '-g' in command:
                index = command.index('-g')
                self.assert_no_escaped_fields(command)
                return self.profiles[command[-1]].get(command[index + 1], '')
            if command[1:3] == ('connection', 'modify'):
                changes = command[4:]
                assert len(changes) % 2 == 0
                self.profiles[command[3]].update(zip(changes[::2], changes[1::2]))
                return ''
            if 'up' in command:
                profile = command[command.index('id') + 1]
                interface = command[-1]
                if profile == 'setup':
                    self.active.pop(interface, None)
                    if self.dhcp_fails:
                        raise RuntimeError('No DHCP lease without a setup cable')
                    self.active[interface] = '192.168.50.156'
                else:
                    # Fail the handover if Wi-Fi takes .52 while Ethernet still owns it.
                    assert '192.168.50.52' not in (address for dev, address in self.active.items() if dev != USB)
                    self.active[interface] = '192.168.50.52'
                    self.wifi_activations += 1
                return ''
            if command[1:3] == ('device', 'disconnect'):
                if command[3] not in self.active:
                    raise RuntimeError('Device is already disconnected')
                del self.active[command[3]]
                return ''
        if command[:5] == ('ip', '-j', '-4', 'address', 'show'):
            return json.dumps([{'ifname': dev, 'addr_info': [{'local': address}]}
                               for dev, address in self.active.items()])
        if command[:5] == ('ip', '-j', '-4', 'route', 'get'):
            return json.dumps([{'dev': USB, 'prefsrc': self.active.get(USB)}])
        raise AssertionError(f'Unexpected networking operation: {command}')

    @staticmethod
    def assert_no_escaped_fields(command):
        assert command[1:3] == ('--escape', 'no'), 'MAC addresses must not contain nmcli escape characters'


class AdapterTests(unittest.TestCase):
    def make_adapter(self, root, name, device_type):
        interface = root / 'net' / name
        (interface / 'wireless').mkdir(parents=True)
        device = root / 'devices' / device_type / 'adapter'
        device.mkdir(parents=True)
        (interface / 'device').symlink_to(device)
        (interface / 'address').write_text(MAC + '\n')

    def test_usb_wifi_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_adapter(root, USB, 'usb3')
            self.assertEqual(wifi.validate_adapter(USB, root / 'net'), MAC)

    def test_internal_wifi_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_adapter(root, 'wlo1', 'pci0000:00')
            with self.assertRaisesRegex(ValueError, 'external USB'):
                wifi.validate_adapter('wlo1', root / 'net')


class HandoverTests(unittest.TestCase):
    def args(self, directory):
        return argparse.Namespace(interface=USB, address='192.168.50.52', profile='robot',
                                  wired_profile='setup', wired_interface='enp86s0',
                                  backup_dir=str(Path(directory) / 'backup'))

    def test_handover_keeps_one_control_identity_and_persists_autoconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            network = FakeNetwork()
            with patch.object(wifi, 'run', network), patch.object(wifi, 'validate_adapter', return_value=MAC):
                report = wifi.install(self.args(directory))
            self.assertEqual(network.active, {'enp86s0': '192.168.50.156', USB: '192.168.50.52'})
            self.assertEqual(network.wifi_activations, 1)
            self.assertTrue(report['wifi_autoconnect'])
            self.assertFalse(report['wifi_powersave'])
            self.assertEqual(network.profiles['robot']['802-11-wireless.mac-address'], MAC)
            self.assertEqual(network.profiles['robot']['ipv4.routes'], '192.168.50.50/32')
            self.assertEqual(network.profiles['setup']['ipv4.method'], 'auto')
            self.assertEqual(network.profiles['setup']['ipv4.addresses'], '')
            before = json.loads((Path(directory) / 'backup' / 'before.json').read_text())
            self.assertEqual(before['setup']['ipv4.addresses'], '192.168.50.52/24')

    def test_missing_setup_cable_does_not_prevent_wifi_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            network = FakeNetwork(dhcp_fails=True)
            with patch.object(wifi, 'run', network), patch.object(wifi, 'validate_adapter', return_value=MAC):
                wifi.install(self.args(directory))
            self.assertEqual(network.active, {USB: '192.168.50.52'})
            self.assertEqual(network.wifi_activations, 1)

    def test_check_rejects_control_address_duplicated_on_ethernet(self):
        network = FakeNetwork()
        network.active[USB] = '192.168.50.52'
        with patch.object(wifi, 'run', network), patch.object(wifi, 'validate_adapter', return_value=MAC):
            with self.assertRaisesRegex(RuntimeError, 'must belong only'):
                wifi.inspect(self.args('/unused'))

    def test_install_rejects_internal_adapter_before_modifying_profiles(self):
        network = FakeNetwork()
        with patch.object(wifi, 'run', network), patch.object(wifi, 'validate_adapter', side_effect=ValueError('external USB required')):
            with self.assertRaisesRegex(ValueError, 'external USB'):
                wifi.install(self.args('/unused'))
        self.assertEqual(network.profiles['setup']['ipv4.method'], 'manual')
        self.assertEqual(network.profiles['robot']['connection.autoconnect'], 'no')


class DdsInterfacePolicyTests(unittest.TestCase):
    def test_dds_setup_rejects_robot_ip_on_ethernet_before_modifying_any_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / 'networkmanager-was-called'
            ip = root / 'ip'
            ip.write_text('#!/bin/sh\nprintf "2: setup-test inet 192.168.50.52/24 scope global setup-test\\n"\n')
            ip.chmod(0o755)
            nmcli = root / 'nmcli'
            nmcli.write_text('#!/bin/sh\ntouch "$NETWORK_TEST_MARKER"\n')
            nmcli.chmod(0o755)
            env = dict(os.environ, PATH=f'{root}:{os.environ.get("PATH", "")}', NETWORK_TEST_MARKER=str(marker))
            result = subprocess.run(['bash', str(SCRIPT.parent / 'configure_dds_network.bash'), '--install'],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('external USB Wi-Fi', result.stderr)
            self.assertFalse(marker.exists(), 'Rejected interface must not reach NetworkManager or sudo')


if __name__ == '__main__':
    unittest.main()

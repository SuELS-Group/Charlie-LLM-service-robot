"""Exercise RViz's effective environment without ROS processes or networking."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[2]


class RvizEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rviz-environment-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        scripts = self.root / 'scripts'
        scripts.mkdir()
        config = self.root / 'controller' / 'config'
        config.mkdir(parents=True)
        (config / 'dds_discovery_servers.json').write_text('{"port": 30123}\n')
        # Stub only the external ROS environment and TF overlay setup. The
        # copied launcher and final executable receive the actual environment.
        setup = self.root / 'ros_setup.bash'
        setup.write_text(':\n')
        launcher = (PROJECT / 'scripts' / 'start_rviz.sh').read_text()
        self.launcher = scripts / 'start_rviz.sh'
        self.launcher.write_text(launcher.replace('/opt/ros/humble/setup.bash', str(setup)))
        (scripts / 'ros_tf2_fix.bash').write_text('pg_source_tf2_fix() { :; }\n')
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        capture = self.root / 'capture.py'
        capture.write_text(
            'import json, os, sys\n'
            'print(json.dumps({"env": dict(os.environ), "args": sys.argv[1:]}))\n'
        )
        ros2 = bin_dir / 'ros2'
        ros2.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(capture))} "$@"\n')
        ros2.chmod(0o755)
        self.env = dict(os.environ, PATH=f'{bin_dir}:{os.environ.get("PATH", "")}')
        for variable in ('ROS_DISCOVERY_SERVER', 'ROS_DOMAIN_ID',
                         'FASTRTPS_DEFAULT_PROFILES_FILE', 'FASTDDS_DEFAULT_PROFILES_FILE'):
            self.env.pop(variable, None)

    def run_launcher(self, *args):
        result = subprocess.run(['bash', str(self.launcher), *args], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_diagnostic_profiles(self, output):
        expected = str(self.root / 'controller' / 'config' / 'fastdds_super_client_profiles.xml')
        self.assertEqual(output['env']['FASTRTPS_DEFAULT_PROFILES_FILE'], expected)
        self.assertEqual(output['env']['FASTDDS_DEFAULT_PROFILES_FILE'], expected)

    def test_defaults_load_diagnostic_profile_and_local_discovery(self):
        output = self.run_launcher()
        self.assert_diagnostic_profiles(output)
        self.assertEqual(output['env']['ROS_DISCOVERY_SERVER'], '127.0.0.1:30123')
        self.assertEqual(output['env']['ROS_DOMAIN_ID'], '42')
        self.assertEqual(output['env']['ROS_SUPER_CLIENT'], 'TRUE')

    def test_inherited_production_profiles_cannot_override_diagnostics(self):
        self.env.update(FASTRTPS_DEFAULT_PROFILES_FILE='/old/production.xml',
                        FASTDDS_DEFAULT_PROFILES_FILE='/new/production.xml',
                        ROS_DISCOVERY_SERVER='192.168.50.49:30000', ROS_DOMAIN_ID='43')
        output = self.run_launcher('--ros-args', '-p', 'example:=value with spaces')
        self.assert_diagnostic_profiles(output)
        self.assertEqual(output['env']['ROS_DISCOVERY_SERVER'], '192.168.50.49:30000')
        self.assertEqual(output['env']['ROS_DOMAIN_ID'], '43')
        self.assertEqual(output['args'][-3:], ['--ros-args', '-p', 'example:=value with spaces'])


if __name__ == '__main__':
    unittest.main()

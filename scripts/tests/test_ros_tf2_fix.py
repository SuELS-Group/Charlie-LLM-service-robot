"""Version selection and activation checks without ROS, network or hardware."""

from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'ros_tf2_fix.bash'


class Tf2OverlayTests(unittest.TestCase):
    def run_shell(self, version, root, body):
        return subprocess.run(
            ['bash', '-c', 'source "$1"\n'
             'test_version="$2"\ndpkg-query() { printf "%s" "$test_version"; }\n'
             + body, 'bash', str(SCRIPT), version, str(root)],
            text=True, capture_output=True, timeout=5)

    def test_only_affected_release_requires_overlay(self):
        for version, needed in [('0.25.22-1jammy', False), ('0.25.23-1jammy', True),
                                ('0.25.24-1jammy', False), ('0.25.25-1jammy', False)]:
            with self.subTest(version=version):
                result = self.run_shell(version, '/tmp', 'pg_tf2_fix_needed')
                self.assertEqual(result.returncode == 0, needed)

    def test_unverified_or_incomplete_build_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            prefix = Path(root) / '.ros_tf2/0.25.24'
            (prefix / 'install/lib').mkdir(parents=True)
            (prefix / 'install/local_setup.bash').write_text('echo LOADED\n')
            for state in ('missing_marker', 'missing_library'):
                with self.subTest(state=state):
                    result = self.run_shell('0.25.23', root, 'pg_source_tf2_fix "$3"')
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn('LOADED', result.stdout)
                    (prefix / 'verified').touch()

    def test_verified_overlay_is_loaded(self):
        with tempfile.TemporaryDirectory() as root:
            prefix = Path(root) / '.ros_tf2/0.25.24'
            (prefix / 'install/lib').mkdir(parents=True)
            (prefix / 'verified').touch()
            (prefix / 'install/lib/libtf2.so').touch()
            (prefix / 'install/local_setup.bash').write_text('export TF2_TEST_LOADED=yes\n')
            result = self.run_shell('0.25.23', root,
                                    'pg_source_tf2_fix "$3" && test "$TF2_TEST_LOADED" = yes')
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_newer_system_package_does_not_need_local_build(self):
        result = self.run_shell('0.25.24-1jammy', '/does-not-exist',
                                'pg_source_tf2_fix "$3"')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_system_upgrade_removes_inherited_overlay_paths(self):
        with tempfile.TemporaryDirectory() as root:
            result = self.run_shell('0.25.25-1jammy', root, '''
prefix="$3/.ros_tf2/0.25.24/install"
export AMENT_PREFIX_PATH="$prefix:/opt/ros/humble"
export CMAKE_PREFIX_PATH="/project:$prefix:/opt/ros/humble"
export COLCON_PREFIX_PATH="$prefix"
export LD_LIBRARY_PATH="$prefix/lib:/other/lib:/opt/ros/humble/lib"
pg_source_tf2_fix "$3" || exit 1
[[ "$AMENT_PREFIX_PATH" == /opt/ros/humble ]] || exit 2
[[ "$CMAKE_PREFIX_PATH" == /project:/opt/ros/humble ]] || exit 3
[[ -z "${COLCON_PREFIX_PATH:-}" ]] || exit 4
[[ "$LD_LIBRARY_PATH" == /other/lib:/opt/ros/humble/lib ]] || exit 5
''')
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_checksum_failure_stops_even_when_caller_handles_errors(self):
        for partial_install in (False, True):
            with self.subTest(partial_install=partial_install), tempfile.TemporaryDirectory() as root:
                prefix = Path(root) / '.ros_tf2/0.25.24'
                prefix.mkdir(parents=True)
                (prefix / 'geometry2-0.25.24.tar.gz').write_bytes(b'corrupt download')
                if partial_install:
                    (prefix / 'verified').touch()
                    (prefix / 'install/lib').mkdir(parents=True)
                    (prefix / 'install/lib/libtf2.so').touch()
                result = self.run_shell('0.25.23', root,
                                        'tar() { echo SHOULD_NOT_EXTRACT; return 1; }; '
                                        'pg_install_tf2_fix "$3" || exit 42')
                self.assertEqual(result.returncode, 42, result.stderr)
                self.assertNotIn('SHOULD_NOT_EXTRACT', result.stdout)
                self.assertFalse((prefix / 'verified').exists())

    def test_complete_verified_install_is_reused_without_download(self):
        with tempfile.TemporaryDirectory() as root:
            prefix = Path(root) / '.ros_tf2/0.25.24'
            (prefix / 'install/lib').mkdir(parents=True)
            for name in ('verified', 'install/lib/libtf2.so', 'install/local_setup.bash'):
                (prefix / name).touch()
            result = self.run_shell('0.25.23', root,
                                    'curl() { echo SHOULD_NOT_DOWNLOAD; return 1; }; '
                                    'pg_install_tf2_fix "$3" || exit 42')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('SHOULD_NOT_DOWNLOAD', result.stdout)

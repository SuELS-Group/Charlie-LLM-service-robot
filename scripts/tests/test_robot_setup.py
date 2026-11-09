"""Exercise calibration guards without sudo, network, or robot hardware.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "full_robot_setup.bash"
FLEET = "stretch-se3-3001"


class CalibrationPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="robot-setup-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "etc" / "hello-robot.conf"

    def backup(self, directory=None):
        directory = directory or self.root / FLEET
        (directory / "udev").mkdir(parents=True)
        (directory / "calibration_ros").mkdir()
        (directory / "stretch_configuration_params.yaml").write_text(
            f"robot:\n  model_name: SE3\n  serial_no: {FLEET}\n"
        )
        (directory / "stretch_user_params.yaml").write_text("{}\n")
        (directory / "udev" / "99-hello.rules").write_text("# test fixture\n")
        (directory / "calibration_ros" / "head_calibrated_20240101000000.urdf").write_text(
            '<robot name="stretch"><link name="base_link"/></robot>\n'
        )
        (directory / "calibration_ros" / "controller_calibration_head_20240101000000.yaml").write_text("{}\n")
        return directory

    def select(self, fleet="", parameters=False):
        command = '''
source "$1"
SYSTEM_CONFIG="$2"
FLEET_ID="$4"
select_calibration "$3" "$3/stretch_user"
if [[ "$5" == parameters ]]; then validate_parameters; fi
printf 'SELECTED=%s\n' "$CALIBRATION_DIR"
'''
        return subprocess.run(
            ["bash", "-c", command, "bash", str(SCRIPT), str(self.config),
             str(self.root), fleet, "parameters" if parameters else "files"],
            capture_output=True, text=True, timeout=10,
        )

    def test_missing_backup_fails_with_restore_instructions(self):
        result = self.select()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BEFORE the Hello Robot installation", result.stderr)
        self.assertIn("cp -a", result.stderr)
        self.assertFalse((self.root / "stretch_user").exists())

    def test_complete_backup_is_selected(self):
        directory = self.backup()
        result = self.select(parameters=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"SELECTED={directory}", result.stdout)

    def test_each_required_file_is_checked(self):
        directory = self.backup()
        for path in list(directory.rglob("*")):
            if path.is_file():
                with self.subTest(file=path.name):
                    contents = path.read_bytes()
                    path.unlink()
                    result = self.select()
                    self.assertNotEqual(result.returncode, 0)
                    path.write_bytes(contents)

    def test_ambiguous_backups_require_selection(self):
        self.backup()
        self.backup(self.root / "stretch-se3-3002")
        result = self.select()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Multiple calibration backups", result.stderr)
        self.assertEqual(self.select(FLEET).returncode, 0)

    def test_installed_user_calibration_takes_precedence(self):
        self.config.parent.mkdir()
        self.config.write_text(f"HELLO_FLEET_ID={FLEET}\n")
        self.backup(self.config.parent / FLEET)
        directory = self.backup(self.root / "stretch_user" / FLEET)
        result = self.select()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"SELECTED={directory}", result.stdout)

    def test_installed_system_backup_is_supported(self):
        self.config.parent.mkdir()
        self.config.write_text(f"HELLO_FLEET_ID={FLEET}\n")
        directory = self.backup(self.config.parent / FLEET)
        result = self.select()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"SELECTED={directory}", result.stdout)

    def test_installed_robot_cannot_be_silently_changed(self):
        self.config.parent.mkdir()
        self.config.write_text(f"HELLO_FLEET_ID={FLEET}\n")
        result = self.select("stretch-se3-3002")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs", result.stderr)

    def test_malformed_latest_urdf_fails(self):
        directory = self.backup()
        (directory / "calibration_ros" / "head_calibrated_20250101000000.urdf").write_text("<broken")
        self.assertNotEqual(self.select().returncode, 0)

    def test_invalid_upstream_timestamp_fails(self):
        directory = self.backup()
        (directory / "calibration_ros" / "head_calibrated_bad.urdf").write_text('<robot name="stretch"/>')
        self.assertNotEqual(self.select().returncode, 0)

    def test_wrong_robot_parameter_identity_fails(self):
        directory = self.backup()
        (directory / "stretch_configuration_params.yaml").write_text("robot:\n  serial_no: stretch-se3-9999\n")
        result = self.select(parameters=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("serial number", result.stderr)

    def test_active_venv_rejected_before_installation(self):
        env = dict(os.environ, VIRTUAL_ENV=str(self.root / "venv"))
        result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Deactivate", result.stderr)

    def test_help_and_invalid_arguments(self):
        result = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        result = subprocess.run(["bash", str(SCRIPT), "--fleet-id", "stretch-re1-1000"], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)


class DNSPreflightTests(unittest.TestCase):
    def check_dns(self, scenario):
        # Stub every network/privileged command: these tests must never alter the
        # machine's real NetworkManager profiles or depend on internet access.
        command = '''
source "$1"
scenario="$2"
timeout() {
    [[ "$scenario" == healthy || "$1" == 15s ]]
}
ip() { printf 'default via 192.168.50.1 dev wlan-test\n'; }
nmcli() {
    case "$2" in
        GENERAL.CONNECTION) printf 'test-wifi\n' ;;
        ipv4.method) printf 'manual\n' ;;
        ipv4.dns)
            if [[ "$scenario" == explicit_dns ]]; then printf '192.168.50.2\n'; fi
            ;;
        *) return 99 ;;
    esac
}
dig() {
    if [[ "$scenario" == broken_gateway ]]; then return 1; fi
    printf '151.101.0.223\n'
}
sudo() { printf 'CHANGED: %s\n' "$*"; }
ensure_download_dns
'''
        return subprocess.run(
            ["bash", "-c", command, "bash", str(SCRIPT), scenario],
            capture_output=True, text=True, timeout=10,
        )

    def test_working_dns_is_not_changed(self):
        result = self.check_dns("healthy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("CHANGED:", result.stdout)

    def test_explicit_dns_is_not_overwritten(self):
        result = self.check_dns("explicit_dns")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("CHANGED:", result.stdout)

    def test_unresponsive_gateway_is_not_used_as_dns(self):
        result = self.check_dns("broken_gateway")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("CHANGED:", result.stdout)

    def test_verified_gateway_repairs_missing_static_dns(self):
        result = self.check_dns("missing_dns")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("connection modify test-wifi ipv4.dns 192.168.50.1", result.stdout)
        self.assertIn("device reapply wlan-test", result.stdout)


if __name__ == "__main__":
    unittest.main()

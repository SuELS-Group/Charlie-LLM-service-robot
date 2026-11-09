"""Test operator gates and failure handling without opening robot hardware."""

import os
from pathlib import Path
import pty
import select
import subprocess
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "robot_post_install.bash"


class PostInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="post-install-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.marker = self.root / "command-ran"

    def run_shell(self, body, input_text=""):
        return subprocess.run(
            ["bash", "-c", 'source "$1"\nTEST_DIR="$2"\n' + body,
             "bash", str(SCRIPT), str(self.root)],
            input=input_text, capture_output=True, text=True, timeout=5,
        )

    def run_terminal(self, body, exchanges):
        master, slave = pty.openpty()
        process = subprocess.Popen(
            ["bash", "-c", 'source "$1"\nTEST_DIR="$2"\n' + body,
             "bash", str(SCRIPT), str(self.root)],
            stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        os.close(slave)
        output = b""
        try:
            for expected, response in exchanges:
                deadline = time.monotonic() + 5
                while expected.encode() not in output:
                    if time.monotonic() > deadline:
                        self.fail(f"Prompt not reached: {expected}; output={output!r}")
                    if select.select([process.stdout], [], [], 0.1)[0]:
                        chunk = os.read(process.stdout.fileno(), 4096)
                        if not chunk:
                            self.fail(f"Exited before prompt: {expected}; output={output!r}")
                        output += chunk
                self.assertFalse(self.marker.exists(), "Command ran before confirmation")
                os.write(master, response)
            tail, _ = process.communicate(timeout=5)
            return process.returncode, (output + tail).decode()
        finally:
            os.close(master)
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_piped_confirmation_cannot_start_command(self):
        result = self.run_shell('confirm_action "Prepare"; touch "$TEST_DIR/command-ran"', "READY\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("operator terminal", result.stderr)
        self.assertFalse(self.marker.exists())

    def test_blank_or_yes_is_not_readiness(self):
        code, output = self.run_terminal(
            'confirm_action "Prepare"; touch "$TEST_DIR/command-ran"',
            [("Type READY", b"\nyes\n"), ("No confirmation received", b"STOP\n")],
        )
        self.assertEqual(code, 2, output)
        self.assertFalse(self.marker.exists())

    def test_exact_confirmation_allows_next_command(self):
        code, output = self.run_terminal(
            'confirm_action "Remove clamp" CLAMP-REMOVED; touch "$TEST_DIR/command-ran"',
            [("Type CLAMP-REMOVED", b"CLAMP-REMOVED\n")],
        )
        self.assertEqual(code, 0, output)
        self.assertTrue(self.marker.exists())

    def test_terminal_eof_stops_without_command(self):
        code, output = self.run_terminal(
            'confirm_action "Prepare"; touch "$TEST_DIR/command-ran"',
            [("Type READY", b"\x04")],
        )
        self.assertEqual(code, 2, output)
        self.assertFalse(self.marker.exists())

    def test_same_boot_cannot_satisfy_requested_power_cycle(self):
        code, output = self.run_terminal('''
STATE_DIR="$TEST_DIR"
FLEET_ID=test
cat /proc/sys/kernel/random/boot_id > "$STATE_DIR/power-cycle-request-test"
power_cycle_guide
touch "$TEST_DIR/command-ran"
''', [("Choice (", b"POWER-CYCLED\n")])
        self.assertNotEqual(code, 0, output)
        self.assertIn("Still in the boot", output)
        self.assertFalse(self.marker.exists())

    def test_shutdown_requires_second_confirmation(self):
        # No real sudo or shutdown is ever invoked by this test.
        code, output = self.run_terminal('''
STATE_DIR="$TEST_DIR"
FLEET_ID=test
sudo() { printf '%s\n' "$*" > "$TEST_DIR/command-ran"; }
power_cycle_guide
''', [("Choice (", b"SHUTDOWN\n"), ("Type SHUTDOWN to confirm", b"STOP\n")])
        self.assertEqual(code, 2, output)
        self.assertFalse(self.marker.exists())

    def test_failed_command_cannot_advance(self):
        result = self.run_shell('''
RUN_DIR="$TEST_DIR"
fake_hardware() { echo "simulated failure"; return 23; }
run_post_command test fake_hardware
touch "$TEST_DIR/command-ran"
''')
        self.assertEqual(result.returncode, 23)
        self.assertFalse(self.marker.exists())
        self.assertIn("simulated failure", (self.root / "test.log").read_text())

    def test_unsaved_calibration_cannot_advance(self):
        (self.root / "stretch_configuration_params.yaml").write_text("{}\n")
        (self.root / "stretch_user_params.yaml").write_text("{}\n")
        result = self.run_shell('''
CALIBRATION_DIR="$TEST_DIR"
run_post_command() { COMMAND_LOG="$TEST_DIR/fake.log"; return 0; }
calibrate_contact arm
touch "$TEST_DIR/command-ran"
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No saved configuration", result.stderr)
        self.assertFalse(self.marker.exists())

    def test_user_override_blocks_ineffective_calibration(self):
        (self.root / "stretch_user_params.yaml").write_text(
            "arm:\n  contact_models:\n    effort_pct:\n      contact_thresh_default: [-20, 20]\n"
        )
        result = self.run_shell('CALIBRATION_DIR="$TEST_DIR"; check_contact_overrides arm')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("overrides arm", result.stderr)

    def encoder_fixture(self):
        directory = self.root / 'calibration_steppers'
        directory.mkdir()
        config = ''
        for joint in ('arm', 'lift', 'left-wheel', 'right-wheel'):
            config += f'hello-motor-{joint}:\n  serial_no: ABC123\n'
            (directory / f'hello-motor-{joint}_ABC123.yaml').write_text('- 1.0\n- 2.0\n')
        (self.root / 'stretch_configuration_params.yaml').write_text(config)
        (self.root / 'stretch_user_params.yaml').write_text('{}\n')

    def test_missing_motor_encoder_backup_stops_before_firmware(self):
        self.encoder_fixture()
        (self.root / 'calibration_steppers/hello-motor-lift_ABC123.yaml').unlink()
        result = self.run_shell('CALIBRATION_DIR="$TEST_DIR"; validate_encoder_calibration')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Restore calibration_steppers", result.stderr)
        self.assertIn("hello-motor-lift_ABC123.yaml", result.stderr)

    def test_encoder_files_must_match_configured_motor_serials(self):
        self.encoder_fixture()
        result = self.run_shell('CALIBRATION_DIR="$TEST_DIR"; validate_encoder_calibration')
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.root / 'stretch_user_params.yaml').write_text('hello-motor-arm:\n  serial_no: DEF456\n')
        result = self.run_shell('CALIBRATION_DIR="$TEST_DIR"; validate_encoder_calibration')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('hello-motor-arm_DEF456.yaml', result.stderr)

    def test_zero_exit_report_with_failure_is_rejected(self):
        (self.root / "report.log").write_text("\x1b[31m[Fail] Comms not ready\x1b[0m\n")
        result = self.run_shell('check_system_report "$TEST_DIR/report.log"')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reported a failure", result.stderr)

    def test_incomplete_report_is_rejected(self):
        (self.root / "report.log").write_text("[Pass] Battery voltage is 13.1 V\n")
        result = self.run_shell('check_system_report "$TEST_DIR/report.log"')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("software report is incomplete", result.stderr)

    def test_complete_report_reaches_operator_review(self):
        (self.root / "report.log").write_text(
            "[Pass] Battery voltage is 13.1 V\n[Pass] ROS2 Humble is ready\n"
        )
        result = self.run_shell('check_system_report "$TEST_DIR/report.log"')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_imu_az_warning_is_nonblocking_and_retained(self):
        (self.root / "report.log").write_text(
            "[Pass] Battery voltage is 13.5 V\n[Pass] ROS2 Humble is ready\n"
            "\x1b[33m[Warn] Sensors not ready (IMU AZ = -10.11 out of range -10.1 to -9.5)\n"
        )
        result = self.run_shell('check_system_report "$TEST_DIR/report.log"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[WARNING] Sensors not ready (IMU AZ = -10.11", result.stdout)
        self.assertIn("without blocking setup", result.stdout)

    def test_other_sensor_warnings_still_block(self):
        (self.root / "report.log").write_text(
            "[Pass] Battery voltage is 13.5 V\n[Pass] ROS2 Humble is ready\n"
            "[Warn] Sensors not ready (D435i is USB2)\n"
        )
        result = self.run_shell('check_system_report "$TEST_DIR/report.log"')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unresolved warnings", result.stderr)

    def test_imu_warning_does_not_mask_failures(self):
        (self.root / "report.log").write_text(
            "[Pass] Battery voltage is 13.5 V\n[Pass] ROS2 Humble is ready\n"
            "[Warn] Sensors not ready (IMU AZ = -10.11 out of range -10.1 to -9.5)\n"
            "[Fail] Actuators not ready\n"
        )
        result = self.run_shell('check_system_report "$TEST_DIR/report.log"')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reported a failure", result.stderr)

    def test_completion_records_imu_warning_without_hardware(self):
        (self.root / "report.log").write_text(
            "[Pass] Battery voltage is 13.5 V\n[Pass] ROS2 Humble is ready\n"
            "[Warn] Sensors not ready (IMU AZ = -10.11 out of range -10.1 to -9.5)\n"
        )
        result = self.run_shell('''
STATE_DIR="$TEST_DIR"
RUN_DIR="$TEST_DIR"
ROBOT_WS="$TEST_DIR"
FLEET_ID=test
LOG_FILE="$TEST_DIR/guide.log"
validate_calibration() { :; }
validate_parameters() { :; }
restore_project_calibration() { :; }
sync_project_meshes() { :; }
colcon() { :; }
confirm_action() { printf 'CONFIRM: %s\n' "${2:-READY}"; }
run_post_command() { COMMAND_LOG="$TEST_DIR/report.log"; }
finish_post_install
post_install_status
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CONFIRM: COMPLETE", result.stdout)
        self.assertIn("completed with warnings", result.stdout)
        receipt = (self.root / "post-install-test.complete").read_text()
        self.assertIn("Result: completed with warnings", receipt)
        self.assertIn("IMU AZ = -10.11", receipt)
        self.assertFalse((self.root / "post-install-test.needs-review").exists())

    def test_only_retired_diagnostics_warning_has_documented_exception(self):
        report = self.root / "report.log"
        report.write_text(
            "[Pass] Battery voltage is 13.5 V\n[Pass] ROS2 Humble is ready\n"
            "[Warn] Python pkgs not up-to-date (run pip3 install -U hello-robot-stretch-diagnostics)\n"
        )
        result = self.run_shell('check_system_report "$TEST_DIR/report.log"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("retired Stretch Diagnostics", result.stdout)
        report.write_text(report.read_text().replace("stretch-diagnostics", "stretch-body"))
        result = self.run_shell('check_system_report "$TEST_DIR/report.log"')
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()

# Installation and updates

The system uses a Stretch 3 robot and a workstation on the same private network. Run robot setup on the robot and workstation setup on the workstation.

## Requirements

- Ubuntu 22.04 LTS and Git on both machines.
- A workstation with an NVIDIA GPU suitable for the configured speech, vision, and language models.
- Network access for downloading system packages and models during installation.
- An SSH account on each managed machine.
- The robot's complete calibration backup and an operator with access to its runstop, power switch, keyboard, and monitor.

Use the robot's external USB Wi-Fi adapter for normal control. Ethernet is for installation. Configure machine addresses and matching ROS domain IDs as described in [Configuration](configuration.md#machines-and-network).

## Robot setup

Clone the repository into the robot user's home directory:

```bash
git clone https://gitlab.uni-oldenburg.de/suels-project/pg-llm-service-robot.git ~/pg-llm-service-robot
cd ~/pg-llm-service-robot
```

Restore this robot's complete `stretch-se3-XXXX` backup into the home directory, replacing `XXXX` with its serial number. Keep an original copy on another device. Do not use another robot's calibration.

The restored directory must include:

```text
~/stretch-se3-XXXX/
├── stretch_configuration_params.yaml
├── stretch_user_params.yaml
├── udev/*.rules
├── calibration_steppers/hello-motor-*.yaml
└── calibration_ros/
    ├── head_calibrated*.urdf
    └── controller_calibration_head_*.yaml
```

Run setup as the normal robot user:

```bash
./scripts/full_robot_setup.bash --check-only
./scripts/full_robot_setup.bash
./scripts/full_robot_setup.bash --post-install
```

Installation requires the charger in **SUPPLY** mode, the lift clamp in the instructed position, and robot applications stopped. Follow the script's physical preparation prompts. The post-install procedure covers a full power cycle, firmware, tool configuration, homing, contact calibration, and a system check. Rerun it after the requested power cycle to continue.

Stay beside the robot during hardware steps, keep its swept workspace clear, and follow the prompt that requires removing the lift clamp before lift calibration. Do not interrupt firmware flashing. Use Ctrl-C to stop a procedure when the preparation is incorrect.

View recorded completion status with:

```bash
./scripts/full_robot_setup.bash --post-install --status
```

Back up the updated calibration directory afterward. Before mobile operation, remove the clamp and disconnect the charger. Leave competing teleoperation autostart applications disabled when the controller owns the robot.

## Workstation setup

```bash
git clone https://gitlab.uni-oldenburg.de/suels-project/pg-llm-service-robot.git ~/pg-llm-service-robot
cd ~/pg-llm-service-robot
./scripts/full_workstations_setup.bash
```

Setup installs ROS 2 Humble, Python environments, workstation dependencies, Ollama, and configured models, then builds the ROS workspace. If it requests a reboot for GPU drivers, reboot and rerun the same command. Open a new terminal after setup to load the environment.

## Connect and deploy

Run on the machine hosting the controller:

```bash
cd ~/pg-llm-service-robot
./scripts/start.sh
```

The desktop launcher uses `gnome-terminal` and Firefox and opens the controller, RViz, and resource monitors. To run only the controller:

```bash
cd ~/pg-llm-service-robot/controller
uv run python server.py
```

1. Complete the interactive SSH key setup in the controller terminal.
2. Open `http://localhost:8080`.
3. Open **Deploy Services** and choose **Generate & Deploy All**.
4. Connect each machine and start its services.

Start the discovery server before opening RViz separately:

```bash
bash scripts/start_rviz.sh
```

See [Daily operation](usage.md) for commands and shutdown.

## Updates

After updating the checkout, rebuild on each relevant machine:

```bash
# On the workstation
source ~/pg-llm-service-robot/scripts/update_workstation_ws.sh
```

```bash
# On the robot
source ~/pg-llm-service-robot/scripts/update_robot_ws.sh
```

The update scripts sync Python dependencies and rebuild the ROS workspace. Regenerate and deploy service files after changing node or service configuration, then restart the affected services. Restart both sides and RViz after changing DDS profiles.

Setup logs and calibration recovery copies are stored under `~/.local/state/pg-llm-service-robot/`.

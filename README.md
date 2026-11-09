# PG-LLM-Service-Robot

Voice-controlled service robot stack built around ROS 2, a web controller, and LLM-based workstation services.

## Project Structure

| Path           | Purpose                                                                           |
|----------------|-----------------------------------------------------------------------------------|
| `controller/`  | FastAPI web controller, SSH/key management, service generation/deployment, Web UI |
| `robot/`       | Robot-side Python deps and ROS 2 workspace (`robot/ros_ws`)                       |
| `workstation/` | Workstation-side Python deps, ROS 2 workspace (`workstation/ament_ws`), LLM node  |
| `scripts/`     | Setup, update, and startup scripts                                                |
| `docs/`        | Extended documentation and setup notes                                            |

## One-Time Setup

Run these once on the respective machines after cloning:

On a freshly installed robot, first restore its complete calibration backup to
`~/stretch-se3-XXXX/`, replacing `XXXX` with that robot's serial number. The setup
stops before installation if the backup is missing or incomplete. See the
[robot setup instructions](docs/setup_project.md#robot-setup) for required files
and the post-install procedure.

```bash
cd ~/pg-llm-service-robot
./scripts/full_robot_setup.bash
# Guided power-cycle, firmware, homing, and calibration (operator required):
./scripts/full_robot_setup.bash --post-install
```

```bash
cd ~/pg-llm-service-robot
./scripts/full_workstations_setup.bash
```

If you only need rebuild/update later, use:

```bash
source ~/pg-llm-service-robot/scripts/update_robot_ws.sh
source ~/pg-llm-service-robot/scripts/update_workstation_ws.sh
```

Set the following variables in your bash.rc with the matching controller machine IP

```bash
export LD_LIBRARY_PATH="$HOME/pg-llm-service-robot/workstation/.venv/lib/python3.10/site-packages/onnxruntime/capi:$LD_LIBRARY_PATH"
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export FASTDDS_DEFAULT_PROFILES_FILE=$HOME/pg-llm-service-robot/controller/config/fastdds_discovery_profiles.xml
export ROS_DISCOVERY_SERVER=<workstation-ip>:30000
export ROS_SUPER_CLIENT=TRUE
```

## Daily Start (Controller + Web UI)

Use the single startup script:

```bash
cd ~/pg-llm-service-robot
./scripts/start.sh
```

What it starts:

- Controller server (`uv run python server.py`) in a new terminal
- Browser at `http://localhost:8080`
- RViz with `workstation/rviz_configs/remote_nav.rviz`

On first startup (no SSH keypair yet), the controller enters interactive SSH setup in the controller terminal before the
web service is available.

## Initial First-Run Setup (Command Line Then Web UI)

Do this on a fresh machine:

1. Run `./scripts/start.sh`.
2. In the **Controller Server** terminal, complete the interactive SSH key setup prompts and deploy keys.
3. Wait for server startup to finish.
4. Open/reload `http://localhost:8080` and continue in the Web UI.
5. Open the cloud-upload icon (Deploy services) and click **Generate & Deploy All**.
6. Back in the main view, connect each machine and start nodes.

## Machine Configuration

- Shared machine data is in `controller/config/machines.json` (host, type, ROS domain, project path).
- Private machine overrides are in `controller/config/machines.private.json` (gitignored).
- On first controller start, private entries are ensured for all machines.
- `user` and `ssh_key_path` are stored per machine in the private file.
- Default managed SSH key path stays `ssh_keys/controller_ed25519`.

## Controller Web UI Features

- Connect/disconnect machines
- Start/stop/restart individual nodes
- Start/stop all nodes per machine
- Live logs via WebSocket
- SSH key management
- Service generation and deployment
- Config reload

## Notes

- `scripts/start.sh` expects `gnome-terminal`, `firefox`, and `ros2` to be available.
- On first run, browser may open before the server is ready; finish SSH setup in the controller terminal, then reload
  the page.

## License

Licensed under **GNU AGPLv3**. See `LICENSE`.

# Developer onboarding

Start with the [architecture overview](high_level_documentation.md) and [installation guide](setup_project.md). The robot and workstation have separate ROS workspaces and Python environments.

## Source map

| Location | Responsibility |
| --- | --- |
| `controller/web/` | Controller routes and application lifecycle |
| `controller/static/` | Browser UI |
| `workstation/llm_node/` | Planner and conversation handling |
| `workstation/mcp_server/tools/` | Tools exposed to the planner |
| `workstation/workstation_common/` | Shared workstation configuration and data handling |
| `workstation/ament_ws/src/` | Workstation ROS packages |
| `robot/ros_ws/src/` | Robot ROS packages and interfaces |
| `robot/robot_common/` | Shared robot state and action support |
| `robot/assets/paper_tray/` | Printable paper-tray parts |

## Make a change

Use `scripts/create_ros2_node.sh` to scaffold a ROS node. Register services in `controller/config/nodes.json`; add shared ROS message, service, or action definitions under `robot/ros_ws/src/interfaces/`.

MCP tools live in category modules under `workstation/mcp_server/tools/`. Keep tool descriptions and argument metadata consistent with the ROS action they invoke. Hardware control remains in robot-side services.

Manage dependencies in the relevant `pyproject.toml` with `uv` and commit its updated lockfile. Rebuild with the appropriate workspace update script, then regenerate and deploy service files when service configuration changes.

## Verification

Behavior and safety tests live beside ROS packages, in `controller/test_*.py`, in `scripts/tests/`, and under `workstation/testing/`. Run the tests relevant to the change in the matching Python environment. Tests requiring ROS packages need their workspace sourced.

For example, the isolated knowledge-store suite needs no robot or model server:

```bash
workstation/.venv/bin/python -m pytest -q workstation/testing/rag
```

Keep regression tests for supported behavior and safety constraints. Put investigation results and change history in issues or commits. Documentation should describe how to use or understand the system.

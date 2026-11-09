# Configuration

Configuration paths below are relative to the repository root. Restart affected services after changing node settings.

## Machines and network

| File | Settings |
| --- | --- |
| `controller/config/machines.json` | Machine addresses, types, project paths, and ROS domain IDs |
| `controller/config/machines.private.json` | Local SSH usernames and key paths; excluded from Git |
| `controller/config/nodes.json` | Services, commands, dependencies, and workspace packages |
| `controller/config/dds_discovery_servers.json` | Discovery-server address, port, and profiles |

Configure the robot and workstation on the same private network with matching ROS domain IDs. Permit SSH and DDS traffic between them, including the configured discovery-server port. The supplied machine pairs use workstation/robot addresses `.50`/`.52` and `.49`/`.51` on `192.168.50.0/24`.

The robot's fixed control address belongs on its external USB Wi-Fi adapter, with autoconnect enabled and power saving disabled. Ethernet uses DHCP for installation. Keep the machine configuration and DDS profile interface allowlists consistent with the chosen addresses.

The setup scripts configure DDS host settings. To apply them to an existing installation:

```bash
./scripts/configure_dds_buffers.bash --install
./scripts/configure_dds_network.bash --install
```

Both scripts accept `--check` to inspect the settings. The robot's USB Wi-Fi configuration can be inspected with `python3 scripts/configure_robot_usb_wifi.py --help`.

Generated services select the production discovery profile. For a manually started ROS process, set the profile variables to the same file and use the workstation's discovery address:

```bash
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_DOMAIN_ID=42
export ROS_DISCOVERY_SERVER=192.168.50.50:30000
export ROS_SUPER_CLIENT=FALSE
export FASTRTPS_DEFAULT_PROFILES_FILE="$PWD/controller/config/fastdds_discovery_profiles.xml"
export FASTDDS_DEFAULT_PROFILES_FILE="$FASTRTPS_DEFAULT_PROFILES_FILE"
```

Run these commands from the repository root. Use `scripts/start_rviz.sh` for RViz; it selects the diagnostic profile. Redeploy services and restart ROS participants after changing discovery settings.

## Remote access

Connect using the workstation's configured address and your SSH account:

```bash
ssh <username>@<workstation-host>
```

If the workstation provides RDP, connect through the permitted local network or tunnel it over SSH:

```bash
ssh -L 13389:localhost:3389 -C -N -o ExitOnForwardFailure=yes <username>@<workstation-host>
```

Leave that terminal open and connect the RDP client to `localhost:13389`.

## Models and node settings

| Location | Purpose |
| --- | --- |
| `workstation/llm_node/config/llm.yaml` | Planner models, model profiles, budgets, and prompts |
| `workstation/mcp_server/config/mcp_server.yaml` | Tool settings, knowledge storage, and embedding model |
| `workstation/ament_ws/src/<package>/config/` | Speech, vision, and other workstation nodes |
| `robot/ros_ws/src/<package>/config/` | Robot actions, cameras, playback, and hardware services |

Model profiles select settings according to available GPU memory. Pull replacement models in Ollama before selecting them. The controller also exposes configuration dialogs for supported nodes.

External planner logging is disabled by default. To enable it, configure `planner_logging_endpoint`, set `planner_logging_enabled`, and supply `CHARLIE_PLANNER_LOGGING_API_KEY` outside the repository. Planner traces can contain user requests and tool results.

## Local knowledge

The searchable sources are UTF-8 `.txt` files in `controller/config/knowledge/`. Use the controller's **Knowledge Base** page to manage them and monitor indexing. Keep documents short, current, and limited to information the robot should answer from.

The administration CLI uses the workstation Python environment and writes JSON to stdout:

```bash
workstation/.venv/bin/python scripts/ingest_knowledge.py status
workstation/.venv/bin/python scripts/ingest_knowledge.py reindex
workstation/.venv/bin/python scripts/ingest_knowledge.py warmup
```

`reindex` requires the configured embedding model in Ollama. Remembered facts can be listed, created, updated, or deleted with the `memories`, `store-memory`, `update-memory`, and `delete-memory` commands, which read a JSON payload from stdin. For example:

```bash
echo '{"limit":20,"offset":0}' | workstation/.venv/bin/python scripts/ingest_knowledge.py memories
```

`ROBOT_KNOWLEDGE_MODE=local` restricts answers to approved local documents. `ROBOT_OPERATION_MODE=study` also enforces this restriction and applies the configured study action policy. After reviewing a current index, generate its approval record:

```bash
workstation/.venv/bin/python scripts/ingest_knowledge.py approval-candidate > /tmp/knowledge-approval.json
```

The reviewer fills in `owner`, `version`, and `approved_at`, then saves the record as `controller/config/knowledge_approval.json`. Approval applies to that index generation and source content; rebuilds require renewed review. The action policy is configured in `robot/ros_ws/src/action_service/config/study_policy.yaml`.

## Local data

| Path | Contents |
| --- | --- |
| `controller/static/face-images/` | Face enrollment source images |
| `controller/static/voice-samples/` | Speaker enrollment recordings |
| `controller/config/face_db.json` | Face embeddings |
| `controller/config/voice_db.json` | Speaker embeddings |
| `controller/config/users_db.json` | User profiles and notes |
| `workstation/mcp_server/config/knowledge_db/` | Document indexes and remembered facts |
| `controller/config/logs/` | Controller logs |

These files are excluded from Git and created locally as needed. Deleting source images or recordings does not by itself erase their embeddings or remembered facts. Remove the associated records and rebuild the relevant database. When retiring a deployment, stop its services before removing local stores and review any backups separately.

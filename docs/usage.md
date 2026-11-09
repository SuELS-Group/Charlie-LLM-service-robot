# Daily operation

Complete [installation](setup_project.md) and configure the intended robot and workstation pair before use.

## Start a session

1. Power on the robot and complete its required homing. Remove the lift clamp, disconnect the charger before mobile operation, and clear the workspace.
2. On the controller machine, run `./scripts/start.sh` from the repository root.
3. Open `http://localhost:8080`, connect each machine, and start its services. Start the robot driver before services that depend on it.
4. Confirm the intended robot appears in RViz and review its battery and service status in the controller.

Keep an operator beside the robot with access to the physical runstop during movement and manipulation.

## Give commands

Address the robot as Charlie and speak in English or German. You can also use **Send to LLM** in the controller.

Examples:

- “Charlie, what can you see?”
- “Charlie, save this location as reception.”
- “Charlie, go to reception.”
- “Charlie, take this from my hand.”
- “Charlie, where can I get a waiting ticket?”

Use the planner page to follow the current request, tool calls, and results. Answer clarification questions before starting a different task. Saved waypoints must exist before the robot can navigate to them.

## Transfer objects

For a handover, wait until the robot presents its gripper. Place or support the object as requested, then show a fresh thumbs-up to the head camera. Keep other body parts clear of the gripper and arm.

If a transfer fails or times out, inspect the robot and object before requesting another attempt. The stored holding state describes an assumption based on previous actions. Correct it explicitly when necessary, for example, “Charlie, you are not holding an item.”

## Stop and resume

Use the physical runstop for immediate intervention. Spoken stop requests also enter the software cancellation path. Check that movement has stopped before approaching the robot.

After a stop or incomplete action, inspect the workspace and follow the controller's recovery status. Release the runstop only when the robot is ready, then issue a new request. Do not assume that a timed-out action completed.

## Manage services

The controller provides per-node start, stop, restart, and logs, plus machine-wide controls. Deploy service files after changing node configuration. Restart affected services after changing their settings.

Restarting the discovery server interrupts ROS discovery. After an explicit discovery-server restart, restart robot and workstation ROS services and RViz.

## Manage knowledge and recognition

Use **Knowledge Base** to add or edit documents, rebuild the index, and review remembered facts. Document updates trigger indexing; wait for a successful build before relying on the new text.

Use **Face Registry** and **Voice Identity** to enroll users when recognition is needed. Use only samples you are authorized to store. Voice samples should contain one speaker and a few seconds of clear speech; mono, 16 kHz WAV is a suitable format. Use **Build DB** after changing voice samples.

Remove a user's source samples and rebuild the recognition database when removing their enrollment. Review remembered facts and local knowledge separately. These stores are local and excluded from Git; details are in [Configuration](configuration.md#local-data).

## End a session

1. Finish or cancel the active task and confirm that motion has stopped.
2. Stop robot services through the controller.
3. Shut down the robot's operating system.
4. Wait for shutdown to finish before switching off robot power.

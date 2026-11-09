function getRobotControlDirectionLabel(direction) {
    switch (direction) {
        case 'forward':
            return 'Forward';
        case 'backward':
            return 'Backward';
        case 'left':
            return 'Turning left';
        case 'right':
            return 'Turning right';
        default:
            return 'Idle';
    }
}

function setRobotControlUi(direction, statusText = 'Active', level = 'info') {
    const stateEl = document.getElementById('robot-control-state');
    const statusEl = document.getElementById('robot-control-status');
    const isError = level === 'error';
    const isWarning = level === 'warning';
    if (stateEl) {
        stateEl.textContent = getRobotControlDirectionLabel(direction);
        stateEl.classList.toggle('is-active', Boolean(direction) && !isError && !isWarning);
        stateEl.classList.toggle('is-warning', isWarning);
        stateEl.classList.toggle('is-error', isError);
    }
    if (statusEl) {
        statusEl.textContent = statusText;
        statusEl.classList.toggle('is-warning', isWarning);
        statusEl.classList.toggle('is-error', isError);
    }

    const activeMap = {
        forward: ['robot-key-up'],
        backward: ['robot-key-down'],
        left: ['robot-key-left'],
        right: ['robot-key-right'],
    };
    for (const id of ['robot-key-up', 'robot-key-down', 'robot-key-left', 'robot-key-right']) {
        document.getElementById(id)?.classList.remove('active');
    }
    for (const id of (activeMap[direction] || [])) {
        document.getElementById(id)?.classList.add('active');
    }
}

function setRobotControlConnectionBadge(label, tone = 'idle') {
    const badge = document.getElementById('robot-control-connection');
    if (!badge) return;
    badge.textContent = label;
    badge.classList.toggle('is-live', tone === 'live');
    badge.classList.toggle('is-connecting', tone === 'connecting');
    badge.classList.toggle('is-offline', tone === 'offline');
}

function setRobotControlModeBadge(mode) {
    const badge = document.getElementById('robot-control-mode');
    if (badge) {
        if (!mode) {
            badge.textContent = 'Unknown';
        } else if (mode === 'position') {
            badge.textContent = 'Position';
        } else {
            badge.textContent = mode.charAt(0).toUpperCase() + mode.slice(1);
        }
    }

    document.getElementById('robot-control-mode-position')?.classList.toggle('is-selected', mode === 'position');
    document.getElementById('robot-control-mode-navigation')?.classList.toggle('is-selected', mode === 'navigation');
}

function buildRobotControlSocketUrl(connection) {
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const params = new URLSearchParams({
        robot_id: connection.robot_id,
        ros_domain_id: String(connection.ros_domain_id),
    });
    return `${protocol}://${connection.host}:${connection.port}${connection.path}?${params.toString()}`;
}

function isRobotControlSocketOpen() {
    return Boolean(robotControlSocket && robotControlSocket.readyState === WebSocket.OPEN);
}

function clearRobotControlReconnect() {
    if (!robotControlReconnectTimer) return;
    window.clearTimeout(robotControlReconnectTimer);
    robotControlReconnectTimer = null;
}

function closeRobotControlSocket() {
    clearRobotControlReconnect();
    const socket = robotControlSocket;
    robotControlSocket = null;
    if (socket && socket.readyState < WebSocket.CLOSING) {
        socket.close(1000, 'Closing robot control');
    }
}

function scheduleRobotControlReconnect() {
    if (!robotControlShouldReconnect || robotControlReconnectTimer || !robotControlConnectionInfo) return;
    const delayMs = Math.min(4000, 800 * Math.max(1, robotControlReconnectAttempt));
    robotControlReconnectTimer = window.setTimeout(() => {
        robotControlReconnectTimer = null;
        connectRobotControlSocket();
    }, delayMs);
}

function sendRobotControlSocketMessage(payload) {
    if (!isRobotControlSocketOpen()) {
        throw new Error('Robot control is not connected yet');
    }
    robotControlSocket.send(JSON.stringify(payload));
}

function applyRobotControlState(state) {
    robotControlCurrentMode = state.currentMode || null;
    setRobotControlModeBadge(robotControlCurrentMode);

    const direction = robotControlCurrentDirection || state.activeDirection || null;
    const statusLevel = state.statusLevel || (state.controllerBusy ? 'warning' : 'info');
    const statusMessage = state.statusMessage || 'Ready to control';
    setRobotControlUi(direction, statusMessage, statusLevel);
}

async function connectRobotControlSocket() {
    if (!robotControlShouldReconnect || !robotControlConnectionInfo) return;
    if (robotControlSocket && (robotControlSocket.readyState === WebSocket.OPEN || robotControlSocket.readyState === WebSocket.CONNECTING)) {
        return;
    }

    setRobotControlConnectionBadge('Connecting', 'connecting');
    const socket = new WebSocket(buildRobotControlSocketUrl(robotControlConnectionInfo));
    robotControlSocket = socket;

    socket.addEventListener('open', async () => {
        if (robotControlSocket !== socket) return;
        robotControlReconnectAttempt = 0;
        robotControlLastSentDirection = null;
        setRobotControlConnectionBadge('Live', 'live');
        if (robotControlCurrentDirection) {
            try {
                await flushRobotControlDirection();
                startRobotControlHeartbeat();
            } catch (error) {
                setRobotControlUi(robotControlCurrentDirection, `Control failed: ${error.message}`, 'error');
            }
        }
    });

    socket.addEventListener('message', (event) => {
        if (robotControlSocket !== socket) return;
        try {
            const payload = JSON.parse(event.data);
            if (payload.type === 'state') {
                applyRobotControlState(payload);
                return;
            }
            if (payload.type === 'error') {
                setRobotControlUi(robotControlCurrentDirection, payload.message || 'Control error', 'error');
                showToast(payload.message || 'Robot control error', 'error');
            }
        } catch (error) {
            console.error('Failed to parse robot control message:', error);
        }
    });

    socket.addEventListener('close', () => {
        if (robotControlSocket === socket) {
            robotControlSocket = null;
        }
        robotControlLastSentDirection = null;
        stopRobotControlHeartbeat();
        if (!robotControlShouldReconnect) {
            setRobotControlConnectionBadge('Offline', 'offline');
            return;
        }
        robotControlReconnectAttempt += 1;
        setRobotControlConnectionBadge('Reconnecting', 'connecting');
        setRobotControlUi(robotControlCurrentDirection, 'Connection lost, reconnecting...', 'warning');
        scheduleRobotControlReconnect();
    });

    socket.addEventListener('error', () => {
        if (robotControlSocket !== socket) return;
        setRobotControlUi(robotControlCurrentDirection, 'WebSocket error', 'warning');
    });
}

function resolveRobotControlDirection() {
    if (!robotControlPressedOrder.length) return null;
    return robotControlPressedOrder[robotControlPressedOrder.length - 1] || null;
}

async function flushRobotControlDirection() {
    if (!robotControlMachineId) return;
    const direction = robotControlCurrentDirection;
    if (direction === robotControlLastSentDirection) return;
    if (!isRobotControlSocketOpen()) {
        robotControlLastSentDirection = null;
        setRobotControlUi(direction, 'Waiting for live connection...', 'warning');
        return;
    }

    try {
        if (direction) {
            sendRobotControlSocketMessage({type: 'motion', direction});
            robotControlLastSentDirection = direction;
            setRobotControlUi(direction, `Controlling ${robotControlMachineName || robotControlMachineId}`);
        } else {
            sendRobotControlSocketMessage({type: 'stop'});
            robotControlLastSentDirection = null;
            setRobotControlUi(null, 'Stopping robot...');
        }
    } catch (error) {
        setRobotControlUi(direction, `Control failed: ${error.message}`, 'error');
        throw error;
    }
}

function startRobotControlHeartbeat() {
    stopRobotControlHeartbeat();
    robotControlHeartbeat = window.setInterval(() => {
        if (!robotControlMachineId || !robotControlCurrentDirection) return;
        if (!isRobotControlSocketOpen()) return;
        try {
            sendRobotControlSocketMessage({type: 'heartbeat'});
        } catch (error) {
            console.error('Robot control heartbeat failed:', error);
            setRobotControlUi(robotControlCurrentDirection, `Control failed: ${error.message}`, 'error');
            stopRobotControlHeartbeat();
        }
    }, 150);
}

function stopRobotControlHeartbeat() {
    if (robotControlHeartbeat) {
        window.clearInterval(robotControlHeartbeat);
        robotControlHeartbeat = null;
    }
}

async function syncRobotControlDirection() {
    const nextDirection = resolveRobotControlDirection();
    if (nextDirection === robotControlCurrentDirection) return;
    robotControlCurrentDirection = nextDirection;
    if (robotControlCurrentDirection) {
        await flushRobotControlDirection();
        startRobotControlHeartbeat();
    } else {
        stopRobotControlHeartbeat();
        await flushRobotControlDirection();
    }
}

function robotControlDirectionForKey(event) {
    switch (event.key) {
        case 'w':
        case 'W':
        case 'ArrowUp':
            return 'forward';
        case 's':
        case 'S':
        case 'ArrowDown':
            return 'backward';
        case 'a':
        case 'A':
        case 'ArrowLeft':
            return 'left';
        case 'd':
        case 'D':
        case 'ArrowRight':
            return 'right';
        default:
            return null;
    }
}

async function handleRobotControlKeyDown(event) {
    if (!robotControlMachineId) return;
    const direction = robotControlDirectionForKey(event);
    if (!direction) return;
    event.preventDefault();
    try {
        await pressRobotControlDirection(direction);
    } catch (error) {
        showToast(`Robot control failed: ${error.message}`, 'error');
    }
}

async function handleRobotControlKeyUp(event) {
    if (!robotControlMachineId) return;
    const direction = robotControlDirectionForKey(event);
    if (!direction) return;
    event.preventDefault();
    try {
        await releaseRobotControlDirection(direction);
    } catch (error) {
        showToast(`Robot control failed: ${error.message}`, 'error');
    }
}

async function emergencyStopRobotControl() {
    stopRobotControlHeartbeat();
    robotControlPressedOrder = [];
    robotControlCurrentDirection = null;
    try {
        if (isRobotControlSocketOpen()) {
            sendRobotControlSocketMessage({type: 'stop'});
        }
    } catch (error) {
        console.error('Robot stop failed:', error);
    } finally {
        robotControlLastSentDirection = null;
        setRobotControlUi(null, 'Idle');
    }
}

async function pressRobotControlDirection(direction) {
    robotControlPressedOrder = robotControlPressedOrder.filter(value => value !== direction);
    robotControlPressedOrder.push(direction);
    await syncRobotControlDirection();
}

async function releaseRobotControlDirection(direction) {
    robotControlPressedOrder = robotControlPressedOrder.filter(value => value !== direction);
    await syncRobotControlDirection();
}

function bindRobotControlPadEvents() {
    for (const button of document.querySelectorAll('.robot-control-key[data-direction]')) {
        const direction = button.dataset.direction;
        if (!direction) continue;
        button.addEventListener('pointerdown', async (event) => {
            if (!robotControlMachineId) return;
            event.preventDefault();
            try {
                button.setPointerCapture?.(event.pointerId);
            } catch (_) {
            }
            try {
                await pressRobotControlDirection(direction);
            } catch (error) {
                showToast(`Robot control failed: ${error.message}`, 'error');
            }
        });
        const release = async (event) => {
            if (!robotControlMachineId) return;
            event.preventDefault();
            try {
                await releaseRobotControlDirection(direction);
            } catch (error) {
                showToast(`Robot control failed: ${error.message}`, 'error');
            }
        };
        button.addEventListener('pointerup', release);
        button.addEventListener('pointercancel', release);
        button.addEventListener('lostpointercapture', release);
    }
}

async function handleRobotControlMode(mode) {
    if (!robotControlMachineId) return;
    if (!isRobotControlSocketOpen()) {
        showToast('Robot control is still connecting.', 'warning');
        return;
    }

    stopRobotControlHeartbeat();
    robotControlPressedOrder = [];
    robotControlCurrentDirection = null;
    robotControlLastSentDirection = null;

    const label = mode === 'navigation' ? 'navigation' : 'position';
    setRobotControlUi(null, `Switching to ${label} mode...`);

    try {
        sendRobotControlSocketMessage({type: 'stop'});
        sendRobotControlSocketMessage({type: 'mode', mode});
    } catch (error) {
        setRobotControlUi(null, `Mode switch failed: ${error.message}`, 'error');
        showToast(`Mode switch failed: ${error.message}`, 'error');
    }
}

function bindRobotControlEvents() {
    document.addEventListener('keydown', handleRobotControlKeyDown);
    document.addEventListener('keyup', handleRobotControlKeyUp);
}

function unbindRobotControlEvents() {
    document.removeEventListener('keydown', handleRobotControlKeyDown);
    document.removeEventListener('keyup', handleRobotControlKeyUp);
}

async function openRobotControl(machineId, machineName) {
    const switchingRobots = robotControlMachineId && robotControlMachineId !== machineId;
    if (robotControlMachineId) {
        await emergencyStopRobotControl();
        unbindRobotControlEvents();
        closeRobotControlSocket();
    }

    robotControlMachineId = machineId;
    robotControlMachineName = machineName || machineId;
    robotControlPressedOrder = [];
    robotControlCurrentDirection = null;
    robotControlLastSentDirection = null;
    robotControlCurrentMode = null;
    robotControlConnectionInfo = null;
    robotControlShouldReconnect = true;
    robotControlReconnectAttempt = 0;
    document.getElementById('robot-control-title').textContent = `Robot Control — ${robotControlMachineName}`;
    document.getElementById('robot-control-modal').classList.remove('hidden');
    bindRobotControlEvents();
    setRobotControlConnectionBadge('Connecting', 'connecting');
    setRobotControlModeBadge(null);
    setRobotControlUi(null, switchingRobots ? `Switched control to ${robotControlMachineName}` : `Connecting to ${robotControlMachineName}...`);
    reinitIcons();

    try {
        robotControlConnectionInfo = await fetchRobotControlConnection(machineId);
        await connectRobotControlSocket();
    } catch (error) {
        robotControlShouldReconnect = false;
        setRobotControlConnectionBadge('Unavailable', 'offline');
        setRobotControlUi(null, `Connection failed: ${error.message}`, 'error');
        showToast(`Robot control unavailable: ${error.message}`, 'error');
    }
}

async function closeRobotControl() {
    robotControlShouldReconnect = false;
    await emergencyStopRobotControl();
    unbindRobotControlEvents();
    closeRobotControlSocket();
    document.getElementById('robot-control-modal').classList.add('hidden');
    robotControlMachineId = null;
    robotControlMachineName = '';
    robotControlConnectionInfo = null;
    robotControlCurrentMode = null;
    setRobotControlConnectionBadge('Offline', 'offline');
    setRobotControlModeBadge(null);
}

function renderMachines(list) {
    const container = document.getElementById('machines-container');

    if (!list.length) {
        container.innerHTML = '<div class="empty-state">No machines configured</div>';
        reinitIcons();
        return;
    }

    container.innerHTML = list.map(m => {
        const collapsed = collapsedMachines[m.id] ? 'collapsed' : '';
        const isRobot = String(m.type || '').toLowerCase() === 'robot';
        const gitText = m.connected
            ? (m.git_branch ? `Branch ${m.git_branch}` : (m.git_error ? `Git: ${m.git_error}` : 'Git unavailable'))
            : 'Git: disconnected';

        return `
        <div class="machine-card ${collapsed}" id="machine-${m.id}">
            <div class="machine-header" onclick="toggleMachine('${m.id}', event)">
                <div class="machine-header-left">
                    ${icon('chevron-down', 'machine-chevron')}
                    <div>
                        <div class="machine-title">${m.name}</div>
                        <div class="machine-meta">
                            <span class="machine-meta-item">${icon('monitor')} ${m.host}</span>
                            <span class="machine-meta-item">${icon('radio')} Domain ${m.ros_domain_id}</span>
                            <span class="machine-meta-item">${icon('tag')} ${m.type}</span>
                            <span class="machine-meta-item">${icon('git-branch')} ${gitText}</span>
                        </div>
                    </div>
                </div>
                <div class="machine-header-right">
                    <span class="conn-badge ${m.connected ? 'on' : 'off'}">
                        <span class="dot"></span>
                        ${m.connected ? 'Connected' : 'Disconnected'}
                    </span>
                    <div class="machine-actions" onclick="event.stopPropagation()">
                        ${!m.connected ? `
                            <button onclick="event.stopPropagation(); handleConnect('${m.id}')" class="btn btn-primary btn-sm">
                                ${icon('plug')} Connect
                            </button>
                        ` : `
                            <button onclick="event.stopPropagation(); handleGitPull('${m.id}')" class="btn btn-ghost btn-sm" title="Git pull">
                                ${icon('git-pull-request-arrow')} Pull
                            </button>
                            <button onclick="event.stopPropagation(); handleStartAll('${m.id}')" class="btn btn-success btn-sm" title="Start all">
                                ${icon('play')} All
                            </button>
                            <button onclick="event.stopPropagation(); handleStopAll('${m.id}')" class="btn btn-danger btn-sm" title="Stop all">
                                ${icon('square')} All
                            </button>
                            ${isRobot ? `
                                <button onclick="event.stopPropagation(); handleRestartMachine('${m.id}')" class="btn btn-ghost btn-sm" title="Restart machine">
                                    ${icon('rotate-cw')} Restart
                                </button>
                                <button onclick="event.stopPropagation(); handleShutdownMachine('${m.id}')" class="btn btn-danger btn-sm" title="Shutdown machine">
                                    ${icon('power')} Shutdown
                                </button>
                                <div class="volume-inline" title="Set speaker volume" onclick="event.stopPropagation()">
                                    <input id="volume-slider-${m.id}" class="volume-inline-slider" type="range"
                                           min="0" max="100" step="1" value="${getSavedVolume(m.id)}"
                                           oninput="handleVolumeInput('${m.id}', this.value)"
                                           onchange="handleSetVolume('${m.id}', this.value)">
                                    <span id="volume-value-${m.id}" class="volume-inline-value">${getSavedVolume(m.id)}%</span>
                                </div>
                                <button
                                    onclick='event.stopPropagation(); openRobotControl(${JSON.stringify(m.id)}, ${JSON.stringify(m.name)})'
                                    class="btn btn-primary btn-sm"
                                    title="Remote control for ${escapeHtml(m.name)}">
                                    Remote Control
                                </button>
                            ` : ''}
                            <button onclick="event.stopPropagation(); refreshMachineStatus('${m.id}')" class="btn btn-ghost btn-sm" title="Refresh">
                                ${icon('refresh-cw')}
                            </button>
                        `}
                    </div>
                </div>
            </div>
            <div class="machine-body" id="body-${m.id}">
                <div class="sysinfo-bar" id="sysinfo-${m.id}" style="display:${isRobot ? 'flex' : 'none'}">
                    <span class="sysinfo-system" id="sysinfo-values-${m.id}"></span>
                    ${isRobot ? renderBatteryInline(m) : ''}
                </div>
                ${isRobot ? renderBatteryDetails(m) : ''}
                <div class="nodes-grid" id="nodes-${m.id}">
                    ${m.connected
            ? `<div class="loading-state">${icon('loader', 'spin')} <span>Loading nodes...</span></div>`
            : '<div class="empty-state">Connect to view nodes</div>'}
                </div>
            </div>
        </div>`;
    }).join('');

    reinitIcons();
    renderBatteryIndicators();

    list.filter(m => m.connected).forEach(m => {
        loadMachineNodes(m.id);
        loadSystemInfo(m.id);
    });
}

function renderNodes(machineId, nodes) {
    const container = document.getElementById(`nodes-${machineId}`);
    if (!container) return;

    if (!nodes.length) {
        container.innerHTML = '<div class="empty-state">No nodes configured</div>';
        reinitIcons();
        return;
    }

    container.innerHTML = nodes.map(n => {
        const nodeType = n.type || 'node';
        const typeBadge = nodeType === 'launch'
            ? '<span class="node-type-badge">launch</span>'
            : '';
        return `
        <div class="node-card" id="node-${machineId}-${n.id}" data-status="${n.status}"
             data-name="${(n.name || '').toLowerCase()}"
             data-desc="${(n.description || '').toLowerCase()}"
             data-service="${(n.service_name || '').toLowerCase()}">
            <div class="node-top">
                <div>
                    <div class="node-name">${n.name}</div>
                    <div class="node-desc">${n.description}</div>
                    ${typeBadge}
                </div>
                <div class="node-status-area">
                    <span class="status-pill ${n.status}">${n.status}</span>
                    ${n.pid ? `<span class="node-uptime">PID ${n.pid}</span>` : ''}
                    ${n.started_at && n.status === 'running' ? `<span class="node-uptime">${relativeUptime(n.started_at)}</span>` : ''}
                </div>
            </div>
            <div class="node-details">
                <div>Service: <span>${n.service_name}</span></div>
                ${n.exit_code != null && n.exit_code !== 0 ? `<div>Exit: <span style="color:var(--red)">${n.exit_code}</span></div>` : ''}
                ${n.memory_current ? `<div>Mem: <span>${formatMemory(n.memory_current)}</span></div>` : ''}
            </div>
            <div class="node-actions">
                <button onclick="handleStart('${machineId}','${n.id}')"
                        class="btn btn-success btn-sm" ${n.status === 'running' ? 'disabled' : ''}>
                    ${icon('play')} Start
                </button>
                <button onclick="handleStop('${machineId}','${n.id}')"
                        class="btn btn-danger btn-sm" ${n.status === 'stopped' || n.status === 'unknown' ? 'disabled' : ''}>
                    ${icon('square')} Stop
                </button>
                <button onclick="handleRestart('${machineId}','${n.id}')"
                        class="btn btn-ghost btn-sm" ${n.status !== 'running' ? 'disabled' : ''}>
                    ${icon('rotate-cw')} Restart
                </button>
                <button onclick="openLogModal('${machineId}','${n.id}','${n.name}')"
                        class="btn btn-ghost btn-sm">
                    ${icon('file-text')} Logs
                </button>
                ${n.id === 'llm_agent' ? `
                <button onclick="openPlannerPage('${machineId}')"
                        class="btn btn-primary btn-sm">
                    ${icon('list')} Planner
                </button>
                <button onclick="openModelSelector('${machineId}')" class="btn btn-ghost btn-sm">
                    ${icon('layers')} Select Model
                </button>
                ` : ''}
                ${n.id === 'tts_playback' ? `
                <button onclick="openTtsConfigModal('${machineId}')" class="btn btn-ghost btn-sm">
                    ${icon('settings-2')} Config
                </button>
                ` : ''}
                ${n.id === 'led_control' ? `
                <button onclick="openLedConfigModal('${machineId}')" class="btn btn-ghost btn-sm">
                    ${icon('settings-2')} Config
                </button>
                ` : ''}
                ${n.id === 'voice_detection' ? `
                <button onclick="openVadConfigModal('${machineId}')" class="btn btn-ghost btn-sm">
                    ${icon('settings-2')} Config
                </button>
                ` : ''}
                ${n.id === 'transcriber' ? `
                <button onclick="openTranscriberConfigModal('${machineId}')" class="btn btn-ghost btn-sm">
                    ${icon('settings-2')} Config
                </button>
                ` : ''}
                ${n.id === 'voice_identity' ? `
                <button onclick="openVoiceIdentityConfigModal('${machineId}')" class="btn btn-ghost btn-sm">
                    ${icon('settings-2')} Config
                </button>
                ` : ''}
            </div>
        </div>`;
    }).join('');

    reinitIcons();
    applyFilters();
}

function updateNodeCard(machineId, ns) {
    const card = document.getElementById(`node-${machineId}-${ns.id}`);
    if (!card) return;

    card.setAttribute('data-status', ns.status);

    const pill = card.querySelector('.status-pill');
    if (pill) {
        pill.className = `status-pill ${ns.status}`;
        pill.textContent = ns.status;
    }

    const statusArea = card.querySelector('.node-status-area');
    if (statusArea) {
        statusArea.querySelectorAll('.node-uptime').forEach(el => el.remove());
        if (ns.pid) {
            const pidSpan = document.createElement('span');
            pidSpan.className = 'node-uptime';
            pidSpan.textContent = `PID ${ns.pid}`;
            statusArea.appendChild(pidSpan);
        }
        if (ns.started_at && ns.status === 'running') {
            const upSpan = document.createElement('span');
            upSpan.className = 'node-uptime';
            upSpan.textContent = relativeUptime(ns.started_at);
            statusArea.appendChild(upSpan);
        }
    }

    const buttons = card.querySelectorAll('.node-actions button');
    if (buttons.length >= 3) {
        const inTransition = ns.status === 'activating' || ns.status === 'deactivating';
        buttons[0].disabled = inTransition || ns.status === 'running';
        buttons[1].disabled = inTransition || ns.status === 'stopped' || ns.status === 'unknown';
        buttons[2].disabled = inTransition || ns.status !== 'running';
    }

    applyFilters();
}

async function loadSystemInfo(machineId, options = {}) {
    try {
        const info = await fetchSystemInfo(machineId);
        const bar = document.getElementById(`sysinfo-${machineId}`);
        if (!bar) return;

        const items = [];
        if (info.hostname) items.push(systemInfoItem('server', 'Host', info.hostname));
        if (info.uptime) items.push(systemInfoItem('clock', 'Up', info.uptime));
        if (info.cpu_usage) items.push(systemInfoItem('activity', 'CPU', info.cpu_usage));
        if (info.memory_used && info.memory_total) {
            items.push(systemInfoItem('memory-stick', 'RAM', `${info.memory_used} / ${info.memory_total}`));
        }
        if (info.disk_used && info.disk_total) {
            items.push(
                systemInfoItem(
                    'hard-drive',
                    'Disk',
                    `${info.disk_used} / ${info.disk_total} (${info.disk_use_percent})`,
                ),
            );
        }
        if (info.gpu_usage) items.push(systemInfoItem('gauge', 'GPU', info.gpu_usage));
        if (info.gpu_memory_used && info.gpu_memory_total) {
            items.push(
                systemInfoItem(
                    'microchip',
                    'VRAM',
                    `${info.gpu_memory_used} / ${info.gpu_memory_total}`,
                ),
            );
        }

        const values = document.getElementById(`sysinfo-values-${machineId}`);
        if (values) values.innerHTML = items.map(i => `<span class="sysinfo-item">${i}</span>`).join('');
        bar.style.display = items.length || bar.querySelector('.battery-inline') ? 'flex' : 'none';
        reinitIcons();
    } catch (e) {
        if (!options.silent) {
            console.warn(`System info unavailable for ${machineId}:`, e.message);
        }
    }
}

function startSystemInfoRefresh() {
    if (systemInfoRefreshTimer) return;
    systemInfoRefreshTimer = window.setInterval(refreshConnectedSystemInfo, 3000);
}

async function refreshConnectedSystemInfo() {
    if (systemInfoRefreshInFlight || document.hidden) return;
    const connectedMachineIds = machines
        .filter(machine => machine.connected)
        .map(machine => machine.id)
        .filter(machineId => document.getElementById(`sysinfo-${machineId}`));

    if (!connectedMachineIds.length) return;

    systemInfoRefreshInFlight = true;
    try {
        await Promise.allSettled(
            connectedMachineIds.map(machineId => loadSystemInfo(machineId, {silent: true})),
        );
    } finally {
        systemInfoRefreshInFlight = false;
    }
}

function systemInfoItem(iconName, label, value) {
    return `${icon(iconName)} <span class="sysinfo-label">${label}</span> `
        + `<span class="sysinfo-value">${value}</span>`;
}

function toggleMachine(machineId, event) {
    if (event.target.closest('.machine-actions')) return;

    const card = document.getElementById(`machine-${machineId}`);
    if (!card) return;
    card.classList.toggle('collapsed');
    collapsedMachines[machineId] = card.classList.contains('collapsed');
    localStorage.setItem('collapsedMachines', JSON.stringify(collapsedMachines));
}

function applyFilters() {
    document.querySelectorAll('.node-card').forEach(card => {
        card.classList.remove('filter-hidden');
    });
}

document.addEventListener('DOMContentLoaded', () => {
});

async function handleConnect(machineId) {
    showToast(`Connecting to ${machineId}...`, 'info');
    try {
        await connectMachine(machineId);
        showToast(`Connected to ${machineId}`, 'success');
        await refreshAllStatus();
    } catch (e) {
        showToast(`Failed to connect: ${e.message}`, 'error');
    }
}

async function handleStart(machineId, nodeId) {
    updateNodeCard(machineId, {
        id: nodeId,
        status: 'activating',
        active_state: 'activating',
        sub_state: '',
        pid: null,
        started_at: null
    });
    showToast(`Starting ${nodeId}...`, 'info');
    try {
        const r = await startNode(machineId, nodeId);
        showToast(r.message, r.success ? 'success' : 'error');
        await pollUntilStable(machineId, nodeId);
    } catch (e) {
        showToast(`Failed: ${e.message}`, 'error');
        await refreshNodeStatus(machineId, nodeId);
    }
}

async function handleStop(machineId, nodeId) {
    updateNodeCard(machineId, {
        id: nodeId,
        status: 'deactivating',
        active_state: 'deactivating',
        sub_state: '',
        pid: null,
        started_at: null
    });
    showToast(`Stopping ${nodeId}...`, 'info');
    try {
        const r = await stopNode(machineId, nodeId);
        showToast(r.message, r.success ? 'success' : 'error');
        await pollUntilStable(machineId, nodeId);
    } catch (e) {
        showToast(`Failed: ${e.message}`, 'error');
        await refreshNodeStatus(machineId, nodeId);
    }
}

async function handleRestart(machineId, nodeId) {
    updateNodeCard(machineId, {
        id: nodeId,
        status: 'deactivating',
        active_state: 'deactivating',
        sub_state: '',
        pid: null,
        started_at: null
    });
    showToast(`Restarting ${nodeId}...`, 'info');
    try {
        const r = await restartNode(machineId, nodeId);
        showToast(r.message, r.success ? 'success' : 'error');
        await pollUntilStable(machineId, nodeId);
    } catch (e) {
        showToast(`Failed: ${e.message}`, 'error');
        await refreshNodeStatus(machineId, nodeId);
    }
}

async function handleGitPull(machineId) {
    showToast(`Running git pull on ${machineId}...`, 'info');
    try {
        const result = await pullMachineGit(machineId);
        showToast(result.message || `Git pull completed on ${machineId}`, 'success');
        await refreshAllStatus();
    } catch (e) {
        showToast(`Git pull failed: ${e.message}`, 'error');
    }
}

async function handleStartAll(machineId) {
    showToast(`Starting all nodes on ${machineId}...`, 'info');
    try {
        const results = await startAllNodes(machineId);
        const failed = results.filter(r => !r.success);
        if (failed.length) showToast(`${results.length - failed.length} started, ${failed.length} failed`, 'warning');
        else showToast('All nodes started', 'success');
        await refreshMachineStatus(machineId);
    } catch (e) {
        showToast(`Failed: ${e.message}`, 'error');
    }
}

async function handleStopAll(machineId) {
    showToast(`Stopping all nodes on ${machineId}...`, 'info');
    try {
        const results = await stopAllNodes(machineId);
        const failed = results.filter(r => !r.success);
        if (failed.length) showToast(`${results.length - failed.length} stopped, ${failed.length} failed`, 'warning');
        else showToast('All nodes stopped', 'success');
        await refreshMachineStatus(machineId);
    } catch (e) {
        showToast(`Failed: ${e.message}`, 'error');
    }
}

async function loadMachineNodes(machineId) {
    try {
        const statuses = await fetchNodesStatus(machineId);
        nodesCache[machineId] = statuses;
        renderNodes(machineId, statuses);
        startStatusPolling(machineId);
    } catch (e) {
        console.error(`Error loading nodes for ${machineId}:`, e);
        const c = document.getElementById(`nodes-${machineId}`);
        if (c) c.innerHTML = `<div class="empty-state" style="color:var(--red)">Error: ${e.message}</div>`;
    }
}

async function refreshMachineStatus(machineId) {
    try {
        const statuses = await fetchNodesStatus(machineId);
        nodesCache[machineId] = statuses;
        renderNodes(machineId, statuses);
    } catch (e) {
        console.error(`Error refreshing ${machineId}:`, e);
        showToast(`Error refreshing ${machineId}`, 'error');
    }
}

async function refreshNodeStatus(machineId, nodeId) {
    try {
        const st = await fetchNodeStatus(machineId, nodeId);
        updateNodeCard(machineId, st);
    } catch (e) {
        console.error(`Error refreshing node ${nodeId}:`, e);
    }
}

async function refreshAllStatus() {
    try {
        machines = await fetchMachines();
        renderMachines(machines);
        await refreshDiscoveryServer();
    } catch (e) {
        console.error('Error refreshing:', e);
        showToast('Error refreshing status', 'error');
    }
}

async function reloadConfig() {
    try {
        await apiCall('/api/config/reload');
        showToast('Configuration reloaded', 'success');
        await refreshAllStatus();
    } catch (e) {
        showToast(`Error: ${e.message}`, 'error');
    }
}

function startStatusPolling(machineId) {
    if (statusPollingIntervals[machineId]) clearInterval(statusPollingIntervals[machineId]);
    statusPollingIntervals[machineId] = setInterval(async () => {
        try {
            const statuses = await fetchNodesStatus(machineId);
            statuses.forEach(s => {
                updateNodeCard(machineId, s);
                if (logModalMachineId === machineId && logModalNodeId === s.id) {
                    updateLogModalControls(s);
                }
            });
        } catch (e) {
        }
    }, 1500);
}

async function pollUntilStable(machineId, nodeId, maxMs = 15000) {
    const start = Date.now();
    const transient = new Set(['activating', 'deactivating', 'reloading']);
    while (Date.now() - start < maxMs) {
        await new Promise(r => setTimeout(r, 600));
        try {
            const st = await fetchNodeStatus(machineId, nodeId);
            updateNodeCard(machineId, st);
            if (logModalMachineId === machineId && logModalNodeId === nodeId) updateLogModalControls(st);
            if (!transient.has(st.active_state) && st.status !== 'activating' && st.status !== 'deactivating') return st;
        } catch (e) {
            break;
        }
    }
}

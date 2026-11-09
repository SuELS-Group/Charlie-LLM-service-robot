function openLogModal(machineId, nodeId, nodeName) {
    logModalMachineId = machineId;
    logModalNodeId = nodeId;
    document.getElementById('log-modal-title').textContent = `Logs — ${nodeName}`;
    document.getElementById('log-content').textContent = '';
    logLineCount = 0;
    updateLogLineCount();
    document.getElementById('log-modal').classList.remove('hidden');
    reinitIcons();
    updateLogModalControls();
    connectLogWebSocket(machineId, nodeId);
}

function closeLogModal() {
    document.getElementById('log-modal').classList.add('hidden');
    if (currentLogWebSocket) {
        currentLogWebSocket.close();
        currentLogWebSocket = null;
    }
    updateLogStatus(false);
    logModalMachineId = null;
    logModalNodeId = null;
}

function connectLogWebSocket(machineId, nodeId) {
    if (currentLogWebSocket) currentLogWebSocket.close();

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${location.host}/ws/logs/${machineId}/${nodeId}`;

    try {
        currentLogWebSocket = new WebSocket(url);
        currentLogWebSocket.onopen = () => updateLogStatus(true);
        currentLogWebSocket.onmessage = (e) => {
            const data = JSON.parse(e.data);
            if (data.error) {
                appendLog(`Error: ${data.error}`);
                return;
            }
            if (data.type === 'history') appendLog(data.logs);
            else if (data.type === 'log') appendLog(data.message + '\n');
        };
        currentLogWebSocket.onerror = () => updateLogStatus(false);
        currentLogWebSocket.onclose = () => {
            updateLogStatus(false);
            // Reconnect automatically if the modal is still open
            if (logModalMachineId === machineId && logModalNodeId === nodeId) {
                setTimeout(() => {
                    if (logModalMachineId === machineId && logModalNodeId === nodeId) {
                        connectLogWebSocket(machineId, nodeId);
                    }
                }, 2000);
            }
        };
    } catch (e) {
        console.error('WebSocket error:', e);
        updateLogStatus(false);
    }
}

function appendLog(text) {
    const content = document.getElementById('log-content');
    const container = document.getElementById('log-container');
    const autoscroll = document.getElementById('log-autoscroll');

    const lines = text.split('\n');
    lines.forEach(line => {
        if (!line && lines.indexOf(line) === lines.length - 1) return;
        const span = document.createElement('span');
        if (/\b(ERROR|FATAL|CRITICAL)\b/i.test(line)) span.className = 'log-error';
        else if (/\b(WARN|WARNING)\b/i.test(line)) span.className = 'log-warn';
        else if (/\bDEBUG\b/i.test(line)) span.className = 'log-debug';
        span.textContent = line + '\n';
        content.appendChild(span);
    });

    logLineCount += (text.match(/\n/g) || []).length;
    updateLogLineCount();
    if (autoscroll.checked) container.scrollTop = container.scrollHeight;
}

function clearLogs() {
    document.getElementById('log-content').textContent = '';
    logLineCount = 0;
    updateLogLineCount();
}

function downloadLogs() {
    const text = document.getElementById('log-content').textContent;
    const blob = new Blob([text], {type: 'text/plain'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${document.getElementById('log-modal-title').textContent.replace(/[^a-zA-Z0-9_-]/g, '_')}.log`;
    a.click();
    URL.revokeObjectURL(a.href);
}

function updateLogStatus(connected) {
    const el = document.getElementById('log-status');
    el.innerHTML = `<span class="log-dot"></span> ${connected ? 'Streaming' : 'Disconnected'}`;
    el.className = `log-status ${connected ? 'connected' : ''}`;
}

function updateLogLineCount() {
    document.getElementById('log-line-count').textContent = `${logLineCount} lines`;
}

function updateLogModalControls(status) {
    if (!logModalMachineId || !logModalNodeId) return;
    const s = status || (nodesCache[logModalMachineId] || []).find(n => n.id === logModalNodeId);
    if (!s) return;

    const btnStart = document.getElementById('log-btn-start');
    const btnStop = document.getElementById('log-btn-stop');
    const btnRestart = document.getElementById('log-btn-restart');
    if (!btnStart) return;

    btnStart.disabled = s.status === 'running';
    btnStop.disabled = s.status === 'stopped' || s.status === 'unknown';
    btnRestart.disabled = s.status !== 'running';

    const badge = document.getElementById('log-service-status-badge');
    if (badge) {
        badge.className = `status-pill ${s.status}`;
        badge.textContent = s.status;
    }
}

async function logModalStart() {
    if (!logModalMachineId || !logModalNodeId) return;
    updateNodeCard(logModalMachineId, {
        id: logModalNodeId,
        status: 'activating',
        active_state: 'activating',
        sub_state: '',
        pid: null,
        started_at: null
    });
    updateLogModalControls({id: logModalNodeId, status: 'activating', active_state: 'activating'});
    showToast(`Starting ${logModalNodeId}...`, 'info');
    try {
        const r = await startNode(logModalMachineId, logModalNodeId);
        showToast(r.message, r.success ? 'success' : 'error');
        await pollUntilStable(logModalMachineId, logModalNodeId);
    } catch (e) {
        showToast(`Failed: ${e.message}`, 'error');
    }
}

async function logModalStop() {
    if (!logModalMachineId || !logModalNodeId) return;
    updateNodeCard(logModalMachineId, {
        id: logModalNodeId,
        status: 'deactivating',
        active_state: 'deactivating',
        sub_state: '',
        pid: null,
        started_at: null
    });
    updateLogModalControls({id: logModalNodeId, status: 'deactivating', active_state: 'deactivating'});
    showToast(`Stopping ${logModalNodeId}...`, 'info');
    try {
        const r = await stopNode(logModalMachineId, logModalNodeId);
        showToast(r.message, r.success ? 'success' : 'error');
        await pollUntilStable(logModalMachineId, logModalNodeId);
    } catch (e) {
        showToast(`Failed: ${e.message}`, 'error');
    }
}

async function logModalRestart() {
    if (!logModalMachineId || !logModalNodeId) return;
    updateNodeCard(logModalMachineId, {
        id: logModalNodeId,
        status: 'deactivating',
        active_state: 'deactivating',
        sub_state: '',
        pid: null,
        started_at: null
    });
    updateLogModalControls({id: logModalNodeId, status: 'deactivating', active_state: 'deactivating'});
    showToast(`Restarting ${logModalNodeId}...`, 'info');
    try {
        const r = await restartNode(logModalMachineId, logModalNodeId);
        showToast(r.message, r.success ? 'success' : 'error');
        await pollUntilStable(logModalMachineId, logModalNodeId);
    } catch (e) {
        showToast(`Failed: ${e.message}`, 'error');
    }
}

document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') {
        if (e.key === 'Escape') {
            e.target.blur();
            e.target.value = '';
            applyFilters();
        }
        return;
    }

    if (e.key === 'Escape') {
        closeLogModal();
        closeSSHSetup();
        closeDeployModal();
        closeROSGraphModal();
    }
});

function relativeUptime(timestamp) {
    if (!timestamp) return '';
    try {
        const ms = Date.now() - new Date(timestamp).getTime();
        if (ms < 0 || isNaN(ms)) return '';
        const sec = Math.floor(ms / 1000);
        if (sec < 60) return `${sec}s`;
        const min = Math.floor(sec / 60);
        if (min < 60) return `${min}m`;
        const hr = Math.floor(min / 60);
        const rm = min % 60;
        if (hr < 24) return `${hr}h ${rm}m`;
        const d = Math.floor(hr / 24);
        return `${d}d ${hr % 24}h`;
    } catch {
        return '';
    }
}

function formatMemory(bytes) {
    if (!bytes || bytes === '[not set]') return '';
    const n = parseInt(bytes, 10);
    if (isNaN(n)) return bytes;
    if (n < 1024) return `${n} B`;
    if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
    return `${(n / 1073741824).toFixed(1)} GB`;
}

function showToast(message, type = 'info') {
    let container = document.querySelector('.toast-container');
    if (!container) {
        container = document.createElement('div');
        container.className = 'toast-container';
        document.body.appendChild(container);
    }

    const iconMap = {success: 'check-circle', error: 'alert-circle', info: 'info', warning: 'alert-triangle'};
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `${icon(iconMap[type] || 'info')} <span>${message}</span>`;
    container.appendChild(toast);
    reinitIcons();

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(8px)';
        toast.style.transition = 'opacity .2s, transform .2s';
        setTimeout(() => toast.remove(), 250);
    }, 4000);
}

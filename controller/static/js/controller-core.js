const API_BASE = '';
let machines = [];
let nodesCache = {};
let statusPollingIntervals = {};
let systemInfoRefreshTimer = null;
let systemInfoRefreshInFlight = false;
let currentLogWebSocket = null;
let logLineCount = 0;
let logModalMachineId = null;
let logModalNodeId = null;
let collapsedMachines = JSON.parse(localStorage.getItem('collapsedMachines') || '{}');
let machineVolumeLevels = JSON.parse(localStorage.getItem('machineVolumeLevels') || '{}');
let serverOnline = false;
let robotControlMachineId = null;
let robotControlMachineName = '';
let robotControlPressedOrder = [];
let robotControlHeartbeat = null;
let robotControlCurrentDirection = null;
let robotControlLastSentDirection = null;
let robotControlSocket = null;
let robotControlReconnectTimer = null;
let robotControlReconnectAttempt = 0;
let robotControlConnectionInfo = null;
let robotControlShouldReconnect = false;
let robotControlCurrentMode = null;
let llmInputConnectionInfo = null;
let llmInputPending = false;
let currentModelModalMachineId = null;

function setServerStatus(online) {
    serverOnline = online;
    const el = document.getElementById('server-status');
    if (!el) return;
    const dot = el.querySelector('.server-dot');
    const text = el.querySelector('.server-status-text');
    if (online) {
        dot.className = 'server-dot online';
        text.textContent = 'Online';
    } else {
        dot.className = 'server-dot offline';
        text.textContent = 'Offline';
    }
}

async function checkServerHealth() {
    try {
        const resp = await fetch(`${API_BASE}/api/machines`, {signal: AbortSignal.timeout(5000)});
        setServerStatus(resp.ok);
        if (resp.ok) {
            const latestMachines = await resp.json();
            const connectionChanged = latestMachines.length !== machines.length
                || latestMachines.some(machine => {
                    const previous = machines.find(item => item.id === machine.id);
                    return !previous || previous.connected !== machine.connected;
                });
            machines = latestMachines;
            if (connectionChanged) {
                renderMachines(machines);
                startSystemInfoRefresh();
            }
            await refreshDiscoveryServer();
        }
    } catch {
        setServerStatus(false);
        const container = document.getElementById('discovery-status');
        const dot = document.getElementById('discovery-dot');
        const text = document.getElementById('discovery-status-text');
        if (container && dot && text) {
            container.classList.add('error');
            dot.className = 'server-dot offline';
            text.textContent = 'Controller offline';
        }
    }
}

function icon(name, cls = '') {
    return `<i data-lucide="${name}" class="${cls}"></i>`;
}

function reinitIcons() {
    if (window.lucide) lucide.createIcons();
}

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

async function apiCall(endpoint, options = {}) {
    const resp = await fetch(`${API_BASE}${endpoint}`, {
        ...options,
        headers: {'Content-Type': 'application/json', ...options.headers},
    });
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({detail: resp.statusText}));
        throw new Error(err.detail || 'API Error');
    }
    return resp.json();
}

const fetchMachines = () => apiCall('/api/machines');
const fetchNodesStatus = (mid) => apiCall(`/api/machines/${mid}/nodes/status`);
const fetchNodeStatus = (mid, nid) => apiCall(`/api/machines/${mid}/nodes/${nid}/status`);
const startNode = (mid, nid) => apiCall(`/api/machines/${mid}/nodes/${nid}/start`, {method: 'POST'});
const stopNode = (mid, nid) => apiCall(`/api/machines/${mid}/nodes/${nid}/stop`, {method: 'POST'});
const restartNode = (mid, nid) => apiCall(`/api/machines/${mid}/nodes/${nid}/restart`, {method: 'POST'});
const connectMachine = (mid) => apiCall(`/api/machines/${mid}/connect`, {method: 'POST'});
const startAllNodes = (mid) => apiCall(`/api/machines/${mid}/nodes/start-all`, {method: 'POST'});
const stopAllNodes = (mid) => apiCall(`/api/machines/${mid}/nodes/stop-all`, {method: 'POST'});
const fetchSystemInfo = (mid) => apiCall(`/api/machines/${mid}/system-info`);
const pullMachineGit = (mid) => apiCall(`/api/machines/${mid}/git/pull`, {method: 'POST'});
const fetchDiscoveryServer = () => apiCall('/api/discovery/server');
const restartDiscoveryServerApi = () => apiCall('/api/discovery/server/restart', {method: 'POST'});
const restartMachine = (mid) => apiCall(`/api/machines/${mid}/restart`, {method: 'POST'});
const shutdownMachine = (mid) => apiCall(`/api/machines/${mid}/shutdown`, {method: 'POST'});
const setMachineVolume = (mid, volume) => apiCall(`/api/machines/${mid}/volume`, {
    method: 'POST',
    body: JSON.stringify({volume})
});
const generateROSGraphApi = () => apiCall('/api/generate_ros_graph', {method: 'POST'});
const fetchRobotControlConnection = (mid) => apiCall(`/api/machines/${mid}/robot-control/connection`);
const fetchLlmInputConnection = () => apiCall('/api/llm-input/connection');

function openPlannerPage(machineId = null) {
    const selectedMachine = machineId
        ? machines.find((machine) => machine.id === machineId)
        : machines.find((machine) => machine.type === 'workstation');
    const params = new URLSearchParams();
    if (selectedMachine?.host) {
        params.set('host', selectedMachine.host);
    }
    window.location.href = `/static/planner.html${params.toString() ? `?${params.toString()}` : ''}`;
}

function openFacesPage() {
    window.location.href = '/faces';
}

function openVoicesPage() {
    window.location.href = '/voices';
}


let passwordModalResolve = null;
let passwordModalReject = null;

function openPasswordModal(options = {}) {
    const title = options.title || 'Sudo Password';
    const label = options.label || 'Enter sudo password:';
    const submitText = options.submitText || 'OK';

    document.getElementById('password-modal-title').textContent = title;
    document.getElementById('password-modal-label').textContent = label;
    document.getElementById('password-modal-submit').textContent = submitText;
    document.getElementById('password-modal-input').value = '';
    document.getElementById('password-modal').classList.remove('hidden');
    setTimeout(() => {
        document.getElementById('password-modal-input').focus();
    }, 100);
    return new Promise((resolve, reject) => {
        passwordModalResolve = resolve;
        passwordModalReject = reject;
    });
}

function openModelSelector(machineId) {
    currentModelModalMachineId = machineId;
    const modal = document.getElementById('model-select-modal');
    const loading = document.getElementById('model-select-loading');
    const form = document.getElementById('model-select-form');
    const dropdown = document.getElementById('model-select-dropdown');
    if (!modal || !loading || !form || !dropdown) return;
    modal.classList.remove('hidden');
    loading.style.display = 'flex';
    form.style.display = 'none';
    dropdown.innerHTML = '';
    apiCall(`/api/machines/${machineId}/llm/models`).then(resp => {
        const models = resp?.models || [];
        const currentModel = resp?.current_model;
        if (!models.length) {
            loading.querySelector('span').textContent = 'No models found';
            return;
        }
        models.forEach(m => {
            const opt = document.createElement('option');
            opt.value = m.name;
            opt.textContent = m.name;
            if (currentModel && m.name === currentModel) {
                opt.selected = true;
            }
            dropdown.appendChild(opt);
        });
        loading.style.display = 'none';
        form.style.display = 'block';
        reinitIcons();
    }).catch(e => {
        showToast(`Failed to list models: ${e.message || e}`, 'error');
        closeModelSelector();
    });
}

function closeModelSelector() {
    const modal = document.getElementById('model-select-modal');
    if (modal) modal.classList.add('hidden');
    currentModelModalMachineId = null;
}

async function modelSelectSave() {
    if (!currentModelModalMachineId) return;
    const dropdown = document.getElementById('model-select-dropdown');
    const saveBtn = document.getElementById('model-select-save');
    if (!dropdown || !saveBtn) return;
    const model = dropdown.value;
    if (!model) {
        showToast('Please choose a model', 'warning');
        return;
    }
    saveBtn.disabled = true;
    try {
        const res = await apiCall(`/api/machines/${currentModelModalMachineId}/llm/models/select`, {
            method: 'POST',
            body: JSON.stringify({model})
        });
        showToast(res.message || `Model set to ${model}`, 'success');
        const machineIdToRefresh = currentModelModalMachineId;
        closeModelSelector();
        // refresh node status for the machine to reflect changes
        await refreshMachineStatus(machineIdToRefresh);
    } catch (e) {
        showToast(`Failed to set model: ${e.message || e}`, 'error');
    } finally {
        saveBtn.disabled = false;
    }
}

function closePasswordModal() {
    document.getElementById('password-modal').classList.add('hidden');
    document.getElementById('password-modal-title').textContent = 'Sudo Password';
    document.getElementById('password-modal-label').textContent = 'Enter sudo password:';
    document.getElementById('password-modal-submit').textContent = 'OK';
    if (passwordModalReject) passwordModalReject('cancelled');
    passwordModalResolve = null;
    passwordModalReject = null;
}

function sanitizeVolume(rawVolume) {
    const parsed = parseInt(rawVolume, 10);
    if (Number.isNaN(parsed)) return null;
    return Math.max(0, Math.min(100, parsed));
}

function getSavedVolume(machineId) {
    const saved = machineVolumeLevels[machineId];
    const volume = sanitizeVolume(saved);
    return volume == null ? 50 : volume;
}

function setSavedVolume(machineId, volume) {
    machineVolumeLevels[machineId] = volume;
    localStorage.setItem('machineVolumeLevels', JSON.stringify(machineVolumeLevels));
}

function updateVolumeDisplay(machineId, rawVolume) {
    const volume = sanitizeVolume(rawVolume);
    if (volume == null) return null;
    const valueEl = document.getElementById(`volume-value-${machineId}`);
    if (valueEl) valueEl.textContent = `${volume}%`;
    setSavedVolume(machineId, volume);
    return volume;
}

document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('password-modal-form');
    const input = document.getElementById('password-modal-input');
    const cancel = document.getElementById('password-modal-cancel');

    if (form && input && cancel) {
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            const val = input.value;
            if (passwordModalResolve) passwordModalResolve(val);
            document.getElementById('password-modal').classList.add('hidden');
            passwordModalResolve = null;
            passwordModalReject = null;
        });
        cancel.addEventListener('click', () => {
            closePasswordModal();
        });
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closePasswordModal();
        });
    }
    // model selector buttons
    const modelCancel = document.getElementById('model-select-cancel');
    const modelSave = document.getElementById('model-select-save');
    if (modelCancel) modelCancel.addEventListener('click', () => closeModelSelector());
    if (modelSave) modelSave.addEventListener('click', () => modelSelectSave());
});

async function handleRestartMachine(machineId) {
    try {
        const password = await openPasswordModal({
            title: 'Restart Machine',
            label: `Enter sudo password to restart ${machineId}:`,
            submitText: 'Restart'
        });
        if (!password) {
            showToast('Restart cancelled: password required.', 'warning');
            return;
        }
        showToast(`Restarting ${machineId}...`, 'info');
        const r = await apiCall(`/api/machines/${machineId}/restart`, {
            method: 'POST',
            body: JSON.stringify({password})
        });
        showToast(r.message || r.status, r.success !== false ? 'success' : 'error');
    } catch (e) {
        if (e !== 'cancelled') showToast(`Failed: ${e.message || e}`, 'error');
    }
}

async function handleShutdownMachine(machineId) {
    try {
        const password = await openPasswordModal({
            title: 'Shutdown Machine',
            label: `Enter sudo password to shut down ${machineId}:`,
            submitText: 'Shutdown'
        });
        if (!password) {
            showToast('Shutdown cancelled: password required.', 'warning');
            return;
        }
        showToast(`Shutting down ${machineId}...`, 'info');
        const r = await apiCall(`/api/machines/${machineId}/shutdown`, {
            method: 'POST',
            body: JSON.stringify({password})
        });
        showToast(r.message || r.status, r.success !== false ? 'success' : 'error');
    } catch (e) {
        if (e !== 'cancelled') showToast(`Failed: ${e.message || e}`, 'error');
    }
}

function handleVolumeInput(machineId, rawVolume) {
    updateVolumeDisplay(machineId, rawVolume);
}

async function handleSetVolume(machineId, rawVolume) {
    const volume = updateVolumeDisplay(machineId, rawVolume);
    if (volume == null) {
        showToast('Invalid volume. Enter a number between 0 and 100.', 'error');
        return;
    }

    const slider = document.getElementById(`volume-slider-${machineId}`);
    if (slider) slider.disabled = true;

    try {
        const r = await setMachineVolume(machineId, volume);
        showToast(r.message, r.success ? 'success' : 'error');
    } catch (e) {
        showToast(`Failed: ${e.message || e}`, 'error');
    } finally {
        if (slider) slider.disabled = false;
    }
}

function renderDiscoveryServer(status) {
    const container = document.getElementById('discovery-status');
    const dot = document.getElementById('discovery-dot');
    const text = document.getElementById('discovery-status-text');
    if (!container || !dot || !text) return;

    container.classList.toggle('error', Boolean(status.last_error));
    dot.className = `server-dot ${status.running ? 'online' : 'offline'}`;
    text.textContent = status.running ? `${status.endpoint} · pid ${status.pid}` : (status.last_error || 'Stopped');
}

async function refreshDiscoveryServer() {
    try {
        const status = await fetchDiscoveryServer();
        renderDiscoveryServer(status);
    } catch (e) {
        console.error('Discovery status error:', e);
        const container = document.getElementById('discovery-status');
        const dot = document.getElementById('discovery-dot');
        const text = document.getElementById('discovery-status-text');
        if (container && dot && text) {
            container.classList.add('error');
            dot.className = 'server-dot offline';
            text.textContent = e.message;
        }
    }
}

async function restartDiscoveryServer() {
    const btn = document.getElementById('discovery-restart-btn');
    if (btn) btn.disabled = true;
    try {
        const status = await restartDiscoveryServerApi();
        renderDiscoveryServer(status);
        showToast('Discovery server restarted', 'success');
    } catch (e) {
        showToast(`Discovery restart failed: ${e.message}`, 'error');
        await refreshDiscoveryServer();
    } finally {
        if (btn) btn.disabled = false;
    }
}

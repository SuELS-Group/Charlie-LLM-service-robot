const DEFAULT_PLANNER_PORT = 8765;

let plannerState = null;
let plannerSocket = null;
let plannerReconnectTimer = null;
let plannerTarget = null;
let plannerReconnectAttempt = 0;
let selectedHistorySessionId = null;
let forceLiveView = false;
let plannerCopyTexts = {};
let plannerCopyCounter = 0;

window.copyPlannerText = async function (copyId) {
    const text = plannerCopyTexts[copyId] || '';
    if (!text) return;
    try {
        await navigator.clipboard.writeText(text);
    } catch (error) {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.setAttribute('readonly', '');
        textarea.style.position = 'fixed';
        textarea.style.left = '-9999px';
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
    }
};

window.viewHistorySession = function (sessionId) {
    selectedHistorySessionId = sessionId;
    forceLiveView = false;
    renderPlannerState();
    window.scrollTo({top: 0, behavior: 'smooth'});
};

window.resumeLiveSession = function () {
    selectedHistorySessionId = null;
    forceLiveView = true;
    renderPlannerState();
};

function plannerIcon(name, cls = '') {
    return `<i data-lucide="${name}" class="${cls}"></i>`;
}

function plannerReinitIcons() {
    if (window.lucide) {
        lucide.createIcons();
    }
}

function escapeHtml(value) {
    return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}

function formatTimestamp(value) {
    if (!value) return 'Unknown time';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short',
    }).format(date);
}

function relativeTime(value) {
    if (!value) return 'just now';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    const diffMs = date.getTime() - Date.now();
    const diffMinutes = Math.round(diffMs / 60000);
    if (Math.abs(diffMinutes) < 1) return 'just now';
    if (Math.abs(diffMinutes) < 60) {
        return new Intl.RelativeTimeFormat(undefined, {numeric: 'auto'}).format(diffMinutes, 'minute');
    }
    const diffHours = Math.round(diffMinutes / 60);
    if (Math.abs(diffHours) < 24) {
        return new Intl.RelativeTimeFormat(undefined, {numeric: 'auto'}).format(diffHours, 'hour');
    }
    const diffDays = Math.round(diffHours / 24);
    return new Intl.RelativeTimeFormat(undefined, {numeric: 'auto'}).format(diffDays, 'day');
}

function formatCount(value, noun) {
    const count = Number(value || 0);
    return `${count} ${noun}${count === 1 ? '' : 's'}`;
}

function formatContextValue(value) {
    const count = Number(value);
    if (!Number.isFinite(count)) return 'Unknown';
    return new Intl.NumberFormat().format(count);
}

function formatPercent(value) {
    if (!Number.isFinite(value)) return 'Unknown';
    return `${Math.round(value * 100)}%`;
}

function plannerStatusClass(status) {
    switch ((status || '').toLowerCase()) {
        case 'success':
        case 'reused':
        case 'answered':
            return 'success';
        case 'failure':
        case 'timeout':
        case 'blocked':
            return 'failure';
        case 'stopped':
        case 'awaiting_input':
        case 'refused':
        case 'superseded':
            return 'warning';
        case 'running':
        case 'planning':
        case 'executing':
        case 'responding':
        case 'active':
            return 'active';
        default:
            return 'idle';
    }
}

function countSessionSteps(session) {
    if (!Array.isArray(session?.plans)) return 0;
    return session.plans.reduce((count, plan) => count + ((plan.steps || []).length), 0);
}

function getLatestPlan(session) {
    if (!Array.isArray(session?.plans) || session.plans.length === 0) {
        return null;
    }
    return session.plans[session.plans.length - 1];
}

function getDisplayedSession(state) {
    const history = Array.isArray(state?.history) ? state.history : [];
    if (!selectedHistorySessionId) {
        if (state?.current_session) {
            forceLiveView = false;
            return {session: state.current_session, isHistory: false, isLastCompleted: false};
        }
        if (!forceLiveView && history.length > 0) {
            return {session: history[0], isHistory: true, isLastCompleted: true};
        }
        return {session: null, isHistory: false, isLastCompleted: false};
    }
    const historySession = history.find((session) => session.id === selectedHistorySessionId);
    if (historySession) {
        return {session: historySession, isHistory: true, isLastCompleted: false};
    }
    selectedHistorySessionId = null;
    return {session: state?.current_session || null, isHistory: false, isLastCompleted: false};
}

async function discoverPlannerTarget() {
    const params = new URLSearchParams(window.location.search);
    const host = params.get('host');
    const port = Number(params.get('port') || DEFAULT_PLANNER_PORT);
    if (host) {
        return {host, port};
    }

    const response = await fetch('/api/machines', {signal: AbortSignal.timeout(5000)});
    if (!response.ok) {
        throw new Error('Failed to load machines from controller');
    }
    const machines = await response.json();
    const workstation = machines.find((machine) => machine.type === 'workstation' && machine.host)
        || machines.find((machine) => machine.host);
    if (!workstation) {
        throw new Error('No workstation host configured');
    }
    return {host: workstation.host, port};
}

function plannerWebSocketUrl(target) {
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    return `${protocol}://${target.host}:${target.port}/ws/planner`;
}

function setConnectionStatus(status, detail = '') {
    const element = document.getElementById('planner-connection-status');
    const dot = element?.querySelector('.server-dot');
    const text = element?.querySelector('.server-status-text');
    if (!element || !dot || !text) return;

    if (status === 'connected') {
        dot.className = 'server-dot online';
        text.textContent = 'Live';
    } else if (status === 'connecting') {
        dot.className = 'server-dot';
        text.textContent = detail || 'Connecting...';
    } else {
        dot.className = 'server-dot offline';
        text.textContent = detail || 'Disconnected';
    }
}

function setPlannerEndpointText(text) {
    const endpoint = document.getElementById('planner-endpoint');
    if (endpoint) {
        endpoint.textContent = text;
    }
}

function clearReconnectTimer() {
    if (plannerReconnectTimer) {
        window.clearTimeout(plannerReconnectTimer);
        plannerReconnectTimer = null;
    }
}

function closePlannerSocket({suppressReconnect = true} = {}) {
    if (!plannerSocket) {
        return;
    }
    plannerSocket._suppressReconnect = suppressReconnect;
    plannerSocket.close();
    plannerSocket = null;
}

function scheduleReconnect() {
    if (plannerReconnectTimer) return;
    const delayMs = Math.min(5000, 1000 * (plannerReconnectAttempt + 1));
    plannerReconnectTimer = window.setTimeout(() => {
        plannerReconnectTimer = null;
        connectPlannerSocket();
    }, delayMs);
}

async function connectPlannerSocket() {
    clearReconnectTimer();
    setConnectionStatus('connecting');

    if (!plannerTarget) {
        try {
            plannerTarget = await discoverPlannerTarget();
        } catch (error) {
            setConnectionStatus('disconnected', 'Discovery failed');
            setPlannerEndpointText(error.message || String(error));
            scheduleReconnect();
            return;
        }
    }

    const url = plannerWebSocketUrl(plannerTarget);
    setPlannerEndpointText(url);
    closePlannerSocket();

    const socket = new WebSocket(url);
    plannerSocket = socket;

    socket.addEventListener('open', () => {
        if (plannerSocket !== socket) return;
        plannerReconnectAttempt = 0;
        clearReconnectTimer();
        setConnectionStatus('connected');
    });

    socket.addEventListener('message', (event) => {
        if (plannerSocket !== socket) return;
        try {
            plannerState = JSON.parse(event.data);
            if (plannerState?.current_session) {
                forceLiveView = false;
            }
            renderPlannerState();
        } catch (error) {
            console.error('Failed to parse planner state', error);
        }
    });

    socket.addEventListener('close', () => {
        if (plannerSocket === socket) {
            plannerSocket = null;
        }
        if (socket._suppressReconnect) {
            return;
        }
        plannerReconnectAttempt += 1;
        setConnectionStatus('disconnected', 'Reconnecting...');
        scheduleReconnect();
    });

    socket.addEventListener('error', () => {
        if (plannerSocket !== socket) return;
        setConnectionStatus('disconnected', 'Connection error');
    });
}

let voiceActivitySocket = null;
let voiceActivityReconnectTimer = null;
let voiceActivityStates = {};

function renderVoiceActivity() {
    const border = document.getElementById('voice-activity-indicator');
    if (!border) return;
    const active = Object.values(voiceActivityStates)
        .some(status => status.available && status.active);
    border.classList.toggle('active', active);
}

function voiceActivitySocketUrl() {
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    return `${protocol}://${window.location.host}/ws/voice-activity`;
}

function connectVoiceActivitySocket() {
    if (voiceActivitySocket && voiceActivitySocket.readyState < WebSocket.CLOSING) return;
    const socket = new WebSocket(voiceActivitySocketUrl());
    voiceActivitySocket = socket;
    socket.onmessage = event => {
        try {
            const status = JSON.parse(event.data);
            if (status.type !== 'voice_activity' || !status.machine_id) return;
            voiceActivityStates[status.machine_id] = {
                available: Boolean(status.available),
                active: Boolean(status.available && status.active),
            };
            renderVoiceActivity();
        } catch (_) {
            // Ignore malformed bridge messages and keep the last valid state.
        }
    };
    socket.onclose = () => {
        if (voiceActivitySocket === socket) voiceActivitySocket = null;
        voiceActivityStates = {};
        renderVoiceActivity();
        clearTimeout(voiceActivityReconnectTimer);
        voiceActivityReconnectTimer = setTimeout(connectVoiceActivitySocket, 2000);
    };
}

function initVoiceActivityIndicator() {
    renderVoiceActivity();
    connectVoiceActivitySocket();
}

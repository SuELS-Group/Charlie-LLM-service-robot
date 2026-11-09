let greetingModeRefreshTimer = null;

async function fetchGreetingMode() {
    return apiCall('/api/greeting-mode');
}

async function setGreetingMode(enabled) {
    return apiCall('/api/greeting-mode', {
        method: 'POST',
        body: JSON.stringify({enabled}),
    });
}

function updateGreetingModeButton(state) {
    const button = document.getElementById('greeting-mode-toggle');
    if (!button) return;

    const enabled = Boolean(state.enabled);
    const nodeRunning = Boolean(state.node_running);

    button.dataset.enabled = enabled ? 'true' : 'false';
    button.dataset.nodeRunning = nodeRunning ? 'true' : 'false';
    button.classList.remove('btn-success', 'btn-ghost');

    if (!nodeRunning) {
        button.disabled = true;
        button.classList.add('btn-ghost');
        button.title = 'Greeting node is not running';
        button.innerHTML = `${icon('user-x')} <span>Greeting Offline</span>`;
        reinitIcons();
        return;
    }

    button.disabled = false;

    if (enabled) {
        button.classList.add('btn-success');
        button.title = 'Greeting Mode is ON';
        button.innerHTML = `${icon('user-round-check')} <span>Greeting On</span>`;
    } else {
        button.classList.add('btn-ghost');
        button.title = 'Greeting Mode is OFF';
        button.innerHTML = `${icon('user-round')} <span>Greeting Off</span>`;
    }

    reinitIcons();
}

async function refreshGreetingMode() {
    try {
        const state = await fetchGreetingMode();
        updateGreetingModeButton(state);
    } catch (error) {
        const button = document.getElementById('greeting-mode-toggle');
        if (!button) return;

        button.disabled = true;
        button.dataset.enabled = 'false';
        button.dataset.nodeRunning = 'false';
        button.classList.remove('btn-success');
        button.classList.add('btn-ghost');
        button.title = `Greeting Mode unavailable: ${error.message}`;
        button.innerHTML = `${icon('user-x')} <span>Greeting Offline</span>`;
        reinitIcons();
    }
}

async function toggleGreetingMode() {
    const button = document.getElementById('greeting-mode-toggle');
    if (!button || button.disabled) return;

    if (button.dataset.nodeRunning !== 'true') {
        showToast('Greeting node is not running', 'warning');
        return;
    }

    const current = button.dataset.enabled === 'true';
    button.disabled = true;

    try {
        const next = await setGreetingMode(!current);
        updateGreetingModeButton(next);

        const enabled = Boolean(next.enabled);
        showToast(`Greeting Mode ${enabled ? 'enabled' : 'disabled'}`, enabled ? 'success' : 'info');
    } catch (error) {
        showToast(`Greeting Mode failed: ${error.message}`, 'error');
        await refreshGreetingMode();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    refreshGreetingMode();

    if (greetingModeRefreshTimer) {
        clearInterval(greetingModeRefreshTimer);
    }

    greetingModeRefreshTimer = setInterval(refreshGreetingMode, 3000);
});

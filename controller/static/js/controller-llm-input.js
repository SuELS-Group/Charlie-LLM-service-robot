function buildLlmInputUrl(connection) {
    const protocol = window.location.protocol === 'https:' ? 'https' : 'http';
    return `${protocol}://${connection.host}:${connection.port}${connection.path}`;
}

function setLlmInputStatus(label, tone = 'offline') {
    const status = document.getElementById('llm-input-status');
    const submit = document.getElementById('llm-input-submit');
    if (status) {
        status.textContent = label;
        status.classList.toggle('is-live', tone === 'live');
        status.classList.toggle('is-warning', tone === 'warning');
        status.classList.toggle('is-offline', tone === 'offline');
    }
    if (submit) {
        submit.disabled = llmInputPending;
    }
}

function setLlmInputTarget(text) {
    const target = document.getElementById('llm-input-target');
    if (target) {
        target.textContent = text;
    }
}

async function ensureLlmInputConnection() {
    if (llmInputConnectionInfo) {
        return llmInputConnectionInfo;
    }
    setLlmInputStatus('Resolving', 'warning');
    llmInputConnectionInfo = await fetchLlmInputConnection();
    setLlmInputTarget(`${llmInputConnectionInfo.workstation_id} · ${buildLlmInputUrl(llmInputConnectionInfo)}`);
    setLlmInputStatus('Ready', 'live');
    return llmInputConnectionInfo;
}

async function openLlmInputModal() {
    document.getElementById('llm-input-modal')?.classList.remove('hidden');
    setTimeout(() => document.getElementById('llm-input-text')?.focus(), 80);
    reinitIcons();
    try {
        await ensureLlmInputConnection();
    } catch (error) {
        setLlmInputStatus('Unavailable', 'offline');
        setLlmInputTarget(error.message || 'Could not find web bridge');
        showToast(`LLM input unavailable: ${error.message}`, 'error');
    }
}

function closeLlmInputModal() {
    document.getElementById('llm-input-modal')?.classList.add('hidden');
}

async function submitLlmInput(event) {
    event.preventDefault();
    const textarea = document.getElementById('llm-input-text');
    const text = (textarea?.value || '').trim();
    if (!text) {
        showToast('Enter a message first', 'warning');
        return;
    }

    let failed = false;
    try {
        const connection = await ensureLlmInputConnection();
        llmInputPending = true;
        setLlmInputStatus('Sending', 'warning');
        const response = await fetch(buildLlmInputUrl(connection), {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text}),
            signal: AbortSignal.timeout(8000),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(payload.error || payload.detail || response.statusText);
        }
        if (textarea) {
            textarea.value = '';
            textarea.focus();
        }
        showToast('Sent to LLM', 'success');
        setLlmInputStatus('Ready', 'live');
    } catch (error) {
        failed = true;
        showToast(`Send failed: ${error.message}`, 'error');
    } finally {
        llmInputPending = false;
        if (failed) {
            setLlmInputStatus('Error', 'offline');
        } else if (llmInputConnectionInfo) {
            setLlmInputStatus('Ready', 'live');
        }
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('llm-input-form')?.addEventListener('submit', submitLlmInput);
});

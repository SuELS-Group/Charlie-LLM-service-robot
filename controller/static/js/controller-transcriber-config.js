let transcriberConfigMachineId = null;

function _linesToList(text) {
    return text.split('\n').map(s => s.trim()).filter(s => s.length > 0);
}

async function openTranscriberConfigModal(machineId) {
    transcriberConfigMachineId = machineId;
    const modal = document.getElementById('transcriber-config-modal');
    const loading = document.getElementById('transcriber-config-loading');
    const form = document.getElementById('transcriber-config-form');
    const errorEl = document.getElementById('transcriber-config-error');

    modal.classList.remove('hidden');
    loading.style.display = '';
    form.style.display = 'none';
    errorEl.style.display = 'none';
    reinitIcons();

    try {
        const resp = await fetch(`/api/machines/${encodeURIComponent(machineId)}/transcriber/config`);
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            throw new Error(d.detail || `HTTP ${resp.status}`);
        }
        const cfg = await resp.json();

        const beamSize = typeof cfg.beam_size === 'number' ? cfg.beam_size : 5;
        document.getElementById('transcriber-beam').value = beamSize;
        document.getElementById('transcriber-beam-value').textContent = beamSize;

        document.getElementById('transcriber-wake-words').value = (cfg.wake_words || []).join('\n');
        document.getElementById('transcriber-stop-keywords').value = (cfg.stop_keywords || []).join('\n');

        loading.style.display = 'none';
        form.style.display = '';
        reinitIcons();
    } catch (e) {
        loading.style.display = 'none';
        form.style.display = '';
        errorEl.textContent = `Failed to load config: ${e.message}`;
        errorEl.style.display = '';
        reinitIcons();
    }
}

function closeTranscriberConfigModal() {
    document.getElementById('transcriber-config-modal').classList.add('hidden');
    transcriberConfigMachineId = null;
}

async function saveTranscriberConfig() {
    if (!transcriberConfigMachineId) return;
    const errorEl = document.getElementById('transcriber-config-error');
    errorEl.style.display = 'none';

    const beam_size = parseInt(document.getElementById('transcriber-beam').value, 10);
    const wake_words = _linesToList(document.getElementById('transcriber-wake-words').value);
    const stop_keywords = _linesToList(document.getElementById('transcriber-stop-keywords').value);

    if (wake_words.length === 0) {
        errorEl.textContent = 'At least one wake word is required.';
        errorEl.style.display = '';
        return;
    }

    try {
        const resp = await fetch(`/api/machines/${encodeURIComponent(transcriberConfigMachineId)}/transcriber/config`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({beam_size, wake_words, stop_keywords}),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.success === false) {
            throw new Error(data.detail || data.message || `HTTP ${resp.status}`);
        }
        closeTranscriberConfigModal();
    } catch (e) {
        errorEl.textContent = `Failed to save config: ${e.message}`;
        errorEl.style.display = '';
    }
}

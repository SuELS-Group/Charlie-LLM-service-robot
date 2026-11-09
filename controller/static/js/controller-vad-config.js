let vadConfigMachineId = null;

async function openVadConfigModal(machineId) {
    vadConfigMachineId = machineId;
    const modal = document.getElementById('vad-config-modal');
    const loading = document.getElementById('vad-config-loading');
    const form = document.getElementById('vad-config-form');
    const errorEl = document.getElementById('vad-config-error');

    modal.classList.remove('hidden');
    loading.style.display = '';
    form.style.display = 'none';
    errorEl.style.display = 'none';
    reinitIcons();

    try {
        const resp = await fetch(`/api/machines/${encodeURIComponent(machineId)}/vad/config`);
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            throw new Error(d.detail || `HTTP ${resp.status}`);
        }
        const cfg = await resp.json();

        document.getElementById('vad-activity-enabled').checked = cfg.activity_enabled !== false;

        const threshold = typeof cfg.threshold === 'number' ? cfg.threshold : 0.5;
        const thresholdPct = Math.round(threshold * 100);
        document.getElementById('vad-threshold').value = thresholdPct;
        document.getElementById('vad-threshold-value').textContent = threshold.toFixed(2);

        const silence = typeof cfg.min_silence_duration_ms === 'number' ? cfg.min_silence_duration_ms : 1300;
        document.getElementById('vad-silence').value = silence;
        document.getElementById('vad-silence-value').textContent = silence;

        const pad = typeof cfg.speech_pad_ms === 'number' ? cfg.speech_pad_ms : 200;
        document.getElementById('vad-pad').value = pad;
        document.getElementById('vad-pad-value').textContent = pad;

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

function closeVadConfigModal() {
    document.getElementById('vad-config-modal').classList.add('hidden');
    vadConfigMachineId = null;
}

async function saveVadConfig() {
    if (!vadConfigMachineId) return;
    const errorEl = document.getElementById('vad-config-error');
    errorEl.style.display = 'none';

    const threshold = parseInt(document.getElementById('vad-threshold').value, 10) / 100;
    const min_silence_duration_ms = parseInt(document.getElementById('vad-silence').value, 10);
    const speech_pad_ms = parseInt(document.getElementById('vad-pad').value, 10);
    const activity_enabled = document.getElementById('vad-activity-enabled').checked;

    try {
        const resp = await fetch(`/api/machines/${encodeURIComponent(vadConfigMachineId)}/vad/config`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({threshold, min_silence_duration_ms, speech_pad_ms, activity_enabled}),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.success === false) {
            throw new Error(data.detail || data.message || `HTTP ${resp.status}`);
        }
        const restartResult = await restartNode(vadConfigMachineId, 'voice_detection');
        if (!restartResult.success) {
            throw new Error(restartResult.message || 'Failed to restart Voice Detection');
        }
        showToast('Voice Detection configuration saved and restarted', 'success');
        closeVadConfigModal();
    } catch (e) {
        errorEl.textContent = `Failed to save config: ${e.message}`;
        errorEl.style.display = '';
    }
}

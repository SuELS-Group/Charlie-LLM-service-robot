let voiceIdentityConfigMachineId = null;

async function openVoiceIdentityConfigModal(machineId) {
    voiceIdentityConfigMachineId = machineId;
    const modal = document.getElementById('voice-identity-config-modal');
    const loading = document.getElementById('voice-identity-config-loading');
    const form = document.getElementById('voice-identity-config-form');
    const errorEl = document.getElementById('voice-identity-config-error');

    modal.classList.remove('hidden');
    loading.style.display = '';
    form.style.display = 'none';
    errorEl.style.display = 'none';
    reinitIcons();

    try {
        const resp = await fetch(`/api/machines/${encodeURIComponent(machineId)}/voice_identity/config`);
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            throw new Error(d.detail || `HTTP ${resp.status}`);
        }
        const cfg = await resp.json();

        const threshold = typeof cfg.threshold === 'number' ? cfg.threshold : 0.45;
        document.getElementById('vi-threshold').value = Math.round(threshold * 100);
        document.getElementById('vi-threshold-value').textContent = threshold.toFixed(2);

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

function closeVoiceIdentityConfigModal() {
    document.getElementById('voice-identity-config-modal').classList.add('hidden');
    voiceIdentityConfigMachineId = null;
}

async function saveVoiceIdentityConfig() {
    if (!voiceIdentityConfigMachineId) return;
    const errorEl = document.getElementById('voice-identity-config-error');
    errorEl.style.display = 'none';

    const threshold = parseInt(document.getElementById('vi-threshold').value, 10) / 100;

    try {
        const resp = await fetch(`/api/machines/${encodeURIComponent(voiceIdentityConfigMachineId)}/voice_identity/config`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({threshold}),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.success === false) {
            throw new Error(data.detail || data.message || `HTTP ${resp.status}`);
        }
        closeVoiceIdentityConfigModal();
    } catch (e) {
        errorEl.textContent = `Failed to save config: ${e.message}`;
        errorEl.style.display = '';
    }
}

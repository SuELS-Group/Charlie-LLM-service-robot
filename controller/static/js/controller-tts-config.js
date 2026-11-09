let ttsConfigMachineId = null;

const TTS_SOUND_LABELS = {
    'thinking-sound': 'Thinking Sound',
};

async function openTtsConfigModal(machineId) {
    ttsConfigMachineId = machineId;
    const modal = document.getElementById('tts-config-modal');
    const loading = document.getElementById('tts-config-loading');
    const form = document.getElementById('tts-config-form');
    const errorEl = document.getElementById('tts-config-error');
    const uploadStatus = document.getElementById('tts-upload-status');

    modal.classList.remove('hidden');
    loading.style.display = '';
    form.style.display = 'none';
    errorEl.style.display = 'none';
    uploadStatus.style.display = 'none';
    document.getElementById('tts-sound-upload-input').value = '';
    reinitIcons();

    try {
        const [configResp, soundsResp] = await Promise.all([
            fetch(`/api/machines/${encodeURIComponent(machineId)}/tts/config`),
            fetch(`/api/machines/${encodeURIComponent(machineId)}/tts/sounds`),
        ]);
        if (!configResp.ok) {
            const d = await configResp.json().catch(() => ({}));
            throw new Error(d.detail || `HTTP ${configResp.status}`);
        }
        const configData = await configResp.json();
        const soundsData = soundsResp.ok ? await soundsResp.json() : {sounds: []};

        _populateSoundDropdown(soundsData.sounds, configData.thinking_sound);
        document.getElementById('tts-feedback-sounds-toggle').checked = !!configData.enable_feedback_sounds;
        const vol = typeof configData.sounds_volume === 'number' ? configData.sounds_volume : 70;
        document.getElementById('tts-volume-slider').value = vol;
        document.getElementById('tts-volume-value').textContent = vol;

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

function _populateSoundDropdown(stems, selectedStem) {
    const sel = document.getElementById('tts-thinking-sound-select');
    sel.innerHTML = '';
    stems.forEach(stem => {
        const opt = document.createElement('option');
        opt.value = stem;
        opt.textContent = TTS_SOUND_LABELS[stem] || stem;
        sel.appendChild(opt);
    });
    if (selectedStem && stems.includes(selectedStem)) {
        sel.value = selectedStem;
    } else if (stems.length > 0) {
        sel.value = stems[0];
    }
}

function closeTtsConfigModal() {
    document.getElementById('tts-config-modal').classList.add('hidden');
    ttsConfigMachineId = null;
}

async function saveTtsConfig() {
    if (!ttsConfigMachineId) return;
    const errorEl = document.getElementById('tts-config-error');
    errorEl.style.display = 'none';
    const enableFeedbackSounds = document.getElementById('tts-feedback-sounds-toggle').checked;
    const thinkingSound = document.getElementById('tts-thinking-sound-select').value;
    const soundsVolume = parseInt(document.getElementById('tts-volume-slider').value, 10);
    try {
        const resp = await fetch(`/api/machines/${encodeURIComponent(ttsConfigMachineId)}/tts/config`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enable_feedback_sounds: enableFeedbackSounds, thinking_sound: thinkingSound, sounds_volume: soundsVolume}),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.success === false) {
            throw new Error(data.detail || data.message || `HTTP ${resp.status}`);
        }
        closeTtsConfigModal();
    } catch (e) {
        errorEl.textContent = `Failed to save config: ${e.message}`;
        errorEl.style.display = '';
    }
}

async function uploadTtsSound() {
    if (!ttsConfigMachineId) return;
    const input = document.getElementById('tts-sound-upload-input');
    const statusEl = document.getElementById('tts-upload-status');
    const btn = document.getElementById('tts-sound-upload-btn');
    const file = input.files[0];
    if (!file) return;

    btn.disabled = true;
    statusEl.style.color = 'var(--text-2)';
        statusEl.textContent = 'Uploading...';

    try {
        const formData = new FormData();
        formData.append('file', file);
        const resp = await fetch(`/api/machines/${encodeURIComponent(ttsConfigMachineId)}/tts/sounds`, {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.success === false) {
            throw new Error(data.detail || data.message || `HTTP ${resp.status}`);
        }
        statusEl.style.color = 'var(--green)';
        statusEl.textContent = `✓ ${file.name} uploaded`;
        input.value = '';

        // Refresh sound list and select the uploaded sound
        const soundsResp = await fetch(`/api/machines/${encodeURIComponent(ttsConfigMachineId)}/tts/sounds`);
        if (soundsResp.ok) {
            const soundsData = await soundsResp.json();
            const stem = file.name.replace(/\.mp3$/i, '');
            _populateSoundDropdown(soundsData.sounds, stem);
        }
    } catch (e) {
        statusEl.style.color = 'var(--red)';
        statusEl.textContent = `Error: ${e.message}`;
    } finally {
        btn.disabled = false;
    }
}

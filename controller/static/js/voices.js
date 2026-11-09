let voicesBusy = false;
let voicesCache = null;
let voiceSource = 'record';
let voiceStream = null;
let voiceAudioContext = null;
let voiceSourceNode = null;
let voiceProcessorNode = null;
let voiceRecordedChunks = [];
let voiceRecordingSampleRate = 44100;
let voiceRecordingStartedAt = null;
let voiceRecordingTimer = null;
let voiceRecordingDataUrl = null;
let voiceRecordingBlobUrl = null;
let voiceUserMode = 'existing';
const NEW_VOICE_USER_VALUE = '__new_voice_user__';
let voiceSupportedExtensions = ['.wav', '.flac', '.mp3', '.m4a', '.mp4', '.aac', '.ogg', '.oga', '.opus', '.webm', '.weba', '.aif', '.aiff', '.au', '.caf', '.wma'];

function voiceIcon(name, cls = '') {
    return `<i data-lucide="${name}" class="${cls}"></i>`;
}

function voicesReinitIcons() {
    if (window.lucide) lucide.createIcons();
}

function voicesEscape(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function voicesSafeFileStem(value) {
    const stem = String(value ?? '')
        .trim()
        .replace(/[^A-Za-z0-9_.-]+/g, '-')
        .replace(/^[._-]+|[._-]+$/g, '');
    return (stem || 'speaker').toLowerCase();
}

function findExistingVoiceUser(name) {
    if (!voicesCache?.users) return null;
    const target = voicesSafeFileStem(name);
    return voicesCache.users.find((user) => voicesSafeFileStem(user.name) === target) || null;
}

function detectedVoiceUsers() {
    return [...(voicesCache?.users || [])].sort((a, b) => a.name.localeCompare(b.name));
}

function populateVoiceUserSelect(selectedName = '') {
    const select = document.getElementById('voices-user-select');
    if (!select) return;
    const users = detectedVoiceUsers();
    const existing = selectedName ? findExistingVoiceUser(selectedName) : null;
    const currentValue = select.value && select.value !== NEW_VOICE_USER_VALUE ? select.value : '';
    const wantsNew = Boolean(selectedName && !existing) || voiceUserMode === 'new';
    const selectedValue = wantsNew ? NEW_VOICE_USER_VALUE : (existing?.name || currentValue || users[0]?.name || NEW_VOICE_USER_VALUE);
    const existingOptions = users.map((user) => `<option value="${voicesEscape(user.name)}">${voicesEscape(user.name)}</option>`).join('');
    const newOption = `<option value="${NEW_VOICE_USER_VALUE}">+ New speaker...</option>`;
    select.innerHTML = users.length ? `${existingOptions}${newOption}` : newOption;
    select.value = selectedValue;
    if (!users.length || select.value === NEW_VOICE_USER_VALUE || !findExistingVoiceUser(select.value)) {
        select.value = NEW_VOICE_USER_VALUE;
    }
    setVoiceUserMode(select.value === NEW_VOICE_USER_VALUE ? 'new' : 'existing');
}

function setVoiceFileAccept(extensions) {
    const input = document.getElementById('voices-file-input');
    const help = document.getElementById('voices-file-help');
    if (!input) return;
    const values = Array.isArray(extensions) && extensions.length ? extensions : voiceSupportedExtensions;
    voiceSupportedExtensions = values;
    input.accept = ['audio/*', ...values].join(',');
    if (help) {
        help.textContent = `Accepted: ${values.join(', ')}. WAV/FLAC are safest; other formats require a torchaudio backend that can decode them.`;
    }
}

function setVoiceUserMode(mode) {
    const select = document.getElementById('voices-user-select');
    const nameInput = document.getElementById('voices-name-input');
    const newLabel = document.getElementById('voices-new-user-label');
    const help = document.getElementById('voices-speaker-help');
    const users = detectedVoiceUsers();
    const selectedExisting = select?.value && select.value !== NEW_VOICE_USER_VALUE ? findExistingVoiceUser(select.value) : null;
    voiceUserMode = mode === 'new' || !users.length || !selectedExisting ? 'new' : 'existing';
    if (select) {
        if (voiceUserMode === 'new') {
            select.value = NEW_VOICE_USER_VALUE;
        } else if (selectedExisting) {
            select.value = selectedExisting.name;
        }
    }
    if (newLabel) newLabel.classList.toggle('hidden', voiceUserMode !== 'new');
    if (nameInput) nameInput.required = voiceUserMode === 'new';
    if (help) {
        help.textContent = voiceUserMode === 'new'
            ? 'Enter a new speaker name. The saved file will use this name as its prefix.'
            : 'The sample will be added to the selected speaker.';
    }
}

function currentVoiceUserName() {
    const select = document.getElementById('voices-user-select');
    if (voiceUserMode === 'existing' && select?.value !== NEW_VOICE_USER_VALUE) {
        return select?.value?.trim() || '';
    }
    return document.getElementById('voices-name-input')?.value?.trim() || '';
}

function voicesSetStatus(online, text) {
    const status = document.getElementById('voices-status');
    if (!status) return;
    status.querySelector('.server-dot').className = `server-dot ${online ? 'online' : 'offline'}`;
    status.querySelector('.server-status-text').textContent = text;
}

async function voicesApi(endpoint, options = {}) {
    const response = await fetch(endpoint, {
        ...options,
        headers: {'Content-Type': 'application/json', ...options.headers},
    });
    if (!response.ok) {
        const error = await response.json().catch(() => ({detail: response.statusText}));
        throw new Error(error.detail || 'API Error');
    }
    return response.json();
}

function formatBytes(bytes) {
    const value = Number(bytes || 0);
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function renderVoicesMeta(data) {
    document.getElementById('voices-db-path').textContent = data.db_path || '';
    document.getElementById('voices-subtitle').textContent =
        `${data.db_count} DB user${data.db_count === 1 ? '' : 's'} and ${data.file_count} sample file${data.file_count === 1 ? '' : 's'} in ${data.sample_dir}.`;
}

function renderVoices(data) {
    voicesCache = data;
    setVoiceFileAccept(data.supported_extensions);
    populateVoiceUserSelect(currentVoiceUserName());
    renderVoicesMeta(data);
    const grid = document.getElementById('voices-grid');
    if (!data.users.length) {
        grid.innerHTML = `
            <div class="faces-empty-state">
                ${voiceIcon('audio-lines')}
                <p>No speakers or sample files found.</p>
            </div>`;
        voicesReinitIcons();
        return;
    }
    grid.innerHTML = data.users.map((user) => {
        const fileRows = user.files?.length ? user.files.map((file) => `
            <div class="voice-file-row">
                <div class="voice-file-main">
                    <div class="voice-file-name" title="${voicesEscape(file.filename)}">${voicesEscape(file.filename)}</div>
                    <div class="voice-file-meta">${formatBytes(file.size_bytes)}${file.modified_at ? ` · ${voicesEscape(file.modified_at.slice(0, 19).replace('T', ' '))}` : ''}</div>
                    <audio controls preload="none" src="${voicesEscape(file.url)}"></audio>
                </div>
                <button class="btn btn-danger btn-sm voice-file-remove-btn" data-filename="${voicesEscape(file.filename)}" title="Delete sample">
                    ${voiceIcon('trash-2')}
                </button>
            </div>
        `).join('') : `<div class="voice-empty-files">No sample files for this DB user yet.</div>`;
        return `
            <article class="person-card voice-user-card">
                <div class="person-card-header">
                    <div>
                        <strong class="person-name">${voicesEscape(user.name)}</strong>
                        <span class="person-meta">${user.files.length} file${user.files.length === 1 ? '' : 's'} · ${user.db_sample_count} DB embedding${user.db_sample_count === 1 ? '' : 's'}</span>
                        <div class="voice-badges">
                            <span class="voice-badge ${user.in_db ? 'ok' : 'warn'}">${voiceIcon(user.in_db ? 'check-circle-2' : 'triangle-alert')} ${user.in_db ? 'In DB' : 'Needs build'}</span>
                            ${user.updated_at ? `<span class="voice-badge">Updated ${voicesEscape(user.updated_at.slice(0, 10))}</span>` : ''}
                        </div>
                    </div>
                    <div class="voice-user-actions">
                        <button class="btn btn-ghost btn-sm voice-user-add-btn" data-name="${voicesEscape(user.name)}" title="Add sample">
                            ${voiceIcon('plus')}
                        </button>
                        <button class="btn btn-danger btn-sm voice-user-remove-btn" data-name="${voicesEscape(user.name)}" title="Delete this user's sample files" ${user.files.length ? '' : 'disabled'}>
                            ${voiceIcon('trash-2')}
                        </button>
                    </div>
                </div>
                <div class="voice-file-list">${fileRows}</div>
            </article>
        `;
    }).join('');
    grid.querySelectorAll('.voice-file-remove-btn').forEach((button) => {
        button.addEventListener('click', () => removeVoiceFile(button.dataset.filename));
    });
    grid.querySelectorAll('.voice-user-add-btn').forEach((button) => {
        button.addEventListener('click', () => openVoiceDialog(button.dataset.name || ''));
    });
    grid.querySelectorAll('.voice-user-remove-btn').forEach((button) => {
        button.addEventListener('click', () => removeVoiceUserFiles(button.dataset.name || ''));
    });
    voicesReinitIcons();
}

async function loadVoices() {
    try {
        voicesSetStatus(true, 'Loading...');
        renderVoices(await voicesApi('/api/voices'));
        voicesSetStatus(true, 'Online');
    } catch (error) {
        voicesSetStatus(false, 'Error');
        document.getElementById('voices-grid').innerHTML = `
            <div class="faces-empty-state">
                ${voiceIcon('circle-alert')}
                <p>${voicesEscape(error.message)}</p>
            </div>`;
        voicesReinitIcons();
    }
}

async function removeVoiceFile(filename) {
    if (voicesBusy) return;
    if (!window.confirm(`Delete voice sample "${filename}"?`)) return;
    voicesBusy = true;
    try {
        renderVoices(await voicesApi(`/api/voices/samples/${encodeURIComponent(filename)}`, {method: 'DELETE'}));
        voicesSetStatus(true, 'Online');
    } catch (error) {
        voicesSetStatus(false, 'Error');
        window.alert(error.message);
    } finally {
        voicesBusy = false;
    }
}

async function removeVoiceUserFiles(name) {
    if (voicesBusy || !name) return;
    if (!window.confirm(`Delete all voice samples for "${name}"? The DB entry remains until you rebuild.`)) return;
    voicesBusy = true;
    try {
        renderVoices(await voicesApi(`/api/voices/users/${encodeURIComponent(name)}/samples`, {method: 'DELETE'}));
        voicesSetStatus(true, 'Online');
    } catch (error) {
        voicesSetStatus(false, 'Error');
        window.alert(error.message);
    } finally {
        voicesBusy = false;
    }
}

async function removeAllVoiceFiles() {
    if (voicesBusy) return;
    if (!voicesCache?.file_count) {
        window.alert('No voice sample files to remove.');
        return;
    }
    if (!window.confirm('Delete all voice sample files? DB users remain until you rebuild.')) return;
    voicesBusy = true;
    voicesSetStatus(true, 'Removing samples...');
    try {
        renderVoices(await voicesApi('/api/voices/samples', {method: 'DELETE'}));
        voicesSetStatus(true, 'Online');
    } catch (error) {
        voicesSetStatus(false, 'Error');
        window.alert(error.message);
    } finally {
        voicesBusy = false;
    }
}

async function regenerateVoices() {
    if (voicesBusy) return;
    voicesBusy = true;
    voicesSetStatus(true, 'Building DB...');
    try {
        const data = await voicesApi('/api/voices/regenerate', {method: 'POST'});
        renderVoices(data);
        const failed = data.builder_result?.failed?.length || 0;
        voicesSetStatus(true, failed ? `Built with ${failed} failed` : 'Online');
        if (failed) {
            window.alert(`${failed} sample file(s) failed during DB build. Check the server logs or API response.`);
        }
    } catch (error) {
        voicesSetStatus(false, 'Error');
        window.alert(error.message);
    } finally {
        voicesBusy = false;
    }
}

function openVoiceDialog(name = '') {
    document.getElementById('voices-add-form').reset();
    const nameInput = document.getElementById('voices-name-input');
    nameInput.value = '';
    populateVoiceUserSelect(name);
    const selectedExisting = Boolean(name && findExistingVoiceUser(name));
    if (name && !selectedExisting) {
        nameInput.value = name;
        setVoiceUserMode('new');
    } else {
        setVoiceUserMode(selectedExisting || detectedVoiceUsers().length ? 'existing' : 'new');
    }
    setVoiceSource('record');
    clearVoiceRecording();
    document.getElementById('voices-dialog').classList.remove('hidden');
    setTimeout(() => {
        const focusId = voiceUserMode === 'existing' ? 'voices-source-select' : 'voices-name-input';
        document.getElementById(focusId)?.focus();
    }, 50);
}

function closeVoiceDialog() {
    document.getElementById('voices-dialog').classList.add('hidden');
    stopVoiceRecording(true);
    stopVoiceStream();
}

function readFileAsDataUrl(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
    });
}

function setVoiceSource(source) {
    voiceSource = source;
    const fileLabel = document.getElementById('voices-file-label');
    const fileInput = document.getElementById('voices-file-input');
    const recorder = document.getElementById('voices-recorder');
    if (source === 'file') {
        fileLabel.classList.remove('hidden');
        fileInput.required = true;
        recorder.classList.add('hidden');
        stopVoiceRecording(true);
        stopVoiceStream();
    } else {
        fileLabel.classList.add('hidden');
        fileInput.required = false;
        recorder.classList.remove('hidden');
    }
}

function stopVoiceStream() {
    if (voiceStream) {
        voiceStream.getTracks().forEach((track) => track.stop());
        voiceStream = null;
    }
}

function clearVoiceRecording() {
    voiceRecordedChunks = [];
    voiceRecordingDataUrl = null;
    if (voiceRecordingBlobUrl) URL.revokeObjectURL(voiceRecordingBlobUrl);
    voiceRecordingBlobUrl = null;
    const preview = document.getElementById('voices-recording-preview');
    preview.src = '';
    preview.classList.add('hidden');
    document.getElementById('voices-record-clear').disabled = true;
    document.getElementById('voices-recording-state').textContent = 'Record 5-10 seconds of one speaker.';
}

function mergeFloat32Chunks(chunks) {
    const length = chunks.reduce((total, chunk) => total + chunk.length, 0);
    const merged = new Float32Array(length);
    let offset = 0;
    chunks.forEach((chunk) => {
        merged.set(chunk, offset);
        offset += chunk.length;
    });
    return merged;
}

function downsampleBuffer(buffer, inputRate, outputRate) {
    if (outputRate === inputRate) return buffer;
    const ratio = inputRate / outputRate;
    const newLength = Math.round(buffer.length / ratio);
    const result = new Float32Array(newLength);
    let offsetResult = 0;
    let offsetBuffer = 0;
    while (offsetResult < result.length) {
        const nextOffsetBuffer = Math.round((offsetResult + 1) * ratio);
        let accum = 0;
        let count = 0;
        for (let i = offsetBuffer; i < nextOffsetBuffer && i < buffer.length; i += 1) {
            accum += buffer[i];
            count += 1;
        }
        result[offsetResult] = count ? accum / count : 0;
        offsetResult += 1;
        offsetBuffer = nextOffsetBuffer;
    }
    return result;
}

function encodeWav(samples, sampleRate) {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const writeString = (offset, string) => {
        for (let i = 0; i < string.length; i += 1) view.setUint8(offset + i, string.charCodeAt(i));
    };
    writeString(0, 'RIFF');
    view.setUint32(4, 36 + samples.length * 2, true);
    writeString(8, 'WAVE');
    writeString(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeString(36, 'data');
    view.setUint32(40, samples.length * 2, true);
    let offset = 44;
    for (let i = 0; i < samples.length; i += 1, offset += 2) {
        const sample = Math.max(-1, Math.min(1, samples[i]));
        view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7FFF, true);
    }
    return new Blob([view], {type: 'audio/wav'});
}

function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(blob);
    });
}


async function beginVoiceRecording() {
    if (voicesBusy) return;
    clearVoiceRecording();
    if (!navigator.mediaDevices?.getUserMedia) {
        window.alert('Microphone access is not supported in this browser.');
        return;
    }
    try {
        voiceStream = await navigator.mediaDevices.getUserMedia({audio: true});
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        voiceAudioContext = new AudioContextClass();
        voiceRecordingSampleRate = voiceAudioContext.sampleRate;
        voiceSourceNode = voiceAudioContext.createMediaStreamSource(voiceStream);
        voiceProcessorNode = voiceAudioContext.createScriptProcessor(4096, 1, 1);
        voiceProcessorNode.onaudioprocess = (event) => {
            const input = event.inputBuffer.getChannelData(0);
            voiceRecordedChunks.push(new Float32Array(input));
        };
        voiceSourceNode.connect(voiceProcessorNode);
        voiceProcessorNode.connect(voiceAudioContext.destination);
        voiceRecordingStartedAt = Date.now();
        document.getElementById('voices-record-start').disabled = true;
        document.getElementById('voices-record-stop').disabled = false;
        document.getElementById('voices-record-clear').disabled = true;
        voiceRecordingTimer = window.setInterval(() => {
            const elapsed = ((Date.now() - voiceRecordingStartedAt) / 1000).toFixed(1);
            document.getElementById('voices-recording-state').textContent = `Recording... ${elapsed}s`;
        }, 200);
    } catch (error) {
        window.alert('Microphone access failed. Check browser permissions.');
        stopVoiceStream();
    }
}

async function stopVoiceRecording(discard = false) {
    if (voiceRecordingTimer) {
        window.clearInterval(voiceRecordingTimer);
        voiceRecordingTimer = null;
    }
    if (voiceProcessorNode) {
        voiceProcessorNode.disconnect();
        voiceProcessorNode.onaudioprocess = null;
        voiceProcessorNode = null;
    }
    if (voiceSourceNode) {
        voiceSourceNode.disconnect();
        voiceSourceNode = null;
    }
    if (voiceAudioContext) {
        await voiceAudioContext.close().catch(() => {});
        voiceAudioContext = null;
    }
    stopVoiceStream();
    document.getElementById('voices-record-start').disabled = false;
    document.getElementById('voices-record-stop').disabled = true;
    if (discard) return;
    if (!voiceRecordedChunks.length) {
        document.getElementById('voices-recording-state').textContent = 'No audio was captured.';
        return;
    }
    const merged = mergeFloat32Chunks(voiceRecordedChunks);
    const downsampled = downsampleBuffer(merged, voiceRecordingSampleRate, 16000);
    const blob = encodeWav(downsampled, 16000);
    voiceRecordingDataUrl = await blobToDataUrl(blob);
    if (voiceRecordingBlobUrl) URL.revokeObjectURL(voiceRecordingBlobUrl);
    voiceRecordingBlobUrl = URL.createObjectURL(blob);
    const preview = document.getElementById('voices-recording-preview');
    preview.src = voiceRecordingBlobUrl;
    preview.classList.remove('hidden');
    document.getElementById('voices-record-clear').disabled = false;
    document.getElementById('voices-recording-state').textContent = `Captured ${(downsampled.length / 16000).toFixed(1)}s WAV at 16 kHz.`;
}

async function submitVoice(event) {
    event.preventDefault();
    if (voicesBusy) return;
    const name = currentVoiceUserName();
    if (!name) {
        window.alert(voiceUserMode === 'existing' ? 'Choose a speaker.' : 'Enter a new speaker name.');
        return;
    }
    const existing = findExistingVoiceUser(name);
    if (voiceUserMode === 'new' && existing) {
        const message = `"${existing.name}" already exists. Add this sample to the existing user instead?`;
        if (!window.confirm(message)) return;
    } else if (existing && !existing.in_db) {
        const message = `"${existing.name}" already has sample files but is not in the DB yet. Add another sample?`;
        if (!window.confirm(message)) return;
    }
    voicesBusy = true;
    voicesSetStatus(true, 'Saving sample...');
    try {
        let audioData = null;
        let filename = null;
        if (voiceSource === 'record') {
            if (!voiceRecordingDataUrl) {
                window.alert('Record audio before submitting.');
                voicesBusy = false;
                voicesSetStatus(true, 'Online');
                return;
            }
            audioData = voiceRecordingDataUrl;
            filename = 'recording.wav';
        } else {
            const file = document.getElementById('voices-file-input').files[0];
            if (!file) {
                voicesBusy = false;
                voicesSetStatus(true, 'Online');
                return;
            }
            audioData = await readFileAsDataUrl(file);
            filename = file.name;
        }
        renderVoices(await voicesApi('/api/voices', {
            method: 'POST',
            body: JSON.stringify({name, filename, audio_data: audioData}),
        }));
        closeVoiceDialog();
        voicesSetStatus(true, 'Sample saved');
    } catch (error) {
        voicesSetStatus(false, 'Error');
        window.alert(error.message);
    } finally {
        voicesBusy = false;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('voices-refresh-btn').addEventListener('click', loadVoices);
    document.getElementById('voices-regenerate-btn').addEventListener('click', regenerateVoices);
    document.getElementById('voices-remove-all-btn').addEventListener('click', removeAllVoiceFiles);
    document.getElementById('voices-add-btn').addEventListener('click', () => openVoiceDialog());
    document.getElementById('voices-dialog-close').addEventListener('click', closeVoiceDialog);
    document.getElementById('voices-dialog-cancel').addEventListener('click', closeVoiceDialog);
    document.getElementById('voices-add-form').addEventListener('submit', submitVoice);
    document.getElementById('voices-source-select').addEventListener('change', (event) => setVoiceSource(event.target.value));
    document.getElementById('voices-user-select').addEventListener('change', (event) => {
        const mode = event.target.value === NEW_VOICE_USER_VALUE ? 'new' : 'existing';
        setVoiceUserMode(mode);
        if (mode === 'new') window.setTimeout(() => document.getElementById('voices-name-input')?.focus(), 0);
    });
    document.getElementById('voices-record-start').addEventListener('click', beginVoiceRecording);
    document.getElementById('voices-record-stop').addEventListener('click', () => stopVoiceRecording(false));
    document.getElementById('voices-record-clear').addEventListener('click', clearVoiceRecording);
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeVoiceDialog();
    });
    setVoiceSource('record');
    setVoiceUserMode('new');
    setVoiceFileAccept(voiceSupportedExtensions);
    voicesReinitIcons();
    loadVoices();
});

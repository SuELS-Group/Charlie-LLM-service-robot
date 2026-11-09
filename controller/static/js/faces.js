let facesBusy = false;
let faceSource = 'file';
let faceStream = null;
let faceCaptureData = null;
let facesCache = null;
let faceNotesDirty = false;

function faceIcon(name, cls = '') {
    return `<i data-lucide="${name}" class="${cls}"></i>`;
}

function facesReinitIcons() {
    if (window.lucide) lucide.createIcons();
}

function facesEscape(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function facesSafeFileStem(value) {
    const stem = String(value ?? '')
        .trim()
        .replace(/[^A-Za-z0-9_.-]+/g, '-')
        .replace(/^[.-]+|[.-]+$/g, '');
    return (stem || 'face').toLowerCase();
}

function findExistingPerson(name) {
    if (!facesCache?.people) return null;
    const target = facesSafeFileStem(name);
    return facesCache.people.find((person) => facesSafeFileStem(person.name) === target) || null;
}

function facesSetStatus(online, text) {
    const status = document.getElementById('faces-status');
    if (!status) return;
    status.querySelector('.server-dot').className = `server-dot ${online ? 'online' : 'offline'}`;
    status.querySelector('.server-status-text').textContent = text;
}

async function facesApi(endpoint, options = {}) {
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

function renderFacesMeta(data) {
    document.getElementById('faces-db-path').textContent = data.image_dir || '';
    document.getElementById('faces-subtitle').textContent =
        `${data.image_count} source image${data.image_count === 1 ? '' : 's'} in ${data.image_dir}. DB currently has ${data.db_count} face${data.db_count === 1 ? '' : 's'}.`;
}

function renderFaces(data) {
    facesCache = data;
    renderFacesMeta(data);
    const grid = document.getElementById('faces-grid');
    if (!data.people.length) {
        grid.innerHTML = `
            <div class="faces-empty-state">
                ${faceIcon('image-off')}
                <p>No source images found.</p>
            </div>`;
        facesReinitIcons();
        return;
    }
    grid.innerHTML = data.people.map((person) => `
        <article class="person-card">
            <div class="person-card-header">
                <div>
                    <strong class="person-name">${facesEscape(person.name)}</strong>
                    <span class="person-meta">${facesEscape(person.images.length)} image${person.images.length === 1 ? '' : 's'}</span>
                </div>
            </div>
            <div class="person-image-grid">
                ${person.images.map((image) => `
                    <div class="source-image">
                        <img alt="${facesEscape(person.name)}" src="${facesEscape(image.url)}">
                        <button class="btn btn-danger btn-sm image-remove-btn" data-filename="${facesEscape(image.filename)}" title="Remove image">
                            ${faceIcon('trash-2')}
                        </button>
                    </div>
                `).join('')}
            </div>
            <div class="person-notes">
                <label>
                    <span>Notes</span>
                    <textarea class="person-notes-input" data-name="${facesEscape(person.name)}" placeholder="Add notes...">${facesEscape(person.notes || '')}</textarea>
                </label>
                <div class="person-notes-actions">
                    <button class="btn btn-ghost btn-sm notes-save-btn" data-name="${facesEscape(person.name)}">Save Notes</button>
                </div>
            </div>
        </article>
    `).join('');
    grid.querySelectorAll('.image-remove-btn').forEach((button) => {
        button.addEventListener('click', () => removeImage(button.dataset.filename));
    });
    grid.querySelectorAll('.notes-save-btn').forEach((button) => {
        button.addEventListener('click', () => {
            const name = button.dataset.name;
            const input = grid.querySelector(`.person-notes-input[data-name="${name}"]`);
            if (!input) return;
            saveNotes(name, input.value.trim());
        });
    });
    facesReinitIcons();
}

async function loadFaces() {
    try {
        facesSetStatus(true, 'Loading...');
        renderFaces(await facesApi('/api/faces'));
        facesSetStatus(true, 'Online');
    } catch (error) {
        facesSetStatus(false, 'Error');
        document.getElementById('faces-grid').innerHTML = `
            <div class="faces-empty-state">
                ${faceIcon('circle-alert')}
                <p>${facesEscape(error.message)}</p>
            </div>`;
        facesReinitIcons();
    }
}

async function removeImage(filename) {
    if (facesBusy) return;
    if (!window.confirm(`Remove image "${filename}"?`)) return;
    facesBusy = true;
    try {
        renderFaces(await facesApi(`/api/faces/images/${encodeURIComponent(filename)}`, {method: 'DELETE'}));
    } catch (error) {
        window.alert(error.message);
    } finally {
        facesBusy = false;
    }
}

async function removeAllImages() {
    if (facesBusy) return;
    if (!facesCache?.image_count) {
        window.alert('No images to remove.');
        return;
    }
    if (!window.confirm('Remove all source images? This cannot be undone.')) return;
    facesBusy = true;
    facesSetStatus(true, 'Removing images...');
    try {
        renderFaces(await facesApi('/api/faces/images', {method: 'DELETE'}));
        facesSetStatus(true, 'Online');
    } catch (error) {
        facesSetStatus(false, 'Error');
        window.alert(error.message);
    } finally {
        facesBusy = false;
    }
}

async function saveNotes(name, notes) {
    if (facesBusy) return;
    facesBusy = true;
    facesSetStatus(true, 'Saving notes...');
    try {
        renderFaces(await facesApi('/api/faces/notes', {
            method: 'POST',
            body: JSON.stringify({name, notes}),
        }));
        facesSetStatus(true, 'Online');
    } catch (error) {
        facesSetStatus(false, 'Error');
        window.alert(error.message);
    } finally {
        facesBusy = false;
    }
}

async function regenerateFaces() {
    if (facesBusy) return;
    facesBusy = true;
    facesSetStatus(true, 'Regenerating...');
    try {
        const data = await facesApi('/api/faces/regenerate', {method: 'POST'});
        renderFaces(data);
        facesSetStatus(true, data.failed?.length ? `${data.loaded} loaded, ${data.failed.length} failed` : 'Online');
    } catch (error) {
        facesSetStatus(false, 'Error');
        window.alert(error.message);
    } finally {
        facesBusy = false;
    }
}

function openFaceDialog(name = '') {
    document.getElementById('faces-add-form').reset();
    document.getElementById('faces-name-input').value = name;
    const notesInput = document.getElementById('faces-notes-input');
    const existing = name ? findExistingPerson(name) : null;
    notesInput.value = existing?.notes || '';
    faceNotesDirty = false;
    document.getElementById('faces-source-select').value = 'file';
    setFaceSource('file');
    document.getElementById('faces-dialog').classList.remove('hidden');
    setTimeout(() => document.getElementById(name ? 'faces-file-input' : 'faces-name-input').focus(), 50);
}

function closeFaceDialog() {
    document.getElementById('faces-dialog').classList.add('hidden');
    stopFaceStream();
    clearCameraCapture();
}

function readFileAsDataUrl(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
    });
}

function setCameraHint(text) {
    const hint = document.getElementById('faces-camera-hint');
    if (hint) hint.textContent = text || '';
}

function stopFaceStream() {
    if (faceStream) {
        faceStream.getTracks().forEach((track) => track.stop());
        faceStream = null;
    }
    const video = document.getElementById('faces-camera-video');
    if (video) video.srcObject = null;
}

function clearCameraCapture() {
    faceCaptureData = null;
    const snapshot = document.getElementById('faces-camera-snapshot');
    const image = document.getElementById('faces-camera-image');
    if (image) image.src = '';
    if (snapshot) snapshot.classList.add('hidden');
}

async function listCameras() {
    if (!navigator.mediaDevices?.enumerateDevices) return [];
    const devices = await navigator.mediaDevices.enumerateDevices();
    return devices.filter((device) => device.kind === 'videoinput');
}

function updateCameraSelect(devices) {
    const select = document.getElementById('faces-camera-select');
    select.innerHTML = '';
    if (!devices.length) {
        select.disabled = true;
        setCameraHint('No camera detected.');
        return;
    }
    select.disabled = false;
    devices.forEach((device, index) => {
        const option = document.createElement('option');
        option.value = device.deviceId;
        option.textContent = device.label || `Camera ${index + 1}`;
        select.appendChild(option);
    });
    setCameraHint('');
}

async function startCamera(deviceId) {
    if (!navigator.mediaDevices?.getUserMedia) {
        setCameraHint('Camera access not supported in this browser.');
        return;
    }
    stopFaceStream();
    try {
        const constraints = {
            video: deviceId ? {deviceId: {exact: deviceId}} : true,
            audio: false,
        };
        faceStream = await navigator.mediaDevices.getUserMedia(constraints);
        const video = document.getElementById('faces-camera-video');
        video.srcObject = faceStream;
        await video.play();
        setCameraHint('');
    } catch (error) {
        setCameraHint('Camera access failed. Check permissions.');
    }
}

async function initCamera() {
    clearCameraCapture();
    const devices = await listCameras();
    updateCameraSelect(devices);
    if (!devices.length) return;
    const select = document.getElementById('faces-camera-select');
    if (!select.value && devices[0]) select.value = devices[0].deviceId;
    await startCamera(select.value || undefined);
    const refreshed = await listCameras();
    updateCameraSelect(refreshed);
}

function setFaceSource(source) {
    faceSource = source;
    const fileLabel = document.getElementById('faces-file-label');
    const fileInput = document.getElementById('faces-file-input');
    const cameraSection = document.getElementById('faces-camera');
    if (source === 'camera') {
        fileLabel.classList.add('hidden');
        fileInput.required = false;
        cameraSection.classList.remove('hidden');
        initCamera().catch(() => setCameraHint('Camera access failed.'));
    } else {
        cameraSection.classList.add('hidden');
        fileLabel.classList.remove('hidden');
        fileInput.required = true;
        stopFaceStream();
        clearCameraCapture();
    }
}

function captureFromCamera() {
    const video = document.getElementById('faces-camera-video');
    if (!video || !video.videoWidth) {
        window.alert('Camera is not ready yet.');
        return;
    }
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const ctx = canvas.getContext('2d');
    if (!ctx) {
        window.alert('Unable to capture from camera.');
        return;
    }
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    faceCaptureData = canvas.toDataURL('image/jpeg', 0.92);
    const snapshot = document.getElementById('faces-camera-snapshot');
    const image = document.getElementById('faces-camera-image');
    image.src = faceCaptureData;
    snapshot.classList.remove('hidden');
}

async function submitFace(event) {
    event.preventDefault();
    if (facesBusy) return;
    const name = document.getElementById('faces-name-input').value.trim();
    if (!name) return;
    const existing = findExistingPerson(name);
    if (existing) {
        const count = existing.images?.length ?? 0;
        const label = existing.name || name;
        const message = `"${label}" already has ${count} image${count === 1 ? '' : 's'}. Adding will replace existing images. Continue?`;
        if (!window.confirm(message)) return;
    }
    if (faceSource === 'camera' && !faceCaptureData) {
        window.alert('Capture a photo before submitting.');
        return;
    }
    if (faceSource !== 'camera' && !document.getElementById('faces-file-input').files[0]) return;
    facesBusy = true;
    facesSetStatus(true, 'Adding image...');
    try {
        const notesValue = document.getElementById('faces-notes-input').value.trim();
        let imageData = null;
        let filename = null;
        if (faceSource === 'camera') {
            imageData = faceCaptureData;
            filename = 'camera.jpg';
        } else {
            const file = document.getElementById('faces-file-input').files[0];
            if (!file) return;
            imageData = await readFileAsDataUrl(file);
            filename = file.name;
        }
        let data = await facesApi('/api/faces', {
            method: 'POST',
            body: JSON.stringify({name, filename, image_data: imageData}),
        });
        if (faceNotesDirty) {
            data = await facesApi('/api/faces/notes', {
                method: 'POST',
                body: JSON.stringify({name, notes: notesValue}),
            });
        }
        renderFaces(data);
        closeFaceDialog();
        facesSetStatus(true, 'Online');
    } catch (error) {
        facesSetStatus(false, 'Error');
        window.alert(error.message);
    } finally {
        facesBusy = false;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('faces-refresh-btn').addEventListener('click', loadFaces);
    document.getElementById('faces-regenerate-btn').addEventListener('click', regenerateFaces);
    document.getElementById('faces-remove-all-btn').addEventListener('click', removeAllImages);
    document.getElementById('faces-add-btn').addEventListener('click', () => openFaceDialog());
    document.getElementById('faces-dialog-close').addEventListener('click', closeFaceDialog);
    document.getElementById('faces-dialog-cancel').addEventListener('click', closeFaceDialog);
    document.getElementById('faces-add-form').addEventListener('submit', submitFace);
    document.getElementById('faces-notes-input').addEventListener('input', () => {
        faceNotesDirty = true;
    });
    document.getElementById('faces-name-input').addEventListener('blur', (event) => {
        if (faceNotesDirty) return;
        const existing = findExistingPerson(event.target.value.trim());
        if (!existing) return;
        const notesInput = document.getElementById('faces-notes-input');
        notesInput.value = existing.notes || '';
    });
    document.getElementById('faces-source-select').addEventListener('change', (event) => {
        setFaceSource(event.target.value);
    });
    document.getElementById('faces-camera-select').addEventListener('change', (event) => {
        startCamera(event.target.value);
    });
    document.getElementById('faces-camera-capture').addEventListener('click', captureFromCamera);
    document.getElementById('faces-camera-clear').addEventListener('click', clearCameraCapture);
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeFaceDialog();
    });
    setFaceSource('file');
    facesReinitIcons();
    loadFaces();
});

let knowledgeBusy = false;
let knowledgeCache = null;
let knowledgeEditing = null;

function knowledgeIcon(name, cls = '') {
    return `<i data-lucide="${name}" class="${cls}"></i>`;
}

function knowledgeReinitIcons() {
    if (window.lucide) lucide.createIcons();
}

function knowledgeEscape(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function knowledgeSetStatus(online, text) {
    const status = document.getElementById('knowledge-status');
    if (!status) return;
    status.querySelector('.server-dot').className = `server-dot ${online ? 'online' : 'offline'}`;
    status.querySelector('.server-status-text').textContent = text;
}

function knowledgeSetBusy(busy, buttonId = null) {
    knowledgeBusy = busy;
    const ids = ['knowledge-reindex-btn', 'knowledge-add-btn', 'knowledge-refresh-btn'];
    if (buttonId) ids.push(buttonId);
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = busy;
    });
}

async function knowledgeApi(endpoint, options = {}) {
    const response = await fetch(endpoint, {
        ...options,
        headers: {'Content-Type': 'application/json', ...options.headers},
    });
    if (!response.ok) {
        const err = await response.json().catch(() => ({detail: response.statusText}));
        throw new Error(err.detail || response.statusText);
    }
    return response.json();
}

function knowledgeRenderList(docs) {
    const list = document.getElementById('knowledge-list');
    if (!docs || docs.length === 0) {
        list.innerHTML = `
            <div class="faces-empty-state">
                ${knowledgeIcon('file-x')}
                <p>No documents yet. Click <strong>Add Doc</strong> to get started.</p>
            </div>`;
        knowledgeReinitIcons();
        return;
    }
    list.innerHTML = docs.map(doc => `
        <div class="knowledge-doc-card" data-filename="${knowledgeEscape(doc.filename)}">
            <div class="knowledge-doc-header">
                <div class="knowledge-doc-title">
                    ${knowledgeIcon('file-text', 'knowledge-doc-icon')}
                    <span class="knowledge-doc-name">${knowledgeEscape(doc.filename)}</span>
                    <span class="knowledge-doc-meta">${doc.word_count} words</span>
                </div>
                <div class="knowledge-doc-actions">
                    <button class="btn btn-ghost btn-sm" data-action="edit" title="Edit">Edit</button>
                    <button class="btn btn-danger btn-sm" data-action="delete" title="Delete">
                        ${knowledgeIcon('trash-2')}
                    </button>
                </div>
            </div>
            <div class="knowledge-doc-preview">${knowledgeEscape(doc.error || doc.preview)}${doc.word_count > 50 ? '…' : ''}</div>
        </div>
    `).join('');
    knowledgeReinitIcons();
}

async function knowledgeLoad() {
    knowledgeSetBusy(true);
    try {
        const data = await knowledgeApi('/api/knowledge/docs');
        knowledgeCache = data;
        await knowledgeLoadStatus();
        document.getElementById('knowledge-docs-path').textContent = data.docs_dir || '';
        document.getElementById('knowledge-subtitle').textContent =
            data.count === 0 ? 'No documents yet' : `${data.count} document${data.count !== 1 ? 's' : ''} in knowledge directory`;
        knowledgeRenderList(data.docs);
    } catch (e) {
        knowledgeSetStatus(false, 'Error');
        document.getElementById('knowledge-subtitle').textContent = `Error: ${e.message}`;
    } finally {
        knowledgeSetBusy(false);
    }
}

async function knowledgeLoadStatus() {
    const status = await knowledgeApi('/api/knowledge/status');
    const job = status.job || {};
    const current = status.ready && !status.pending_changes && status.compatible;
    const label = job.state === 'running' ? 'Updating search…' :
        job.state === 'failed' ? 'Update failed' : current ? 'Search ready' : 'Update pending';
    knowledgeSetStatus(current && job.state !== 'failed', label);
    let details = `${status.chunk_count || 0} searchable chunks. `;
    if (status.last_success) details += `Last indexed: ${new Date(status.last_success).toLocaleString()}. `;
    if (status.pending_changes && status.ready) details += 'Search still uses the previous saved version. ';
    if (!status.ready) details += 'Documents are not searchable yet. ';
    if (job.error) details += `${job.error}. Use Reindex to retry.`;
    document.getElementById('knowledge-index-status').textContent = details;
    document.getElementById('knowledge-reindex-btn').disabled = knowledgeBusy || job.state === 'running';
}

async function knowledgePollStatus() {
    try { await knowledgeLoadStatus(); }
    catch (e) { knowledgeSetStatus(false, 'Status unavailable'); }
    setTimeout(knowledgePollStatus, 2000);
}

async function knowledgeReindex() {
    try {
        await knowledgeApi('/api/knowledge/reindex', {method: 'POST'});
        await knowledgeLoadStatus();
    } catch (e) { alert(`Error: ${e.message}`); }
}

async function knowledgeEdit(filename) {
    try {
        const doc = await knowledgeApi(`/api/knowledge/docs/${encodeURIComponent(filename)}/content`);
        knowledgeOpenDialog();
        knowledgeEditing = filename;
        document.getElementById('knowledge-dialog-title').textContent = 'Edit Document';
        document.getElementById('knowledge-name-input').value = filename;
        document.getElementById('knowledge-name-input').disabled = true;
        document.getElementById('knowledge-source-select').disabled = true;
        document.getElementById('knowledge-content-input').value = doc.content;
    } catch (e) { alert(`Error: ${e.message}`); }
}

function knowledgeCloseReindexDialog() {
    document.getElementById('knowledge-reindex-dialog').classList.add('hidden');
}

function knowledgeOpenDialog() {
    knowledgeEditing = null;
    document.getElementById('knowledge-dialog-title').textContent = 'Add Document';
    document.getElementById('knowledge-name-input').disabled = false;
    document.getElementById('knowledge-source-select').disabled = false;
    document.getElementById('knowledge-file-input').value = '';
    document.getElementById('knowledge-name-input').value = '';
    document.getElementById('knowledge-content-input').value = '';
    document.getElementById('knowledge-source-select').value = 'text';
    knowledgeSourceChanged();
    document.getElementById('knowledge-dialog').classList.remove('hidden');
    setTimeout(() => document.getElementById('knowledge-name-input').focus(), 50);
}

function knowledgeCloseDialog() {
    document.getElementById('knowledge-dialog').classList.add('hidden');
}

function knowledgeSourceChanged() {
    const source = document.getElementById('knowledge-source-select').value;
    const isFile = source === 'file';
    document.getElementById('knowledge-name-label').classList.toggle('hidden', isFile);
    document.getElementById('knowledge-content-label').classList.toggle('hidden', isFile);
    document.getElementById('knowledge-file-label').classList.toggle('hidden', !isFile);
}

async function knowledgeSubmit(event) {
    event.preventDefault();
    if (knowledgeBusy) return;
    const source = document.getElementById('knowledge-source-select').value;
    knowledgeSetBusy(true);
    try {
        if (source === 'file') {
            const fileInput = document.getElementById('knowledge-file-input');
            const file = fileInput.files[0];
            if (!file) throw new Error('No file selected');
            const content = await file.text();
            await knowledgeApi('/api/knowledge/docs', {
                method: 'POST',
                body: JSON.stringify({filename: file.name, content}),
            });
        } else {
            const name = document.getElementById('knowledge-name-input').value.trim();
            const content = document.getElementById('knowledge-content-input').value;
            if (!name) throw new Error('Filename is required');
            await knowledgeApi(knowledgeEditing ? `/api/knowledge/docs/${encodeURIComponent(knowledgeEditing)}` : '/api/knowledge/docs', {
                method: knowledgeEditing ? 'PUT' : 'POST',
                body: JSON.stringify({filename: name, content}),
            });
        }
        knowledgeCloseDialog();
        await knowledgeLoad();
    } catch (e) {
        alert(`Error: ${e.message}`);
    } finally {
        knowledgeSetBusy(false);
    }
}

async function knowledgeDelete(filename) {
    if (!confirm(`Delete "${filename}"?`)) return;
    if (knowledgeBusy) return;
    knowledgeSetBusy(true);
    try {
        await knowledgeApi(`/api/knowledge/docs/${encodeURIComponent(filename)}`, {method: 'DELETE'});
        await knowledgeLoad();
    } catch (e) {
        alert(`Error: ${e.message}`);
    } finally {
        knowledgeSetBusy(false);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    knowledgeLoad();
    knowledgePollStatus();
    document.getElementById('knowledge-list').addEventListener('click', event => {
        const button = event.target.closest('button[data-action]');
        if (!button || knowledgeBusy) return;
        const filename = button.closest('[data-filename]').dataset.filename;
        if (button.dataset.action === 'delete') knowledgeDelete(filename);
        else knowledgeEdit(filename);
    });
    knowledgeReinitIcons();
    document.getElementById('knowledge-dialog').addEventListener('click', e => {
        if (e.target === document.getElementById('knowledge-dialog')) knowledgeCloseDialog();
    });
    document.getElementById('knowledge-reindex-dialog').addEventListener('click', e => {
        if (e.target === document.getElementById('knowledge-reindex-dialog')) knowledgeCloseReindexDialog();
    });
});


let memoryOffset = 0;
let memoryRecords = [];
let memoryEditing = null;
let memoryBusy = false;

async function memoryLoad() {
    try {
        const data = await knowledgeApi(`/api/knowledge/memories?offset=${memoryOffset}&limit=20`);
        if (memoryOffset && !data.memories.length) {
            memoryOffset = Math.max(0, memoryOffset - 20);
            return memoryLoad();
        }
        memoryRecords = data.memories;
        document.getElementById('memory-count').textContent = `${data.count} saved facts`;
        document.getElementById('memory-list').innerHTML = data.memories.map(record => `
            <div class="knowledge-doc-card" data-memory-id="${knowledgeEscape(record.id)}">
                <div class="knowledge-doc-preview">${knowledgeEscape(record.fact)}</div>
                <small>Saved: ${knowledgeEscape(record.stored_at || 'unknown')}${record.modified_at ? ` · Edited: ${knowledgeEscape(record.modified_at)}` : ''}</small>
                <div class="knowledge-doc-actions">
                    <button class="btn btn-ghost btn-sm" data-memory-action="edit">Edit</button>
                    <button class="btn btn-danger btn-sm" data-memory-action="delete">Delete</button>
                </div>
            </div>`).join('') || '<p>No saved facts yet.</p>';
        document.getElementById('memory-previous').disabled = memoryOffset === 0;
        document.getElementById('memory-next').disabled = memoryOffset + 20 >= data.count;
    } catch (e) {
        document.getElementById('memory-count').textContent = `Error: ${e.message}`;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    memoryLoad();
    document.getElementById('memory-refresh').addEventListener('click', memoryLoad);
    document.getElementById('memory-previous').addEventListener('click', () => { memoryOffset = Math.max(0, memoryOffset - 20); memoryLoad(); });
    document.getElementById('memory-next').addEventListener('click', () => { memoryOffset += 20; memoryLoad(); });
    document.getElementById('memory-cancel').addEventListener('click', () => document.getElementById('memory-dialog').classList.add('hidden'));
    document.getElementById('memory-list').addEventListener('click', async event => {
        const button = event.target.closest('[data-memory-action]');
        if (!button || memoryBusy) return;
        const id = button.closest('[data-memory-id]').dataset.memoryId;
        if (button.dataset.memoryAction === 'edit') {
            memoryEditing = id;
            document.getElementById('memory-fact').value = memoryRecords.find(record => record.id === id).fact;
            document.getElementById('memory-dialog').classList.remove('hidden');
            return;
        }
        if (!confirm('Delete this remembered fact?')) return;
        memoryBusy = true;
        try {
            await knowledgeApi(`/api/knowledge/memories/${encodeURIComponent(id)}`, {method: 'DELETE'});
            await memoryLoad();
        } catch (e) { alert(`Error: ${e.message}`); }
        finally { memoryBusy = false; }
    });
    document.getElementById('memory-form').addEventListener('submit', async event => {
        event.preventDefault();
        if (memoryBusy) return;
        memoryBusy = true;
        document.getElementById('memory-save').disabled = true;
        try {
            await knowledgeApi(`/api/knowledge/memories/${encodeURIComponent(memoryEditing)}`, {
                method: 'PUT', body: JSON.stringify({fact: document.getElementById('memory-fact').value}),
            });
            document.getElementById('memory-dialog').classList.add('hidden');
            await memoryLoad();
        } catch (e) { alert(`Error: ${e.message}`); }
        finally { memoryBusy = false; document.getElementById('memory-save').disabled = false; }
    });
});

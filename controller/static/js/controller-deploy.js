const fetchServicesStatus = () => apiCall('/api/services/status');
const generateServices = () => apiCall('/api/services/generate', {method: 'POST'});
const deployToMachine = (mid) => apiCall(`/api/services/deploy/${mid}`, {method: 'POST'});
const generateAndDeployAll = () => apiCall('/api/services/generate-and-deploy', {method: 'POST'});

function openDeployModal() {
    document.getElementById('deploy-modal').classList.remove('hidden');
    reinitIcons();
    renderDeployModal();
}

function closeDeployModal() {
    document.getElementById('deploy-modal').classList.add('hidden');
}

async function renderDeployModal() {
    const body = document.getElementById('deploy-modal-body');
    body.innerHTML = `<div class="loading-state">${icon('loader', 'spin')} <span>Loading...</span></div>`;
    reinitIcons();

    try {
        const status = await fetchServicesStatus();
        let html = '';

        html += `<p class="deploy-desc">Generate systemd service files from config and deploy them to machines over SSH.</p>`;

        html += '<div class="ssh-machines-list">';
        for (const [mid, m] of Object.entries(status.machines)) {
            const connCls = m.connected ? 'ssh-machine-ok' : 'ssh-machine-err';
            const connIcon = m.connected ? 'check-circle' : 'wifi-off';
            const connText = m.connected ? 'Connected' : 'Disconnected';
            const svcCount = m.generated_services.length;
            const svcText = svcCount > 0 ? `${svcCount} services` : 'None generated';

            html += `<div class="ssh-machine-row ${connCls}">
                <div class="ssh-machine-info">
                    <span class="ssh-machine-name">${m.name}</span>
                    <span class="ssh-machine-host">${m.type} · ${svcText}</span>
                </div>
                <div class="ssh-machine-status">
                    ${icon(connIcon)}
                    <span>${connText}</span>
                </div>
                <div class="deploy-machine-actions">
                    <button onclick="handleDeployMachine('${mid}')"
                            class="btn btn-ghost btn-sm" ${!m.connected || svcCount === 0 ? 'disabled' : ''}
                            title="Deploy to ${m.name}" id="deploy-btn-${mid}">
                        ${icon('upload')} Deploy
                    </button>
                    <button onclick="handleHideMachine('${mid}')"
                            class="btn btn-danger btn-sm"
                            title="Hide ${m.name}">
                        ${icon('eye-off')} Hide
                    </button>
                </div>
            </div>`;
        }
        html += '</div>';

        html += `<div class="ssh-actions">
            <button onclick="handleGenerateOnly()" class="btn btn-ghost" id="generate-only-btn">
                ${icon('file-cog')} Generate Only
            </button>
            <button onclick="handleGenerateAndDeploy()" class="btn btn-primary" id="gen-deploy-btn">
                ${icon('upload-cloud')} Generate & Deploy All
            </button>
        </div>`;

        body.innerHTML = html;
        reinitIcons();
    } catch (e) {
        body.innerHTML = `<div class="empty-state" style="color:var(--red)">Error: ${e.message}</div>`;
    }
}

async function handleHideMachine(machineId) {
    try {
        const result = await hideMachine(machineId);
        showToast(result.message || `${machineId} hidden`, 'success');
        await refreshAllStatus();
        await renderDeployModal();
    } catch (e) {
        showToast(`Hide failed: ${e.message}`, 'error');
    }
}

async function handleGenerateOnly() {
    const btn = document.getElementById('generate-only-btn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `${icon('loader', 'spin')} Generating...`;
        reinitIcons();
    }

    try {
        const result = await generateServices();
        showToast(`Generated ${result.total} service files`, 'success');
        await renderDeployModal();
    } catch (e) {
        showToast(`Generation failed: ${e.message}`, 'error');
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `${icon('file-cog')} Generate Only`;
            reinitIcons();
        }
    }
}

async function handleGenerateAndDeploy() {
    const btn = document.getElementById('gen-deploy-btn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `${icon('loader', 'spin')} Working...`;
        reinitIcons();
    }

    try {
        const result = await generateAndDeployAll();
        const entries = Object.entries(result.results);
        const ok = entries.filter(([_, r]) => r.status === 'success');
        const skipped = entries.filter(([_, r]) => r.status === 'skipped');
        const failed = entries.filter(([_, r]) => r.status === 'error');

        let msg = `Deployed to ${ok.length}/${entries.length} machines`;
        if (skipped.length) msg += `, ${skipped.length} skipped (not connected)`;
        if (failed.length) msg += `, ${failed.length} failed`;

        showToast(msg, failed.length ? 'warning' : 'success');
        await renderDeployModal();
    } catch (e) {
        showToast(`Deploy failed: ${e.message}`, 'error');
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `${icon('upload-cloud')} Generate & Deploy All`;
            reinitIcons();
        }
    }
}

async function handleDeployMachine(machineId) {
    const btn = document.getElementById(`deploy-btn-${machineId}`);
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `${icon('loader', 'spin')}`;
        reinitIcons();
    }

    try {
        const result = await deployToMachine(machineId);
        if (result.status === 'success') {
            showToast(`${result.message} to ${machineId}`, 'success');
        } else {
            showToast(`Deploy to ${machineId}: ${result.message}`, 'error');
        }
        await renderDeployModal();
    } catch (e) {
        showToast(`Deploy to ${machineId} failed: ${e.message}`, 'error');
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `${icon('upload')} Deploy`;
            reinitIcons();
        }
    }
}

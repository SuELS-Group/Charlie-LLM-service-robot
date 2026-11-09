const fetchSSHStatus = () => apiCall('/api/ssh/status');
const submitSSHSetup = (credentials, users) => apiCall('/api/ssh/setup', {
    method: 'POST',
    body: JSON.stringify({credentials, users}),
});
const hideMachine = (mid) => apiCall(`/api/machines/${mid}/hide`, {method: 'POST'});

async function openSSHSetup() {
    document.getElementById('ssh-modal').classList.remove('hidden');
    reinitIcons();
    await renderSSHModal();
}

function closeSSHSetup() {
    document.getElementById('ssh-modal').classList.add('hidden');
}

async function renderSSHModal() {
    const body = document.getElementById('ssh-modal-body');
    body.innerHTML = `<div class="loading-state">${icon('loader', 'spin')} <span>Loading...</span></div>`;
    reinitIcons();

    try {
        const status = await fetchSSHStatus();

        let html = '';

        if (status.keys_exist) {
            html += `<div class="ssh-status-banner ssh-ok">
                ${icon('check-circle')}
                <span>SSH keypair exists</span>
            </div>`;
        } else {
            html += `<div class="ssh-status-banner ssh-warn">
                ${icon('alert-triangle')}
                <span>No SSH keys found — enter credentials to generate and deploy</span>
            </div>`;
        }

        html += '<div class="ssh-machines-list">';
        for (const m of status.machines) {
            const connected = status.connectivity[m.id];
            const isLocal = m.is_local;
            const statusCls = connected ? 'ssh-machine-ok' : (isLocal ? 'ssh-machine-ok' : 'ssh-machine-err');
            const statusText = isLocal ? 'Local' : (connected ? 'Key OK' : 'Not configured');
            const statusIcon = isLocal ? 'monitor' : (connected ? 'check-circle' : 'alert-circle');
            const displayUser = m.user || 'unset';

            html += `<div class="ssh-machine-row ${statusCls}">
                <div class="ssh-machine-info">
                    <span class="ssh-machine-name">${m.name}</span>
                    <span class="ssh-machine-host">${displayUser}@${m.host}</span>
                </div>
                <div class="ssh-machine-status">
                    ${icon(statusIcon)}
                    <span>${statusText}</span>
                </div>
                ${!isLocal ? `
                    <input type="text" class="ssh-password-input" id="ssh-user-${m.id}"
                           placeholder="SSH user" value="${m.user || ''}" autocomplete="off">
                    <input type="password" class="ssh-password-input" id="ssh-pw-${m.id}"
                           placeholder="Password" autocomplete="off">
                    <div class="ssh-machine-error" id="ssh-err-${m.id}"></div>
                ` : ''}
            </div>`;
        }
        html += '</div>';

        const hasAnyRemote = status.machines.some(m => !m.is_local);

        if (!status.keys_exist || status.machines.some(m => !m.is_local && !status.connectivity[m.id])) {
            html += `<div class="ssh-actions">
                <button onclick="handleSSHSetup()" class="btn btn-primary" id="ssh-deploy-btn">
                    ${icon('key-round')} ${status.keys_exist ? 'Re-deploy Keys' : 'Generate & Deploy Keys'}
                </button>
            </div>`;
        } else if (hasAnyRemote) {
            html += `<div class="ssh-actions">
                <button onclick="handleSSHSetup()" class="btn btn-ghost" id="ssh-deploy-btn">
                    ${icon('rotate-cw')} Regenerate & Re-deploy Keys
                </button>
            </div>`;
        }

        body.innerHTML = html;
        reinitIcons();
    } catch (e) {
        body.innerHTML = `<div class="empty-state" style="color:var(--red)">Error: ${e.message}</div>`;
    }
}

async function handleSSHSetup() {
    const btn = document.getElementById('ssh-deploy-btn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `${icon('loader', 'spin')} Deploying...`;
        reinitIcons();
    }

    document.querySelectorAll('.ssh-machine-error').forEach(el => {
        el.textContent = '';
    });

    const credentials = {};
    const users = {};
    document.querySelectorAll('.ssh-password-input').forEach(input => {
        if (input.id.startsWith('ssh-pw-') && input.value) {
            const mid = input.id.replace('ssh-pw-', '');
            credentials[mid] = input.value;
        }
        if (input.id.startsWith('ssh-user-') && input.value.trim()) {
            const mid = input.id.replace('ssh-user-', '');
            users[mid] = input.value.trim();
        }
    });

    try {
        const result = await submitSSHSetup(credentials, users);
        const failed = Object.entries(result.results).filter(([_, r]) => !r.ok);
        if (failed.length === 0) {
            showToast('SSH keys deployed to all machines', 'success');
        } else {
            failed.forEach(([mid, r]) => {
                const errEl = document.getElementById(`ssh-err-${mid}`);
                if (errEl) errEl.textContent = r.error || 'Deployment failed';
            });
            const failedNames = failed.map(([mid]) => mid).join(', ');
            showToast(`Key deployment failed for: ${failedNames}`, 'warning');
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = `${icon('key-round')} Retry`;
                reinitIcons();
            }
            return;
        }
        await renderSSHModal();
        await refreshAllStatus();
    } catch (e) {
        showToast(`SSH setup failed: ${e.message}`, 'error');
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `${icon('key-round')} Retry`;
            reinitIcons();
        }
    }
}


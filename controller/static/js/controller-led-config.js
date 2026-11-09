let ledConfigMachineId = null;

function _floatToHex(v) {
    const b = Math.round(Math.max(0, Math.min(1, v)) * 255);
    return b.toString(16).padStart(2, '0');
}

function _hexToFloats(hex) {
    return {
        r: parseInt(hex.slice(1, 3), 16) / 255,
        g: parseInt(hex.slice(3, 5), 16) / 255,
        b: parseInt(hex.slice(5, 7), 16) / 255,
    };
}

async function openLedConfigModal(machineId) {
    ledConfigMachineId = machineId;
    const modal = document.getElementById('led-config-modal');
    const loading = document.getElementById('led-config-loading');
    const form = document.getElementById('led-config-form');
    const errorEl = document.getElementById('led-config-error');

    modal.classList.remove('hidden');
    loading.style.display = '';
    form.style.display = 'none';
    errorEl.style.display = 'none';
    reinitIcons();

    try {
        const resp = await fetch(`/api/machines/${encodeURIComponent(machineId)}/led/config`);
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            throw new Error(d.detail || `HTTP ${resp.status}`);
        }
        const cfg = await resp.json();

        const defaults = {
            think_brightness: 10,
            trace_brightness: 20,
            spin_brightness: 20,
            max_color_brightness: 20,
        };
        for (const [cfgKey, defVal] of Object.entries(defaults)) {
            const elemId = 'led-' + cfgKey.replace(/_/g, '-');
            const val = typeof cfg[cfgKey] === 'number' ? cfg[cfgKey] : defVal;
            document.getElementById(elemId).value = val;
            document.getElementById(elemId + '-value').textContent = val;
        }

        const idleHex = '#' + _floatToHex(cfg.idle_r ?? 0) + _floatToHex(cfg.idle_g ?? 0) + _floatToHex(cfg.idle_b ?? 1);
        document.getElementById('led-idle-color').value = idleHex;
        const idleAlpha = Math.round((cfg.idle_a ?? 0.1) * 100);
        document.getElementById('led-idle-alpha').value = idleAlpha;
        document.getElementById('led-idle-alpha-value').textContent = idleAlpha;

        const actionHex = '#' + _floatToHex(cfg.action_r ?? 1) + _floatToHex(cfg.action_g ?? 0) + _floatToHex(cfg.action_b ?? 0);
        document.getElementById('led-action-color').value = actionHex;
        const actionAlpha = Math.round((cfg.action_a ?? 1.0) * 100);
        document.getElementById('led-action-alpha').value = actionAlpha;
        document.getElementById('led-action-alpha-value').textContent = actionAlpha;

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

function closeLedConfigModal() {
    document.getElementById('led-config-modal').classList.add('hidden');
    ledConfigMachineId = null;
}

async function saveLedConfig() {
    if (!ledConfigMachineId) return;
    const errorEl = document.getElementById('led-config-error');
    errorEl.style.display = 'none';

    const think_brightness = parseInt(document.getElementById('led-think-brightness').value, 10);
    const trace_brightness = parseInt(document.getElementById('led-trace-brightness').value, 10);
    const spin_brightness = parseInt(document.getElementById('led-spin-brightness').value, 10);
    const max_color_brightness = parseInt(document.getElementById('led-max-color-brightness').value, 10);

    const idleFloats = _hexToFloats(document.getElementById('led-idle-color').value);
    const idle_a = parseInt(document.getElementById('led-idle-alpha').value, 10) / 100;
    const actionFloats = _hexToFloats(document.getElementById('led-action-color').value);
    const action_a = parseInt(document.getElementById('led-action-alpha').value, 10) / 100;

    try {
        const resp = await fetch(`/api/machines/${encodeURIComponent(ledConfigMachineId)}/led/config`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                think_brightness,
                trace_brightness,
                spin_brightness,
                max_color_brightness,
                idle_r: parseFloat(idleFloats.r.toFixed(4)),
                idle_g: parseFloat(idleFloats.g.toFixed(4)),
                idle_b: parseFloat(idleFloats.b.toFixed(4)),
                idle_a: parseFloat(idle_a.toFixed(4)),
                action_r: parseFloat(actionFloats.r.toFixed(4)),
                action_g: parseFloat(actionFloats.g.toFixed(4)),
                action_b: parseFloat(actionFloats.b.toFixed(4)),
                action_a: parseFloat(action_a.toFixed(4)),
            }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.success === false) {
            throw new Error(data.detail || data.message || `HTTP ${resp.status}`);
        }
        closeLedConfigModal();
    } catch (e) {
        errorEl.textContent = `Failed to save config: ${e.message}`;
        errorEl.style.display = '';
    }
}

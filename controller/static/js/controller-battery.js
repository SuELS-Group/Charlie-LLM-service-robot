const batteryStates = new Map();
const batteryRequests = new Set();
const batteryAnnouncementRequests = new Set();
const batteryAnnouncementVersions = new Map();
const batteryAnnouncementErrors = new Map();
let batteryRefreshTimer = null;

function renderBatteryInline(machine) {
    const id = escapeHtml(machine.id);
    return `<span class="battery-inline" id="battery-${id}" data-tone="offline">
        <span class="sysinfo-item battery-charge" title="Estimated battery charge">
            <span class="battery-gauge" aria-hidden="true"><span data-battery="fill"></span></span>
            <span class="sysinfo-label">Battery</span><span class="sysinfo-value" data-battery="percentage">—</span>
            <span class="battery-state" data-battery="state">Connecting…</span>
        </span>
        <span class="sysinfo-item"><span class="sysinfo-label" data-battery="runtime-label">Until recharge</span><span class="sysinfo-value" data-battery="runtime">—</span></span>
        <label class="sysinfo-item battery-announcements">
            <span class="sysinfo-label">Akkuansagen</span>
            <select data-battery="announcement-mode" data-machine-id="${id}" disabled
                    aria-label="Gesprochene Akkumeldungen"
                    title="Reduziert: jede Warnstufe einmal pro Akkubetrieb. Am Ladekabel bleiben Statusansagen stumm."
                    onchange="setBatteryAnnouncementMode(this)">
                <option value="reduced" selected>Reduziert</option>
                <option value="full">Alle</option>
                <option value="off">Aus</option>
            </select>
            <span data-battery="announcement-feedback" role="status" aria-live="polite"></span>
        </label>
        <button type="button" class="battery-toggle sysinfo-item" id="battery-toggle-${id}" data-machine-id="${id}"
                aria-expanded="false" aria-controls="battery-details-${id}" onclick="toggleBatteryDetails(this)">
            Measurements &amp; estimate <span class="battery-chevron" aria-hidden="true">⌄</span>
        </button>
    </span>`;
}

function renderBatteryDetails(machine) {
    return `<section class="battery-details-card" id="battery-details-${escapeHtml(machine.id)}" hidden aria-label="Battery measurements and estimate">
        <div class="battery-detail-heading"><span data-battery="caption">estimated charge</span><span data-battery="quality">Waiting for data</span></div>
        <div class="battery-detail-metrics">
            <span class="sysinfo-item"><span class="sysinfo-label">Voltage</span><span class="sysinfo-value" data-battery="voltage">—</span></span>
            <span class="sysinfo-item"><span class="sysinfo-label" data-battery="current-label">Load</span><span class="sysinfo-value" data-battery="load">—</span></span>
        </div>
        <div class="battery-detail-content">
            <div class="battery-chart-block">
                <svg class="battery-chart" viewBox="0 0 300 72" preserveAspectRatio="none" role="img" aria-label="Recent measured and filtered battery voltage" data-battery="chart"></svg>
                <span class="battery-chart-legend"><span>Measured voltage</span><span>Filtered voltage</span></span>
            </div>
            <p data-battery="explanation">Waiting for fresh battery measurements.</p>
        </div>
    </section>`;
}

function toggleBatteryDetails(button) {
    const details = document.getElementById(`battery-details-${button.dataset.machineId}`);
    if (!details) return;
    details.hidden = !details.hidden;
    button.setAttribute('aria-expanded', String(!details.hidden));
}

async function setBatteryAnnouncementMode(select) {
    const machineId = select.dataset.machineId;
    const mode = select.value;
    if (batteryAnnouncementRequests.has(machineId) || !['reduced', 'full', 'off'].includes(mode)) return;
    batteryAnnouncementRequests.add(machineId);
    batteryAnnouncementErrors.delete(machineId);
    batteryAnnouncementVersions.set(machineId, (batteryAnnouncementVersions.get(machineId) || 0) + 1);
    select.disabled = true;
    renderBatteryIndicators();
    try {
        const response = await fetch(`/api/machines/${encodeURIComponent(machineId)}/battery/announcements`, {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode}),
            signal: AbortSignal.timeout(5000),
        });
        if (!response.ok) throw new Error('Cannot save battery announcements');
        const result = await response.json();
        if (result.mode !== mode) throw new Error('Battery announcement mode was not confirmed');
        const cached = batteryStates.get(machineId);
        if (cached) cached.data.announcement_mode = result.mode;
    } catch (_) {
        batteryAnnouncementErrors.set(machineId, 'Speichern fehlgeschlagen');
    } finally {
        batteryAnnouncementVersions.set(machineId, (batteryAnnouncementVersions.get(machineId) || 0) + 1);
        batteryAnnouncementRequests.delete(machineId);
        renderBatteryIndicators();
    }
}

function batteryDurationLabel(seconds) {
    if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return 'Estimating…';
    const minutes = Math.round(seconds / 60);
    if (minutes < 5) return '< 5 min';
    const duration = minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60 ? `${minutes % 60} min` : ''}`.trim() : `${minutes} min`;
    return `≈ ${duration}`;
}

function batteryRuntimeLabel(data) {
    switch (data.runtime_status) {
        case 'connected':
            if (data.charging_state === 'full') return 'Full';
            if (data.charging_state === 'charging') {
                if (data.charge_time_status === 'current_too_low') return 'Charge rate too low';
                if (data.charge_time_status === 'current_unavailable') return 'No charge-rate data';
                return batteryDurationLabel(data.time_to_full_seconds);
            }
            return data.charging_state === 'not charging' ? 'Not charging' : 'Checking…';
        case 'learning': return 'Learning usage…';
        case 'recharge_recommended': return 'Recharge recommended';
        case 'load_unavailable': return 'Not enough load data';
        case 'estimate': {
            const seconds = data.runtime_to_recharge_seconds;
            if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return 'Unavailable';
            return batteryDurationLabel(seconds);
        }
        default: return 'Unavailable';
    }
}

function batteryChartMarkup(history) {
    const points = (Array.isArray(history) ? history : []).slice(-120).filter(point =>
        [point.sampled_at, point.voltage, point.filtered_voltage].every(value => typeof value === 'number' && Number.isFinite(value)));
    if (points.length < 2) return '';
    const values = points.flatMap(point => [point.voltage, point.filtered_voltage]);
    const low = Math.min(...values) - .05;
    const range = Math.max(.2, Math.max(...values) - low + .05);
    const start = points[0].sampled_at;
    const duration = Math.max(1, points[points.length - 1].sampled_at - start);
    const path = key => points.map((point, index) => `${index ? 'L' : 'M'}${((point.sampled_at - start) / duration * 300).toFixed(1)},${(68 - (point[key] - low) / range * 64).toFixed(1)}`).join(' ');
    return `<path class="battery-chart-raw" d="${path('voltage')}"/><path class="battery-chart-filtered" d="${path('filtered_voltage')}"/>`;
}

function updateBatteryPanel(machineId, data) {
    const panel = document.getElementById(`battery-${machineId}`);
    if (!panel) return;
    const details = document.getElementById(`battery-details-${machineId}`);
    const get = field => panel.querySelector(`[data-battery="${field}"]`) || details?.querySelector(`[data-battery="${field}"]`);
    const set = (field, value) => { const element = get(field); if (element) element.textContent = value; };
    const modeSelect = get('announcement-mode');
    const savingMode = batteryAnnouncementRequests.has(machineId);
    const hasMode = ['reduced', 'full', 'off'].includes(data?.announcement_mode);
    if (modeSelect) {
        modeSelect.disabled = savingMode || !hasMode;
        if (!savingMode) modeSelect.value = hasMode ? data.announcement_mode : 'reduced';
    }
    set('announcement-feedback', savingMode ? 'Speichert…' : batteryAnnouncementErrors.get(machineId) || (hasMode ? '' : 'Nicht erreichbar'));
    const available = data?.available === true;
    const number = value => typeof value === 'number' && Number.isFinite(value);
    const percent = available && number(data.percentage) && data.percentage >= 0 && data.percentage <= 100 ? data.percentage : null;
    const connected = available && data.charger_connected === true;
    const full = connected && data.charging_state === 'full';
    const warning = available && ['low', 'critical'].includes(data.voltage_warning) ? data.voltage_warning : null;
    panel.dataset.tone = !available ? 'offline' : warning || (connected ? 'charging' : 'normal');
    set('percentage', percent === null ? '—' : `${full ? '' : '≈ '}${Math.round(percent)}%`);
    set('caption', !available ? 'no fresh measurement' : full ? 'full charge confirmed' : data.quality === 'rough_charge_estimate' ? 'rough charging estimate' : 'estimated charge');
    set('state', !available ? 'Offline' : !connected ? 'On battery' :
        ({charging: 'Charging', full: 'Full', 'not charging': 'Connected · not charging', discharging: 'Connected · discharging'}[data.charging_state] || 'Connected · charge state unknown'));
    set('voltage', available && number(data.voltage) ? `${data.voltage.toFixed(2)} V` : '—');
    const chargeCurrent = connected && number(data.charging_current);
    set('current-label', chargeCurrent ? 'Charge current' : 'Load');
    set('load', chargeCurrent ? `${data.charging_current.toFixed(1)} A` : available && number(data.load_current) ? `${data.load_current.toFixed(1)} A` : '—');
    set('runtime-label', !available ? 'Estimate' : connected ? full ? 'Charge' : data.charging_state === 'charging' ? 'Until full' : 'Charger' : 'Until recharge');
    set('runtime', available ? batteryRuntimeLabel(data) : 'Unavailable');
    set('quality', !available ? 'No live data' : warning === 'critical' ? 'Critical voltage' : warning === 'low' ? 'Low voltage · recharge' :
        data.quality === 'settling' ? `Settling · ${number(data.warmup_remaining_seconds) ? Math.ceil(data.warmup_remaining_seconds) : '…'} s` : full ? 'Confirmed' : data.quality === 'rough_charge_estimate' ? 'Rough estimate' : 'Estimate');
    const fill = get('fill');
    if (fill) fill.style.width = `${percent ?? 0}%`;
    const chart = get('chart');
    if (chart) chart.innerHTML = available ? batteryChartMarkup(data.history) : '';
    if (!available) {
        set('explanation', 'Fresh battery measurements are unavailable. Check that the robot and Web Bridge are running.');
    } else if (connected && !full) {
        const integrated = data.method === 'charge_current_counting';
        set('explanation', integrated ? 'Charge is estimated from the separate PIMU charging-current sensor. Time until full assumes the present charging rate; the final charging phase can take longer. Battery capacity, current calibration and the initial voltage-based charge estimate are not calibrated.' :
            'Charging is confirmed separately from cable connection. Charge percentage is a rough, filtered voltage estimate while a measured net charging current is unavailable. A reliable time until full requires that current; charging voltage alone cannot provide it.');
    } else {
        const raw = number(data.raw_percentage) ? `Instantaneous voltage reading: ${Math.round(data.raw_percentage)}%. ` : '';
        const capacity = number(data.capacity_ah) ? `${data.capacity_ah} Ah nominal capacity` : 'nominal capacity';
        const threshold = number(data.recharge_percentage) ? Math.round(data.recharge_percentage) : 60;
        const learningMinutes = number(data.runtime_min_history_seconds) ? Math.round(data.runtime_min_history_seconds / 60) : 5;
        set('explanation', `${raw}Smoothed charge estimate; battery age, load and temperature affect accuracy. Runtime uses ${capacity} and recent load, after ${learningMinutes} minutes of uninterrupted measurements, to the ${threshold}% recharge level. It is an approximation. Voltage warnings use the current measurement.`);
    }
}

function renderBatteryIndicators() {
    for (const [id, {data, received}] of batteryStates) {
        updateBatteryPanel(id, performance.now() - received <= 5000 ? data : {available: false});
    }
}

async function refreshBatteryIndicators() {
    if (document.hidden) return;
    await Promise.allSettled(machines.filter(machine => String(machine.type).toLowerCase() === 'robot').map(async machine => {
        if (batteryRequests.has(machine.id)) return;
        batteryRequests.add(machine.id);
        const modeVersion = batteryAnnouncementVersions.get(machine.id) || 0;
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 4000);
        let data;
        try {
            const response = await fetch(`/api/machines/${encodeURIComponent(machine.id)}/battery`, {cache: 'no-store', signal: controller.signal});
            if (!response.ok) throw new Error(`Battery status ${response.status}`);
            data = await response.json();
        } catch (_) {
            data = {available: false};
        } finally {
            clearTimeout(timeout);
            batteryRequests.delete(machine.id);
        }
        // A poll started before a save must not overwrite its confirmed selection.
        if (modeVersion !== (batteryAnnouncementVersions.get(machine.id) || 0)) {
            data.announcement_mode = batteryStates.get(machine.id)?.data.announcement_mode;
        }
        batteryStates.set(machine.id, {data, received: performance.now()});
        updateBatteryPanel(machine.id, data);
    }));
}

function initBatteryIndicators() {
    if (batteryRefreshTimer !== null) return;
    refreshBatteryIndicators();
    batteryRefreshTimer = setInterval(refreshBatteryIndicators, 2000);
    setInterval(renderBatteryIndicators, 1000);
    document.addEventListener('visibilitychange', () => {
        renderBatteryIndicators();
        if (!document.hidden) refreshBatteryIndicators();
    });
}

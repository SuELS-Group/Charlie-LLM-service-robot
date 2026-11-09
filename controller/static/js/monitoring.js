'use strict';

let networkChart = null;
let refreshTimer = null;

document.addEventListener('DOMContentLoaded', () => {
    lucide.createIcons();
    initNetworkChart();
    monitoringRefresh();
    refreshTimer = setInterval(monitoringRefresh, 3000);
    updateClock();
    setInterval(updateClock, 1000);
});


function updateClock() {
    const el = document.getElementById('monitoring-clock');
    if (el) {
        el.textContent = new Date().toLocaleTimeString();
    }
}


async function monitoringRefresh() {
    await Promise.all([
        fetchTopicStats(),
        fetchNetworkStats(),
    ]);
}


async function fetchTopicStats() {
    try {
        const resp = await fetch('/api/monitoring/topics');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        renderTopicBars(data.services || []);
        updateStatus(true);
    } catch (err) {
        console.warn('Topic stats fetch failed:', err);
        updateStatus(false);
    }
}


async function fetchNetworkStats() {
    try {
        const resp = await fetch('/api/monitoring/network');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();

        document.getElementById('net-iface-label').textContent =
            data.interface || 'unknown';
        const dot = document.querySelector('#net-status-indicator .server-dot');
        if (dot) {
            dot.className = 'server-dot' + (data.status && data.status.is_up ? '' : ' server-dot-offline');
        }

        document.getElementById('val-rx-dropped').textContent =
            formatNumber(data.counters ? data.counters.rx_dropped : 0);
        document.getElementById('val-tx-dropped').textContent =
            formatNumber(data.counters ? data.counters.tx_dropped : 0);
        document.getElementById('val-rx-errors').textContent =
            formatNumber(data.counters ? data.counters.rx_errors : 0);
        document.getElementById('val-tx-errors').textContent =
            formatNumber(data.counters ? data.counters.tx_errors : 0);

        const rxD = parseInt(data.counters ? data.counters.rx_dropped || 0 : 0);
        const txD = parseInt(data.counters ? data.counters.tx_dropped || 0 : 0);
        const rxE = parseInt(data.counters ? data.counters.rx_errors || 0 : 0);
        const txE = parseInt(data.counters ? data.counters.tx_errors || 0 : 0);
        applyDropClass('val-rx-dropped', rxD);
        applyDropClass('val-tx-dropped', txD);
        applyDropClass('val-rx-errors', rxE);
        applyDropClass('val-tx-errors', txE);

        if (data.history && data.history.length > 0) {
            updateNetworkChart(data.history);
        }
    } catch (err) {
        console.warn('Network stats fetch failed:', err);
    }
}


function applyDropClass(id, value) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('warn', 'crit');
    if (value > 10000) el.classList.add('crit');
    else if (value > 100) el.classList.add('warn');
}


function formatNumber(n) {
    if (n === null || n === undefined) return '--';
    n = parseInt(n);
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return String(n);
}


function updateStatus(ok) {
    const el = document.getElementById('monitoring-status');
    if (!el) return;
    const dot = el.querySelector('.server-dot');
    const text = el.querySelector('.server-status-text');
    if (dot) {
        dot.className = 'server-dot' + (ok ? '' : ' server-dot-offline');
    }
    if (text) {
        text.textContent = ok ? 'Connected' : 'No data';
    }
}


function initNetworkChart() {
    const ctx = document.getElementById('chart-network');
    if (!ctx) return;
    networkChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [
                {
                    label: 'RX',
                    data: [],
                    borderColor: '#5b8af5',
                    backgroundColor: 'rgba(91, 138, 245, 0.08)',
                    borderWidth: 1.5,
                    pointRadius: 0,
                    tension: 0.2,
                    fill: true,
                },
                {
                    label: 'TX',
                    data: [],
                    borderColor: '#3ddc84',
                    backgroundColor: 'rgba(61, 220, 132, 0.05)',
                    borderWidth: 1.5,
                    pointRadius: 0,
                    tension: 0.2,
                    fill: true,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 200 },
            interaction: {
                intersect: false,
                mode: 'index',
            },
            plugins: {
                legend: {
                    display: true,
                    position: 'top',
                    labels: {
                        color: '#a0a4b0',
                        boxWidth: 12,
                        padding: 8,
                        font: { size: 11 },
                    },
                },
                tooltip: {
                    callbacks: {
                        label: function(ctx) {
                            const kb = parseFloat(ctx.parsed.y);
                            if (kb >= 1024) return ctx.dataset.label + ': ' + (kb / 1024).toFixed(1) + ' MiB/s';
                            return ctx.dataset.label + ': ' + kb.toFixed(1) + ' KiB/s';
                        },
                    },
                },
            },
            scales: {
                x: {
                    display: true,
                    ticks: {
                        color: '#606570',
                        font: { size: 10 },
                        maxTicksLimit: 8,
                    },
                    grid: { color: 'rgba(255,255,255,0.03)' },
                },
                y: {
                    display: true,
                    title: {
                        display: false,
                    },
                    ticks: {
                        color: '#606570',
                        font: { size: 10 },
                        callback: function(v) {
                            if (v >= 1024) return (v / 1024).toFixed(1) + 'M';
                            return v.toFixed(0) + 'K';
                        },
                    },
                    grid: { color: 'rgba(255,255,255,0.03)' },
                    beginAtZero: true,
                },
            },
        },
    });
}


function updateNetworkChart(history) {
    if (!networkChart) return;
    const labels = [];
    const rxData = [];
    const txData = [];
    for (let i = 0; i < history.length; i++) {
        const age = (history.length - 1 - i) * 1.0;
        labels.push('-' + formatAge(age));
        rxData.push((history[i].rx_bytes_per_sec || 0) / 1024);
        txData.push((history[i].tx_bytes_per_sec || 0) / 1024);
    }
    networkChart.data.labels = labels;
    networkChart.data.datasets[0].data = rxData;
    networkChart.data.datasets[1].data = txData;
    networkChart.update('none');
}


function formatAge(seconds) {
    if (seconds < 60) return Math.round(seconds) + 's';
    return Math.round(seconds / 60) + 'm';
}


function renderTopicBars(topics) {
    const categories = {
        heartbeat: document.getElementById('topic-bars-heartbeat'),
        camera: document.getElementById('topic-bars-camera'),
        communication: document.getElementById('topic-bars-communication'),
    };

    for (const key of Object.keys(categories)) {
        if (!categories[key]) continue;
        const group = topics.filter(t => t.category === key);
        if (group.length === 0) {
            categories[key].innerHTML = '<div class="monitoring-empty">No data yet</div>';
            continue;
        }
        categories[key].innerHTML = group.map(t => serviceBarHTML(t)).join('');
    }
}


function serviceBarHTML(svc) {
    const active = svc.active === true;
    const status = svc.status || (active ? 'active' : 'unknown');
    const age = svc.age_s || 0;
    const barClass = active ? 'ok' : 'crit';
    let valueText = active ? 'active' : status;
    if (!active && age > 0) {
        const mins = Math.floor(age / 60);
        const secs = Math.floor(age % 60);
        if (mins > 0) {
            valueText += ' (down ' + mins + 'm ' + secs + 's)';
        } else {
            valueText += ' (down ' + secs + 's)';
        }
    }

    return (
        '<div class="topic-bar-row" title="' +
        escapeAttr(svc.description || '') + '">' +
        '<span class="topic-bar-label">' +
        escapeHtml(svc.label || svc.name) +
        '</span>' +
        '<div class="topic-bar-fill">' +
        '<div class="topic-bar-inner ' + barClass + '" style="width:' + (active ? 100 : 0) + '%"></div>' +
        '</div>' +
        '<span class="topic-bar-value' + (!active ? ' stale' : '') + '">' +
        valueText +
        '</span>' +
        '</div>'
    );
}


function escapeHtml(str) {
    return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}


function escapeAttr(str) {
    return String(str || '').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

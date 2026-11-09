const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

function view() {
    const fields = new Map();
    const panel = {dataset: {}, querySelector(selector) {
        if (!fields.has(selector)) fields.set(selector, {textContent: '', innerHTML: '', style: {}});
        return fields.get(selector);
    }};
    let now = 100;
    const context = vm.createContext({document: {getElementById: () => panel}, performance: {now: () => now}, Map, Set});
    vm.runInContext(readFileSync(path.join(__dirname, 'static/js/controller-battery.js'), 'utf8'), context);
    return {context, panel, field: name => panel.querySelector(`[data-battery="${name}"]`), tick: value => {now = value;}};
}

test('a critical voltage is visible even when charge smoothing is still high', () => {
    const {context, panel, field} = view();
    context.updateBatteryPanel('robot', {available: true, percentage: 80, voltage: 10.1, voltage_warning: 'critical', load_current: 4, quality: 'estimate'});
    assert.equal(panel.dataset.tone, 'critical');
    assert.equal(field('percentage').textContent, '≈ 80%');
    assert.equal(field('quality').textContent, 'Critical voltage');
});

test('charge connection is separate from charge confirmation and has no invented percentage', () => {
    const {context, field} = view();
    context.updateBatteryPanel('robot', {available: true, percentage: null, charger_connected: true, charging_state: 'not charging', runtime_status: 'connected'});
    assert.equal(field('percentage').textContent, '—');
    assert.equal(field('state').textContent, 'Connected · not charging');
    assert.equal(field('runtime-label').textContent, 'Charger');
    assert.equal(field('runtime').textContent, 'Not charging');
});

test('charging switches to time until full and shows measured charge current', () => {
    const {context, field} = view();
    context.updateBatteryPanel('robot', {available: true, percentage: 81, charger_connected: true, charging_state: 'charging',
        runtime_status: 'connected', time_to_full_seconds: 2700, charging_current: 2.3, method: 'charge_current_counting'});
    assert.equal(field('runtime-label').textContent, 'Until full');
    assert.equal(field('runtime').textContent, '≈ 45 min');
    assert.equal(field('current-label').textContent, 'Charge current');
    assert.equal(field('load').textContent, '2.3 A');
    assert.doesNotMatch(field('runtime').textContent, /External power/);
});

test('the inline opener toggles a separate card without changing machine state', () => {
    const {context, panel} = view();
    panel.hidden = true;
    const attributes = {};
    const button = {dataset: {machineId: 'robot'}, setAttribute: (key, value) => {attributes[key] = value;}};
    context.toggleBatteryDetails(button);
    assert.equal(panel.hidden, false);
    assert.equal(attributes['aria-expanded'], 'true');
    context.toggleBatteryDetails(button);
    assert.equal(panel.hidden, true);
    assert.equal(attributes['aria-expanded'], 'false');
});

test('offline and locally expired data clear percentages, runtime and the plot', () => {
    const {context, field, tick} = view();
    vm.runInContext("batteryStates.set('robot', {received: 100, data: {available: true, percentage: 81, runtime_status: 'estimate', runtime_to_recharge_seconds: 2400}})", context);
    context.renderBatteryIndicators();
    assert.equal(field('runtime').textContent, '≈ 40 min');
    tick(6000);
    context.renderBatteryIndicators();
    assert.equal(field('percentage').textContent, '—');
    assert.equal(field('runtime').textContent, 'Unavailable');
    assert.equal(field('chart').innerHTML, '');
});

test('zero percent and recharge recommendation are displayed as valid values', () => {
    const {context, field} = view();
    context.updateBatteryPanel('robot', {available: true, percentage: 0, runtime_status: 'recharge_recommended'});
    assert.equal(field('percentage').textContent, '≈ 0%');
    assert.equal(field('runtime').textContent, 'Recharge recommended');
});

test('the chart accepts finite numeric samples and rejects injected markup', () => {
    const {context} = view();
    const history = [{sampled_at: 1, voltage: 12, filtered_voltage: 11.9}, {sampled_at: 11, voltage: 11.8, filtered_voltage: 11.9},
        {sampled_at: 12, voltage: '<script>bad</script>', filtered_voltage: 12}, {sampled_at: 13, voltage: NaN, filtered_voltage: 12}];
    const chart = context.batteryChartMarkup(history);
    assert.match(chart, /battery-chart-filtered/);
    assert.doesNotMatch(chart, /script|NaN|Infinity/);
});

test('announcement mode remains editable without measurements and disables when the bridge is unavailable', () => {
    const {context, field} = view();
    context.updateBatteryPanel('robot', {available: false, announcement_mode: 'off'});
    assert.equal(field('announcement-mode').value, 'off');
    assert.equal(field('announcement-mode').disabled, false);
    context.updateBatteryPanel('robot', {available: false});
    assert.equal(field('announcement-mode').disabled, true);
    assert.equal(field('announcement-feedback').textContent, 'Nicht erreichbar');
});

test('announcement controls default to reduced and offer all three modes', () => {
    const {context} = view();
    context.escapeHtml = value => value;
    const markup = context.renderBatteryInline({id: 'robot'});
    assert.match(markup, /value="reduced" selected/);
    assert.match(markup, /value="full"/);
    assert.match(markup, /value="off"/);
    assert.match(markup, /onchange="setBatteryAnnouncementMode\(this\)"/);
});

test('a confirmed save survives refresh, while a failed save restores the confirmed mode', async () => {
    const {context, field} = view();
    context.AbortSignal = AbortSignal;
    vm.runInContext("batteryStates.set('robot', {received: 100, data: {available: false, announcement_mode: 'reduced'}})", context);
    const select = field('announcement-mode');
    select.dataset = {machineId: 'robot'};
    select.value = 'off';
    let complete;
    context.fetch = async (url, options) => {
        assert.equal(url, '/api/machines/robot/battery/announcements');
        assert.equal(options.method, 'POST');
        assert.equal(JSON.parse(options.body).mode, 'off');
        return new Promise(resolve => {complete = resolve;});
    };
    const saved = context.setBatteryAnnouncementMode(select);
    assert.equal(select.disabled, true);
    assert.equal(field('announcement-feedback').textContent, 'Speichert…');
    complete({ok: true, json: async () => ({mode: 'off'})});
    await saved;
    context.renderBatteryIndicators();
    assert.equal(select.value, 'off');
    assert.equal(select.disabled, false);
    context.fetch = async () => ({ok: false});
    select.value = 'full';
    await context.setBatteryAnnouncementMode(select);
    assert.equal(select.value, 'off');
    assert.equal(field('announcement-feedback').textContent, 'Speichern fehlgeschlagen');
});

test('a poll started before a save cannot overwrite the newly saved mode', async () => {
    const {context, field} = view();
    Object.assign(context, {AbortSignal, AbortController, setTimeout, clearTimeout, machines: [{id: 'robot', type: 'robot'}]});
    vm.runInContext("batteryStates.set('robot', {received: 100, data: {available: false, announcement_mode: 'reduced'}})", context);
    let completePoll;
    context.fetch = async (_url, options) => options.method === 'POST' ?
        {ok: true, json: async () => ({mode: 'off'})} : new Promise(resolve => {completePoll = resolve;});
    const poll = context.refreshBatteryIndicators();
    const select = field('announcement-mode');
    select.dataset = {machineId: 'robot'};
    select.value = 'off';
    await context.setBatteryAnnouncementMode(select);
    completePoll({ok: true, json: async () => ({available: false, announcement_mode: 'reduced'})});
    await poll;
    assert.equal(select.value, 'off');
});

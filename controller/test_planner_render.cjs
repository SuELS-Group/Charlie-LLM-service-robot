const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

function renderer() {
    const elements = new Map();
    const element = (id) => {
        if (!elements.has(id)) elements.set(id, {innerHTML: '', textContent: '', hidden: false});
        return elements.get(id);
    };
    const context = vm.createContext({console, Date, Intl, window: {}, document: {
        getElementById: element, querySelector: () => element('title'), querySelectorAll: () => [],
    }});
    for (const file of ['planner-core.js', 'planner-render.js']) {
        vm.runInContext(fs.readFileSync(path.join(__dirname, 'static/js', file), 'utf8'), context);
    }
    return {context, element};
}

function session() {
    const question = 'Welcher Wegpunkt? current_location oder grabbing point?';
    return {id: 'live', user_request: 'Charlie, fahre zum Wegpunkt', status: 'awaiting_input',
        phase: 'awaiting_input', response_text: question,
        clarifications: [{question, state: 'waiting', expires_at: new Date(Date.now() + 30000).toISOString()}],
        plans: [{action: 'ask_user', status: 'awaiting_input', decision: {answer: question}, steps: []}]};
}

test('an open question is prominent and never labelled as a completed request or final response', () => {
    const {context, element} = renderer();
    context.plannerState = {current_session: session(), history: []};
    vm.runInContext('plannerState = globalThis.plannerState; renderPlannerState()', context);
    assert.equal(element('planner-live-question').hidden, false);
    assert.match(element('planner-live-question').innerHTML, /Waiting for your reply/);
    assert.match(element('planner-live-question').innerHTML, /grabbing point/);
    assert.match(element('planner-live-question').innerHTML, /\d+s left to reply/);
    const html = element('current-session-panel').innerHTML;
    assert.match(html, /Asks for clarification/);
    assert.match(html, /Waiting for reply/);
    assert.doesNotMatch(html, /Completes request|Final Response|No steps were generated/);
});

test('the current question stays visible while inspecting an older session', () => {
    const {context, element} = renderer();
    context.snapshot = {current_session: session(), history: [{id: 'old', status: 'timeout', plans: []}]};
    vm.runInContext('plannerState = snapshot; selectedHistorySessionId = "old"; renderPlannerState()', context);
    assert.match(element('planner-live-question').innerHTML, /Show live question/);
    assert.match(element('planner-live-question').innerHTML, /grabbing point/);
    assert.equal(element('title').textContent, 'Historical Session');
});

test('accepted replies remain readable in history and all transcript HTML is escaped', () => {
    const {context, element} = renderer();
    const completed = session();
    completed.status = 'success';
    completed.clarifications[0].state = 'answered';
    completed.clarifications[0].answer = 'Hey Charlie <img src=x onerror=alert(1)>';
    completed.clarifications[0].rejected_replies = [{text: '<script>bad</script>', reason: 'Expired'}];
    context.snapshot = {current_session: null, history: [completed]};
    vm.runInContext('plannerState = snapshot; renderPlannerState()', context);
    const html = element('current-session-panel').innerHTML;
    assert.equal(element('planner-live-question').hidden, true);
    assert.match(html, /Answer received/);
    assert.match(html, /Hey Charlie &lt;img/);
    assert.match(html, /Reply not accepted/);
    assert.doesNotMatch(html, /<img|<script>/);
});

test('a locally elapsed deadline waits for the server instead of claiming an active reply window', () => {
    const {context} = renderer();
    assert.match(context.clarificationTimeLeft('2000-01-01T00:00:00Z'), /ended/);
    assert.doesNotMatch(context.clarificationTimeLeft('2000-01-01T00:00:00Z'), /open|left to reply/);
});

test('a replaced clarification shows the new request separately from an accepted answer', () => {
    const {context, element} = renderer();
    const previous = session();
    previous.status = 'superseded';
    previous.response_text = 'Ich verwerfe den bisherigen Auftrag.';
    previous.clarifications[0].state = 'superseded';
    previous.clarifications[0].replacement_request = 'Charlie, wie geht es dir?';
    previous.plans.push({action: 'replace_request', status: 'superseded', steps: []});
    context.snapshot = {current_session: null, history: [previous]};
    vm.runInContext('plannerState = snapshot; renderPlannerState()', context);
    const html = element('current-session-panel').innerHTML;
    assert.match(html, /Replaced by a new request/);
    assert.match(html, /Charlie, wie geht es dir\?/);
    assert.match(html, /Request change/);
    assert.match(html, /Replaces previous request/);
    assert.doesNotMatch(html, /Answer received|Received reply|Completes request/);
});

test('task progress distinguishes confirmed, unspoken and unexecuted goals and escapes labels', () => {
    const {context, element} = renderer();
    const completed = session();
    completed.status = 'failure';
    completed.planning_decisions = 4;
    completed.task_progress = [
        {label: 'Arm kalibrieren', completion_tool: 'calibrate_arm', status: 'completed'},
        {label: 'Akkustand <script>bad</script>', completion_tool: 'battery_status', status: 'ready'},
        {label: 'Vorwärts fahren', completion_tool: 'move_relative', status: 'pending'},
    ];
    context.snapshot = {history: [completed]};
    vm.runInContext('plannerState = snapshot; renderPlannerState()', context);
    const html = element('current-session-panel').innerHTML;
    assert.match(html, /Task progress · 1 \/ 3 completed/);
    assert.match(html, /4 planning decisions/);
    assert.match(html, /Retrieved · not yet spoken/);
    assert.match(html, /Not run/);
    assert.match(html, /Akkustand &lt;script&gt;/);
    assert.doesNotMatch(html, /<script>/);
});

test('A recovery block is shown as not started', () => {
    const {context} = renderer();
    const html = context.renderTaskProgress([{id: 'calibrate', label: 'Calibrate arm',
        completion_tool: 'calibrate_arm', status: 'blocked', evidence: []}], 'failure', 1);
    assert.match(html, /Not started · blocked/);
    assert.doesNotMatch(html, /Result unconfirmed/);
});

test('failed planning attempts expose output limits and retries without claiming completion', () => {
    const {context, element} = renderer();
    context.snapshot = {history: [{id: 'failure', status: 'failure', error_text: 'Output limit reached',
        planning_error: {code: 'output_limit'}, plans: [
            {type: 'planning_attempt', label: 'Decision 2 · attempt 1', status: 'failure',
                error_code: 'output_limit', retry_scheduled: true, context_window: {
                    max_tokens: 65536, used_tokens: 3000, response_tokens: 4096,
                    output_token_limit: 4096, finish_reason: 'length', elapsed_s: 10}},
            {type: 'planning_attempt', label: 'Decision 2 · attempt 2', status: 'failure',
                error_code: 'output_limit', retry_scheduled: false, context_window: {
                    max_tokens: 65536, used_tokens: 3000, response_tokens: 8192,
                    output_token_limit: 8192, finish_reason: 'length', elapsed_s: 20}},
        ]}]};
    vm.runInContext('plannerState = snapshot; renderPlannerState()', context);
    const html = element('current-session-panel').innerHTML;
    assert.match(html, /Planning retry scheduled/);
    assert.match(html, /4,096 \/ 4,096 output tokens/);
    assert.match(html, /8,192 \/ 8,192 output tokens/);
    assert.match(html, /finish: length/);
    assert.match(html, /Not executed/);
    assert.match(html, /Only planning is retried/);
    assert.doesNotMatch(html, /Completes request/);
});

test('runtime rejected tool calls show the concrete reason and never count as completed', () => {
    const {context} = renderer();
    const html = context.renderPlans([{action: 'call_tool', status: 'failure',
        error_code: 'blocked_before_execution', rejection_reason: 'Expected turn_to_relative <unsafe>',
        steps: [{name: 'move_relative', status: 'blocked'}]}]);
    assert.match(html, /Expected turn_to_relative &lt;unsafe&gt;/);
    assert.match(html, /Not executed/);
    assert.doesNotMatch(html, /Completes request|<unsafe>/);
});

test('driver runstop is visible at session level separately from planning errors', () => {
    const {context, element} = renderer();
    context.snapshot = {history: [{id: 'runstop', status: 'failure', plans: [],
        execution_error: {code: 'RUNSTOP_ACTIVE'}, error_text: 'Cannot execute goals while in mode=runstopped'}]};
    vm.runInContext('plannerState = snapshot; renderPlannerState()', context);
    const html = element('current-session-panel').innerHTML;
    assert.match(html, /RUNSTOP_ACTIVE/);
    assert.match(html, /mode=runstopped/);
});

test('a reused lookup is explicitly distinguished from a new tool execution', () => {
    const {context} = renderer();
    const html = context.renderPlans([{status: 'reused', action: 'call_tool', needs_more_planning: true,
        steps: [{name: 'get_waypoints', status: 'reused', reused_result: true, source_result_index: 0,
            result_summary: 'waypoints=[desk]'}]}]);
    assert.match(html, /Existing result 1 reused · no new tool call/);
    assert.match(html, /desk/);
    assert.doesNotMatch(html, /Completes request|Planning failed/);
});

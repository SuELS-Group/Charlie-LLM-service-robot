function renderPlannerState() {
    plannerCopyTexts = {};
    plannerCopyCounter = 0;
    const {session, isHistory, isLastCompleted} = getDisplayedSession(plannerState);
    renderOverview(plannerState, session, isHistory, isLastCompleted);
    renderLiveClarification(plannerState, isHistory);
    renderCurrentSession(session, isHistory, isLastCompleted);
    renderHistory(plannerState?.history || []);
    plannerReinitIcons();
}

function clarificationTimeLeft(expiresAt) {
    const deadline = Date.parse(expiresAt || '');
    if (!Number.isFinite(deadline)) return 'Reply window open';
    const remaining = Math.ceil((deadline - Date.now()) / 1000);
    return remaining > 0 ? `${remaining}s left to reply` : 'Reply window ended · waiting for update';
}

function updateClarificationCountdowns() {
    document.querySelectorAll('[data-clarification-deadline]').forEach((element) => {
        element.textContent = clarificationTimeLeft(element.dataset.clarificationDeadline);
    });
}

function renderLiveClarification(state, isHistory) {
    const banner = document.getElementById('planner-live-question');
    if (!banner) return;
    const session = state?.current_session;
    const questions = session?.clarifications || [];
    const question = questions[questions.length - 1];
    banner.hidden = session?.status !== 'awaiting_input';
    if (banner.hidden) {
        banner.innerHTML = '';
        return;
    }
    banner.innerHTML = `
        <strong>Waiting for your reply</strong>
        <p>${escapeHtml(question?.question || session.response_text || '')}</p>
        <div class="planner-clarification-meta">
            <span>Say “Charlie”, then your answer.</span>
            ${question?.expires_at ? `<span data-clarification-deadline="${escapeHtml(question.expires_at)}">${escapeHtml(clarificationTimeLeft(question.expires_at))}</span>` : ''}
            ${isHistory ? '<a class="planner-inline-link" href="#" onclick="resumeLiveSession(); return false;">Show live question</a>' : ''}
        </div>`;
}

function renderClarifications(questions) {
    if (!Array.isArray(questions) || !questions.length) return '';
    const labels = {waiting: 'Waiting for reply', answered: 'Answer received',
        expired: 'Reply window expired', cancelled: 'Cancelled', closed: 'Closed',
        superseded: 'Replaced by a new request'};
    return `<section class="planner-clarification-list" aria-label="Questions and replies">
        ${questions.map((question) => `
            <article class="planner-clarification ${question.state === 'waiting' ? 'waiting' : ''}">
                <div class="planner-clarification-meta">
                    <strong>Clarification</strong>
                    <span class="planner-badge ${question.state === 'waiting' ? 'warning' : (question.state === 'answered' ? 'success' : 'idle')}">${escapeHtml(labels[question.state] || question.state)}</span>
                </div>
                <p>${escapeHtml(question.question)}</p>
                ${question.replacement_request
        ? `<div class="planner-clarification-answer"><strong>New request</strong><p>${escapeHtml(question.replacement_request)}</p></div>`
        : (question.answer ? `<div class="planner-clarification-answer"><strong>Received reply</strong><p>${escapeHtml(question.answer)}</p></div>` : '')}
                ${(question.rejected_replies || []).map((reply) => `<div class="planner-clarification-rejected"><strong>Reply not accepted</strong><p>${escapeHtml(reply.text)}</p><p>${escapeHtml(reply.reason)}</p></div>`).join('')}
                <small>${escapeHtml(formatTimestamp(question.asked_at))}</small>
            </article>`).join('')}
    </section>`;
}

function renderTaskProgress(tasks, status, decisions) {
    if (!Array.isArray(tasks) || !tasks.length) return '';
    const completed = tasks.filter(task => task.status === 'completed').length;
    const active = ['active', 'planning', 'executing', 'awaiting_input'].includes(status);
    const labels = {completed: 'Confirmed', ready: 'Retrieved · not yet spoken',
        needs_answer: 'Search finished · answer pending',
        in_progress: active ? 'In progress' : 'Not completed', pending: active ? 'Pending' : 'Not run',
        failed: 'Failed', blocked: 'Not started · blocked', uncertain: 'Result unconfirmed'};
    return `<section class="planner-task-progress" aria-label="Task progress">
        <div class="planner-clarification-meta"><strong>Task progress · ${completed} / ${tasks.length} completed</strong>
            <span>${escapeHtml(String(decisions ?? 0))} planning decisions</span></div>
        <ol>${tasks.map(task => `<li class="planner-task-row">
            <span>${escapeHtml(task.label)}</span>
            <span class="planner-badge ${task.status === 'completed' ? 'success' : (task.status === 'failed' ? 'failure' : 'warning')}">${escapeHtml(labels[task.status] || task.status)}</span>
            <small>${escapeHtml(task.completion_tool)}</small>
        </li>`).join('')}</ol>
    </section>`;
}

function renderOverview(state, session, isHistory, isLastCompleted = false) {
    const latestPlan = getLatestPlan(session);
    const agent = state?.agent || {};
    const overview = document.getElementById('planner-overview');
    if (!overview) return;

    const sessionMode = isLastCompleted ? 'Last completed' : (isHistory ? 'History' : 'Live');
    const planCount = Array.isArray(session?.plans) ? session.plans.length : 0;
    const stepCount = countSessionSteps(session);
    const contextMetrics = latestPlan?.context_window || null;
    const retainedSessions = Array.isArray(state?.history) ? state.history.length : 0;

    overview.innerHTML = `
        <article class="planner-overview-card">
            <span class="planner-overview-label">Agent</span>
            <strong class="planner-overview-value">${escapeHtml((agent.status || 'idle').replaceAll('_', ' '))}</strong>
            <span class="planner-overview-meta">${agent.updated_at ? `Updated ${escapeHtml(relativeTime(agent.updated_at))}` : 'No live data yet'}</span>
        </article>
        <article class="planner-overview-card">
            <span class="planner-overview-label">Context Window</span>
            ${renderContextWindow(contextMetrics)}
        </article>
        <article class="planner-overview-card">
            <span class="planner-overview-label">Current Plan</span>
            <strong class="planner-overview-value">${escapeHtml(session ? `${sessionMode} session` : 'No active session')}</strong>
            <span class="planner-overview-meta">${escapeHtml(session ? `${formatCount(planCount, 'plan')}, ${formatCount(stepCount, 'step')} • ${formatCount(retainedSessions, 'recent session')}` : 'Planner has not executed a plan yet')}</span>
        </article>
    `;
}

function renderContextWindow(metrics) {
    if (!metrics) {
        return `
            <strong class="planner-overview-value">Waiting for planner activity</strong>
            <span class="planner-overview-meta">No planner prompt recorded yet</span>
        `;
    }

    const usedTokens = metrics.used_tokens;
    const maxTokens = metrics.max_tokens;
    const ratio = Number.isFinite(metrics.usage_ratio) ? metrics.usage_ratio : 0;
    const width = `${Math.max(0, Math.min(100, ratio * 100))}%`;
    const headline = usedTokens !== null && usedTokens !== undefined
        ? `${formatContextValue(usedTokens)} / ${formatContextValue(maxTokens)}`
        : `Max ${formatContextValue(maxTokens)}`;
    const secondary = usedTokens !== null && usedTokens !== undefined
        ? `${formatPercent(ratio)} used${metrics.remaining_tokens !== null && metrics.remaining_tokens !== undefined ? `, ${formatContextValue(metrics.remaining_tokens)} left` : ''}`
        : `${escapeHtml(metrics.model || 'Planner model')} configured`;

    return `
        <strong class="planner-overview-value">${headline}</strong>
        <div class="planner-progress">
            <div class="planner-progress-track">
                <div class="planner-progress-fill" style="width: ${width};"></div>
            </div>
        </div>
        <span class="planner-overview-meta">${secondary}</span>
    `;
}

function renderCurrentSession(session, isHistory = false, isLastCompleted = false) {
    const subtitle = document.getElementById('current-session-subtitle');
    const badge = document.getElementById('current-session-badge');
    const panel = document.getElementById('current-session-panel');
    const titleNode = document.querySelector('.planner-panel-main h1');

    if (titleNode) {
        titleNode.textContent = isLastCompleted
            ? 'Last Completed Session'
            : (isHistory ? 'Historical Session' : 'Live Session');
    }

    if (!session) {
        subtitle.innerHTML = isHistory
            ? '<a class="planner-inline-link" href="#" onclick="resumeLiveSession(); return false;">Back to Live Session</a>'
            : 'The planner is idle.';
        badge.className = 'planner-badge idle';
        badge.textContent = 'Idle';
        panel.innerHTML = `
            <div class="planner-empty-state">
                ${plannerIcon('bot')}
                <p>Waiting for the next planner session.</p>
            </div>
        `;
        return;
    }

    subtitle.innerHTML = isHistory
        ? '<a class="planner-inline-link" href="#" onclick="resumeLiveSession(); return false;">Back to Live View</a>'
        : escapeHtml(session.phase_detail || 'Planner session in progress');

    badge.className = `planner-badge ${plannerStatusClass(session.status || session.phase)}`;
    badge.textContent = session.status || session.phase || 'active';
    panel.innerHTML = `
        <div class="planner-session-stack">
            <section class="planner-section">
                <span class="planner-section-label">Request</span>
                <div class="planner-request-value">${escapeHtml(session.user_request || session.english_request || 'Unknown')}</div>
            </section>

            ${renderClarifications(session.clarifications || [])}
            ${renderTaskProgress(session.task_progress, session.status, session.planning_decisions)}

            <section class="planner-meta-grid">
                <div class="planner-meta-chip">
                    <span>Status</span>
                    <strong>${escapeHtml(session.status || 'active')}</strong>
                </div>
                <div class="planner-meta-chip">
                    <span>Phase</span>
                    <strong>${escapeHtml(session.phase || 'idle')}</strong>
                </div>
                <div class="planner-meta-chip">
                    <span>Updated</span>
                    <strong>${escapeHtml(relativeTime(session.updated_at))}</strong>
                </div>
                <div class="planner-meta-chip">
                    <span>Started</span>
                    <strong>${escapeHtml(formatTimestamp(session.started_at))}</strong>
                </div>
            </section>

            <section class="planner-section">
                <span class="planner-section-label">Selected Tools</span>
                <div class="planner-chip-row">
                    ${(session.selected_tools || []).map((tool) => `<span class="planner-chip">${escapeHtml(tool)}</span>`).join('')
    || '<span class="planner-chip muted">No tool selection recorded</span>'}
                </div>
            </section>

            ${session.response_text && session.status !== 'awaiting_input' ? `
                <section class="planner-response-box">
                    <span class="planner-section-label">${session.status === 'superseded' ? 'Request change' : 'Final Response'}</span>
                    <p>${escapeHtml(session.response_text)}</p>
                </section>
            ` : ''}

            ${session.error_text ? `
                <section class="planner-response-box error">
                    <span class="planner-section-label">${escapeHtml(session.planning_error?.code || session.execution_error?.code || 'Error')}</span>
                    <p>${escapeHtml(session.error_text)}</p>
                </section>
            ` : ''}

            <div class="planner-plan-list">
                ${renderPlans(session.plans || [])}
            </div>
        </div>
    `;
}

function renderPlans(plans) {
    if (!Array.isArray(plans) || plans.length === 0) {
        return `
            <div class="planner-empty-state compact">
                ${plannerIcon('workflow')}
                <p>No execution plans yet.</p>
            </div>
        `;
    }

    return plans.map((plan) => {
        const contextWindow = plan.context_window || null;
        const asksUser = plan.action === 'ask_user';
        const replacesRequest = plan.action === 'replace_request';
        const failedAttempt = plan.type === 'planning_attempt' || !!plan.error_code;
        const progressLabel = failedAttempt ? (plan.retry_scheduled ? 'Planning retry scheduled' : 'Not executed')
            : (replacesRequest ? 'Replaces previous request' : (asksUser ? 'Asks for clarification' : (plan.needs_more_planning ? 'Needs follow-up plan' : 'Completes request')));
        return `
            <section class="planner-plan-card">
                <header class="planner-plan-header">
                    <div>
                        <h3>${escapeHtml(plan.label || `Plan ${Number(plan.iteration || 0) + 1}`)}</h3>
                        <p>${escapeHtml(plan.reasoning || 'No reasoning provided.')}</p>
                    </div>
                    <div class="planner-plan-status">
                        <span class="planner-badge ${plannerStatusClass(plan.status)}">${escapeHtml(plan.status || 'pending')}</span>
                        <span class="planner-plan-meta">${escapeHtml(formatTimestamp(plan.created_at))}</span>
                    </div>
                </header>
                <div class="planner-plan-meta-grid">
                    <div class="planner-plan-meta-card">
                        <span>Planner Prompt</span>
                        <strong>${renderContextHeadline(contextWindow)}</strong>
                        <small>${renderContextDetail(contextWindow)}</small>
                    </div>
                    <div class="planner-plan-meta-card">
                        <span>Progress</span>
                        <strong>${escapeHtml(progressLabel)}</strong>
                        <small>${escapeHtml(formatCount((plan.steps || []).length, 'step'))}</small>
                    </div>
                </div>
                ${failedAttempt ? `<div class="planner-attempt-error"><strong>${escapeHtml(plan.error_code)}</strong><p>${escapeHtml(plan.rejection_reason || (plan.retry_scheduled ? 'Only planning is retried. No tool was executed from this attempt.' : 'This decision did not execute a tool.'))}</p></div>` : ''}
                ${asksUser && plan.decision?.answer ? `<div class="planner-decision-question"><strong>Question</strong><p>${escapeHtml(plan.decision.answer)}</p></div>` : ''}
                ${renderPlanProgressBar(contextWindow)}
                ${renderPromptDebug(plan.prompt_debug || null)}
                <div class="planner-step-list">
                    ${(plan.steps || []).map(renderStep).join('') || `
                        <div class="planner-empty-state compact">
                            ${plannerIcon('circle-dashed')}
                            <p>${failedAttempt ? 'Planning failed before tool execution.' : (replacesRequest ? 'The previous request is dropped. The new request is processed separately.' : (asksUser ? 'The robot is asking a question before continuing.' : 'No steps were generated for this plan.'))}</p>
                        </div>
                    `}
                </div>
            </section>
        `;
    }).join('');
}

function renderPromptDebug(debug) {
    if (!debug) {
        return '';
    }
    const messageCount = Array.isArray(debug.messages) ? debug.messages.length : 0;
    const promptChars = (debug.full_system_prompt || '').length + (debug.user_prompt || '').length;
    const rawParsedPlan = debug.raw_parsed_plan ? JSON.stringify(debug.raw_parsed_plan, null, 2) : '';
    const compiledPlan = debug.compiled_plan ? JSON.stringify(debug.compiled_plan, null, 2) : '';
    const rawMessages = Array.isArray(debug.messages) ? JSON.stringify(debug.messages, null, 2) : '';
    return `
        <details class="planner-prompt-debug">
            <summary>
                <span>LLM Debug Exchange</span>
                <small>${escapeHtml(`${formatContextValue(promptChars)} chars • ${messageCount} messages`)}</small>
            </summary>
            <div class="planner-prompt-debug-body">
                <div class="planner-prompt-debug-grid">
                    <div class="planner-meta-chip">
                        <span>Model</span>
                        <strong>${escapeHtml(debug.model || 'Unknown')}</strong>
                    </div>
                    <div class="planner-meta-chip">
                        <span>Iteration</span>
                        <strong>${escapeHtml(String((debug.iteration ?? 0) + 1))}</strong>
                    </div>
                    <div class="planner-meta-chip">
                        <span>Continuation</span>
                        <strong>${escapeHtml(debug.is_continuation ? 'Yes' : 'No')}</strong>
                    </div>
                    <div class="planner-meta-chip">
                        <span>Context Limit</span>
                        <strong>${escapeHtml(formatContextValue(debug.max_context_tokens || 0))}</strong>
                    </div>
                </div>
                ${renderPromptDebugBlock('Full Input Messages', rawMessages || 'No messages recorded.')}
                ${renderPromptDebugBlock('System Prompt', debug.full_system_prompt || '')}
                ${renderPromptDebugBlock('User Prompt', debug.user_prompt || '')}
                ${renderPromptDebugBlock('Raw LLM Output', debug.raw_response || 'No raw output recorded.')}
                ${renderPromptDebugBlock('Parsed LLM Plan', rawParsedPlan || 'No parsed plan recorded.')}
                ${renderPromptDebugBlock('Compiled Execution Plan', compiledPlan || 'No compiled plan recorded.')}
            </div>
        </details>
    `;
}

function renderPromptDebugBlock(label, value) {
    const copyId = `planner-copy-${plannerCopyCounter++}`;
    plannerCopyTexts[copyId] = String(value || '');
    return `
        <section class="planner-prompt-block">
            <div class="planner-prompt-block-header">
                <span>${escapeHtml(label)}</span>
                <div class="planner-prompt-block-actions">
                    <small>${escapeHtml(formatCount(String(value || '').length, 'char'))}</small>
                    <button class="planner-copy-button" type="button" onclick="copyPlannerText('${copyId}')" title="Copy full text">
                        ${plannerIcon('copy')}
                    </button>
                </div>
            </div>
            <pre>${escapeHtml(value || '')}</pre>
        </section>
    `;
}

function renderContextHeadline(metrics) {
    if (!metrics) {
        return 'No prompt metrics';
    }
    if (metrics.used_tokens !== null && metrics.used_tokens !== undefined) {
        return `${formatContextValue(metrics.used_tokens)} / ${formatContextValue(metrics.max_tokens)} tokens`;
    }
    return `Max ${formatContextValue(metrics.max_tokens)} tokens`;
}

function renderContextDetail(metrics) {
    if (!metrics) {
        return 'Planner has not returned prompt token counts yet.';
    }
    const detailParts = [];
    if (metrics.model) {
        detailParts.push(metrics.model);
    }
    if (metrics.response_tokens !== null && metrics.response_tokens !== undefined) {
        detailParts.push(`${formatContextValue(metrics.response_tokens)}${metrics.output_token_limit ? ` / ${formatContextValue(metrics.output_token_limit)}` : ''} output tokens`);
    }
    if (metrics.finish_reason) detailParts.push(`finish: ${metrics.finish_reason}`);
    if (Number.isFinite(metrics.elapsed_s)) detailParts.push(`${metrics.elapsed_s.toFixed(2)}s`);
    if (metrics.prompt_chars) {
        detailParts.push(`${formatContextValue(metrics.prompt_chars)} prompt chars`);
    }
    return detailParts.join(' • ') || 'No extra metrics';
}

function renderPlanProgressBar(metrics) {
    if (!metrics || !Number.isFinite(metrics.usage_ratio)) {
        return '';
    }
    const width = `${Math.max(0, Math.min(100, metrics.usage_ratio * 100))}%`;
    return `
        <div class="planner-plan-progress">
            <div class="planner-progress-track compact">
                <div class="planner-progress-fill" style="width: ${width};"></div>
            </div>
            <span class="planner-plan-meta">${escapeHtml(formatPercent(metrics.usage_ratio))} of configured context window</span>
        </div>
    `;
}

function renderStep(step) {
    return `
        <article class="planner-step-card ${plannerStatusClass(step.status)}">
            <div class="planner-step-head">
                <div>
                    <h4>${escapeHtml(step.title || step.name || 'Step')}</h4>
                    <p>${escapeHtml(step.reason || 'No step rationale provided.')}</p>
                </div>
                <span class="planner-badge ${plannerStatusClass(step.status)}">${escapeHtml(step.status || 'pending')}</span>
            </div>
            <div class="planner-step-meta">
                <span>${escapeHtml(step.name || 'unknown')}</span>
                ${step.reused_result ? `<span>Existing result ${Number(step.source_result_index) + 1} reused · no new tool call</span>` : ''}
                ${step.should_announce ? '<span>Announces result</span>' : ''}
                ${step.returns_data ? '<span>Returns data</span>' : ''}
            </div>
            ${renderArgs(step.args)}
            ${step.result_summary && step.result_summary !== 'no data' ? `
                <div class="planner-result-line">
                    <strong>Result</strong>
                    <span>${escapeHtml(step.result_summary)}</span>
                </div>
            ` : ''}
            ${step.error ? `
                <div class="planner-result-line error">
                    <strong>Error</strong>
                    <span>${escapeHtml(step.error)}</span>
                </div>
            ` : ''}
            ${step.announcement ? `
                <div class="planner-result-line">
                    <strong>Announcement</strong>
                    <span>${escapeHtml(step.announcement)}</span>
                </div>
            ` : ''}
        </article>
    `;
}

function renderArgs(args) {
    if (!args || Object.keys(args).length === 0) {
        return '<div class="planner-args muted">No arguments</div>';
    }

    return `
        <div class="planner-args">
            ${Object.entries(args).map(([key, value]) => `
                <span class="planner-arg-pill">
                    <strong>${escapeHtml(key)}</strong>
                    <code>${escapeHtml(JSON.stringify(value))}</code>
                </span>
            `).join('')}
        </div>
    `;
}

function renderHistory(history) {
    const panel = document.getElementById('planner-history-panel');
    if (!panel) return;
    if (!Array.isArray(history) || history.length === 0) {
        panel.innerHTML = `
            <div class="planner-empty-state compact">
                ${plannerIcon('history')}
                <p>No completed sessions yet.</p>
            </div>
        `;
        return;
    }

    panel.innerHTML = history.map((session) => {
        const isSelected = session.id === selectedHistorySessionId;
        return `
            <button class="planner-history-button${isSelected ? ' selected' : ''}" onclick="viewHistorySession('${escapeHtml(session.id)}')">
                <div class="planner-history-main">
                    <strong class="planner-history-title">${escapeHtml(session.english_request || session.user_request || 'Unknown request')}</strong>
                    <span class="planner-history-meta">${escapeHtml(formatTimestamp(session.completed_at || session.updated_at))}</span>
                    <span class="planner-history-meta">${escapeHtml(`${formatCount((session.plans || []).length, 'plan')} • ${formatCount(countSessionSteps(session), 'step')} • ${formatCount((session.selected_tools || []).length, 'tool')}`)}</span>
                </div>
                <div class="planner-history-side">
                    <span class="planner-badge ${plannerStatusClass(session.status)}">${escapeHtml(session.status || 'unknown')}</span>
                    ${plannerIcon('chevron-right')}
                </div>
            </button>
        `;
    }).join('');
}

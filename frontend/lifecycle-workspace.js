/* frontend/lifecycle-workspace.js */
/* global AbortController */

const AgileForgeWorkspace = (() => {
    const STAGES = Object.freeze([
        [1, 'Create Project', 'framing'], [2, 'Vision', 'framing'],
        [3, 'Product Goal', 'framing'], [4, 'Specification', 'framing'],
        [5, 'Backlog', 'framing'], [6, 'Roadmap', 'framing'],
        [7, 'Stories', 'delivery'], [8, 'Sprint plan & start', 'delivery'],
        [9, 'Develop & verify', 'delivery'], [10, 'Close Stories', 'delivery'],
        [11, 'Review & close Sprint', 'delivery'], [12, 'Learn & triage', 'delivery'],
        [13, 'Assess next Sprint', 'delivery'],
    ].map(([id, label, group]) => Object.freeze({ id, label, group })));
    const STAGE_BY_REQUEST = Object.freeze({
        generate_vision_bootstrap: 2, record_vision_interview_turn: 2, decide_vision_review: 2,
        begin_vision_revision: 2, record_product_goal_interview_turn: 3,
        decide_product_goal_review: 3, fulfill_product_goal: 3, abandon_product_goal: 3,
        register_specification_source: 4, structure_specification: 4, decide_specification: 4,
        record_backlog_draft: 5, decide_backlog: 5, record_roadmap_draft: 6,
        decide_roadmap: 6, record_story_draft: 7, decide_story: 7,
        apply_story_dependencies: 7, repair_story_readiness: 7, record_sprint_plan: 8,
        decide_sprint_plan: 8, start_sprint: 8, start_sprint_retry: 8,
        complete_task: 9, close_story: 10, review_sprint: 11, close_sprint: 11,
        record_post_sprint_triage: 12, retry_sprint: 13,
    });
    const WAITING_REVIEW_KINDS = new Set([
        'decide_backlog', 'decide_product_goal_review', 'decide_roadmap',
        'decide_sprint_plan', 'decide_specification', 'decide_story',
        'decide_vision_review', 'review_sprint',
    ]);

    function stages() { return STAGES.map((stage) => ({ ...stage })); }

    function stageForDecision(decision) {
        if (!decision || typeof decision.request_kind !== 'string') return null;
        return STAGE_BY_REQUEST[decision.request_kind] ?? null;
    }

    function currentStageIds(position) {
        const ids = new Set();
        for (const decision of Array.isArray(position?.decisions) ? position.decisions : []) {
            const stageId = stageForDecision(decision);
            const recommendation = decision?.recommendation_kind;
            const required = recommendation === 'required' || recommendation === 'recovery';
            const waitingReview = decision?.category === 'waiting'
                && WAITING_REVIEW_KINDS.has(decision?.request_kind);
            if (stageId !== null && (decision?.category === 'available' && required || waitingReview)) {
                ids.add(stageId);
            }
        }
        return [...ids].sort((left, right) => left - right);
    }

    function exactAction(task, position, actions) {
        if (!positive(task?.task_id) || typeof task?.instance_key !== 'string'
            || typeof task?.fact_fingerprint !== 'string') return null;
        const decisions = (Array.isArray(position?.decisions) ? position.decisions : []).filter((decision) => (
            decision?.request_kind === 'complete_task'
            && decision?.category === 'available'
            && ['NEXT_TASK_READY', 'IN_PROGRESS_TASK_REQUIRED'].includes(decision?.reason_code)
            && decision?.instance_key === task.instance_key
            && typeof decision?.decision_fingerprint === 'string'
            && decision.decision_fingerprint.trim()
            && Array.isArray(decision?.fact_references)
            && decision.fact_references.some((reference) => (
                reference?.fact_type === 'task'
                && String(reference?.fact_id) === String(task.task_id)
                && reference?.fingerprint === task.fact_fingerprint
            ))
        ));
        if (decisions.length !== 1) return null;
        const matches = (Array.isArray(actions) ? actions : []).filter((action) => (
            action?.request_kind === 'complete_task'
            && action?.node_id === decisions[0].node_id
            && action?.instance_key === task.instance_key
            && action?.endpoint === 'sprint/task/complete'
            && action?.availability !== 'locked'
        ));
        return matches.length === 1 ? { ...matches[0] } : null;
    }

    function taskRows(status, position, actions) {
        const tasks = Array.isArray(status?.tasks) ? status.tasks : (Array.isArray(status?.items) ? status.items : []);
        return tasks.filter((task) => positive(task?.task_id)).map((task) => {
            if (task.status === 'Done') return { task, availability: 'Done', action: null };
            const action = exactAction(task, position, actions);
            if (action) return { task, availability: 'Ready', action };
            if (task.dependencies_satisfied === false) {
                return { task, availability: 'Dependency condition not satisfied', action: null };
            }
            if (task.dependencies_satisfied === true) {
                return { task, availability: 'Queued / not current', action: null };
            }
            return { task, availability: 'No completion action currently advertised', action: null };
        });
    }

    function taskCounts(tasks) {
        const rows = Array.isArray(tasks) ? tasks : [];
        const done = rows.filter((task) => task?.status === 'Done').length;
        return { total: rows.length, done, remaining: rows.length - done };
    }

    function createView() {
        return { stageId: null, sprintId: null, taskId: null, tab: 'details', filter: 'all', scopeKey: null };
    }

    function reconcileView(view, status) {
        const result = { ...createView(), ...(view || {}) };
        const tasks = Array.isArray(status?.tasks) ? status.tasks : (Array.isArray(status?.items) ? status.items : []);
        if (positive(result.taskId)) {
            result.taskAvailability = tasks.some((task) => task?.task_id === result.taskId)
                ? 'available' : 'unavailable';
        } else result.taskAvailability = 'none';
        return result;
    }

    function positive(value) { return Number.isInteger(value) && value > 0; }

    function retryScopeKey(currentRetry, sprintId) {
        if (currentRetry === null || currentRetry === undefined) return `sprint:${sprintId}`;
        const id = currentRetry?.retry_attempt_id;
        return positive(id) ? `retry:${id}:sprint:${sprintId}` : null;
    }

    function validDetail(payload, selection) {
        const data = payload?.data;
        const task = data?.task;
        return data?.project_id === selection.projectId
            && task?.task_id === selection.taskId
            && task?.sprint_id === selection.sprintId
            && retryScopeKey(data.current_retry, selection.sprintId) === selection.scopeKey;
    }

    function sameSubject(left, right) {
        return left?.projectId === right?.projectId
            && left?.sprintId === right?.sprintId
            && left?.taskId === right?.taskId
            && left?.scopeKey === right?.scopeKey;
    }

    function createController({ requestJson, onChange = () => {} } = {}) {
        if (typeof requestJson !== 'function') throw new TypeError('requestJson is required.');
        let selection = null;
        let generation = 0;
        let activeController = null;
        let kind = 'idle';
        let data = null;
        let error = null;
        let lastConfirmedAt = null;
        const notify = () => onChange(snapshot());
        const snapshot = () => ({ kind, data, error, selection: selection && { ...selection }, lastConfirmedAt });

        async function load(nextSelection, isRefresh) {
            if (!positive(nextSelection?.projectId) || !positive(nextSelection?.sprintId)
                || !positive(nextSelection?.taskId) || typeof nextSelection?.scopeKey !== 'string') {
                throw new TypeError('A positive project, Sprint, Task and scope selection is required.');
            }
            const requestGeneration = ++generation;
            const retainsConfirmedSubject = sameSubject(selection, nextSelection);
            activeController?.abort();
            activeController = new AbortController();
            selection = { ...nextSelection };
            if (!retainsConfirmedSubject) {
                data = null;
                lastConfirmedAt = null;
            }
            kind = 'loading';
            error = null;
            notify();
            const base = `/api/projects/${selection.projectId}/sprints/${selection.sprintId}/tasks/${selection.taskId}`;
            try {
                const [detail, execution] = await Promise.all([
                    requestJson(base, { signal: activeController.signal }),
                    requestJson(`${base}/execution`, { signal: activeController.signal }),
                ]);
                if (requestGeneration !== generation) return snapshot();
                if (!validDetail(detail, selection) || !validDetail(execution, selection)) {
                    data = null;
                    kind = 'error';
                    error = 'The Task detail response identity did not match the selected Project, Sprint, Task, and retry scope.';
                    notify();
                    return snapshot();
                }
                data = { ...detail.data, execution: execution.data };
                kind = 'ready';
                error = null;
                lastConfirmedAt = new Date().toISOString();
            } catch (caught) {
                if (requestGeneration !== generation || caught?.name === 'AbortError') return snapshot();
                if (caught?.status === 404) {
                    data = null;
                    kind = 'unavailable';
                    error = caught.message;
                } else if (retainsConfirmedSubject && data) {
                    kind = 'stale';
                    error = caught?.message || 'Manual refresh failed.';
                } else {
                    kind = 'error';
                    error = caught?.message || 'Task detail could not be loaded.';
                }
            }
            notify();
            return snapshot();
        }
        return {
            select(nextSelection) { return load(nextSelection, false); },
            refresh() { return selection ? load(selection, true) : Promise.resolve(snapshot()); },
            snapshot,
            unavailable(message = 'Task is unavailable in the retained Sprint scope.') {
                generation += 1;
                activeController?.abort();
                activeController = null;
                data = null;
                lastConfirmedAt = null;
                kind = 'unavailable';
                error = message;
                notify();
                return snapshot();
            },
            dispose() { generation += 1; activeController?.abort(); activeController = null; },
        };
    }

    function mapMarkup({ position = {}, view = createView(), lastConfirmedAt = null } = {}) {
        const current = new Set(currentStageIds(position));
        const cards = stages().map((stage) => {
            const viewing = view.stageId === stage.id;
            const here = current.has(stage.id);
            return `<button type="button" class="workspace-stage${viewing ? ' is-viewing' : ''}${here ? ' is-current' : ''}" id="workspace-stage-${stage.id}" data-workspace-stage="${stage.id}"${here ? ' aria-current="step"' : ''} aria-label="Stage ${String(stage.id).padStart(2, '0')}: ${escapeText(stage.label)}${here ? ', You are here' : ''}${viewing ? ', Viewing' : ''}"><span class="workspace-stage-number">${String(stage.id).padStart(2, '0')}</span><span class="workspace-stage-label">${escapeText(stage.label)}</span>${here ? '<span class="workspace-here">You are here</span>' : ''}${viewing ? '<span class="workspace-viewing">Viewing</span>' : ''}</button>`;
        });
        const returnMarkup = current.size && !current.has(view.stageId)
            ? '<button type="button" class="workspace-return-current" data-workspace-return-current="true">Return to current work</button>'
            : '';
        const freshness = lastConfirmedAt
            ? `Last confirmed ${escapeText(lastConfirmedAt)}; manual refresh required`
            : 'Loading lifecycle; manual refresh required';
        return `<section class="lifecycle-workspace" aria-label="Project lifecycle workspace"><section class="workspace-stage-group" aria-label="Project framing"><p class="workspace-group-label">Project framing · 01–06</p><div class="workspace-map" role="list">${cards.slice(0, 6).join('')}</div></section><section class="workspace-stage-group" aria-label="Sprint delivery"><p class="workspace-group-label">Sprint delivery · 07–13</p><div class="workspace-map" role="list">${cards.slice(6).join('')}</div></section>${returnMarkup}<p class="workspace-freshness">${freshness}</p></section>`;
    }

    function mount(host, bridge = {}) {
        if (!host || typeof host.innerHTML !== 'string') return;
        const view = bridge.view || createView();
        host.innerHTML = mapMarkup({ position: bridge.position, view, lastConfirmedAt: bridge.lastConfirmedAt });
        const buttons = Array.from(host.querySelectorAll('[data-workspace-stage]'));
        buttons.forEach((button, index) => {
            button.addEventListener('click', () => {
                const stageId = Number(button.dataset.workspaceStage);
                bridge.onStageSelect?.(stageId);
            });
            button.addEventListener('keydown', (event) => {
                const nextIndex = event.key === 'ArrowRight' ? index + 1
                    : event.key === 'ArrowLeft' ? index - 1
                        : event.key === 'Home' ? 0
                            : event.key === 'End' ? buttons.length - 1 : null;
                if (nextIndex === null) return;
                event.preventDefault();
                const target = buttons[(nextIndex + buttons.length) % buttons.length];
                target.focus();
                bridge.onStageSelect?.(Number(target.dataset.workspaceStage));
            });
        });
        host.querySelector('[data-workspace-return-current]')?.addEventListener('click', () => {
            const current = currentStageIds(bridge.position);
            if (current.length) bridge.onReturnToCurrent?.(current[0]);
        });
    }

    function escapeText(value) {
        return String(value ?? '').replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
    }

    return Object.freeze({ stages, stageForDecision, currentStageIds, taskRows, taskCounts, createView, reconcileView, createController, mapMarkup, mount });
})();

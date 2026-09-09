import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const sourcePath = path.resolve(import.meta.dirname, '../frontend/project.js');
const source = fs.readFileSync(sourcePath, 'utf8');
const fingerprint = (character) => `sha256:${character.repeat(64)}`;

function loadFrontend() {
    const createElement = () => ({
        _textContent: '',
        innerHTML: '',
        set textContent(value) {
            this._textContent = String(value ?? '');
            this.innerHTML = this._textContent
                .replaceAll('&', '&amp;')
                .replaceAll('<', '&lt;')
                .replaceAll('>', '&gt;')
                .replaceAll('"', '&quot;');
        },
        get textContent() { return this._textContent; },
    });
    const context = vm.createContext({
        AbortController,
        console,
        crypto: { randomUUID: () => 'retry-ui-uuid', subtle: webcrypto.subtle },
        document: {
            createElement,
            getElementById() { return null; },
            querySelector() { return null; },
            querySelectorAll() { return []; },
            addEventListener() {},
        },
        fetch: async () => ({ ok: true, text: async () => '{}' }),
        TextEncoder,
        URLSearchParams,
        window: { addEventListener() {}, location: { href: '' }, setTimeout() {} },
    });
    vm.runInContext(source, context, { filename: sourcePath });
    return context;
}

function completedSprintRetryState() {
    const sprintId = 31;
    const retryAction = {
        node_id: 'execution.sprint.retry',
        instance_key: `sprint:${sprintId}`,
        request_kind: 'retry_sprint',
        endpoint: 'sprint/retry',
        transport: 'semantic',
    };
    return {
        position: {
            decisions: [{
                ...retryAction,
                category: 'available',
                recommendation_kind: 'optional_reentry',
                reason_code: 'SPRINT_RETRY_AVAILABLE',
                decision_fingerprint: fingerprint('a'),
                fact_references: [{
                    fact_type: 'sprint_retry_target',
                    fact_id: String(sprintId),
                    fingerprint: fingerprint('b'),
                }],
            }],
        },
        actions: [retryAction],
        status: {
            project_id: 7,
            sprint: { sprint_id: sprintId, status: 'completed', completed_at: '2026-09-09T10:00:00Z' },
            accepted_plan: {
                sprint_id: sprintId,
                status: 'completed',
                goal: 'Re-execute the accepted Sprint scope.',
                owner: {
                    kind: 'solo_project',
                    key: 'agileforge:sprint-owner:solo-project:v1:project:7',
                    label: '[agileforge:sprint-owner:solo-project:v1:project:7] Solo operator for Retry Project',
                    display_label: 'Solo operator for Retry Project',
                },
                sprint_plan_artifact_id: 41,
                sprint_plan_artifact_decision_id: 51,
                plan_fingerprint: fingerprint('c'),
                candidate_set_fingerprint: fingerprint('d'),
                task_content_fingerprint: fingerprint('e'),
                acceptance: {
                    rationale: 'The source Sprint was accepted.',
                    reviewer: 'operator@example.com',
                    decided_at: '2026-09-09T09:00:00Z',
                },
                selected_stories: [{
                    story_id: 101,
                    story_item_id: 'US-0101',
                    title: 'Keep retry selection exact.',
                    story_points: 3,
                    task_count: 1,
                }],
                total_points: 3,
                task_count: 1,
            },
            original_start: {
                start_id: 61,
                sprint_id: sprintId,
                sprint_plan_artifact_id: 41,
                sprint_plan_artifact_decision_id: 51,
                plan_fingerprint: fingerprint('c'),
                candidate_set_fingerprint: fingerprint('d'),
                task_content_fingerprint: fingerprint('e'),
            },
            start: {
                start_id: 61,
                sprint_id: sprintId,
                sprint_plan_artifact_id: 41,
                sprint_plan_artifact_decision_id: 51,
                plan_fingerprint: fingerprint('c'),
                candidate_set_fingerprint: fingerprint('d'),
                task_content_fingerprint: fingerprint('e'),
            },
            tasks: [{
                task_id: 71,
                sprint_id: sprintId,
                story_id: 101,
                description: 'Preserve the original completion history.',
                status: 'Done',
                fact_fingerprint: fingerprint('f'),
                instance_key: 'task:71',
            }],
            stories: [{
                story_id: 101,
                status: 'Done',
                sprint_ids: [sprintId],
                source_story_item_id: 'US-0101',
                instance_key: 'story:101',
            }],
            current_retry: null,
            effective_status: 'completed',
            review: { review_id: 81, status: 'completed' },
            closure: { closure_id: 91, status: 'completed' },
        },
    };
}

test('completed Sprint exposes only its exact scoped retry action before preview', () => {
    const context = loadFrontend();
    const state = completedSprintRetryState();

    const markup = context.sprintStatusMarkup(
        { kind: 'ready', data: state.status },
        state.position,
        state.actions,
    );

    assert.match(markup, /data-direct-action="retry_sprint"/);
    assert.match(markup, /Sprint #31 is complete/);
    assert.match(markup, /Retry Sprint/);
    assert.doesNotMatch(markup, /target branch/i);
});

function response(data, status = 200) {
    return {
        ok: status >= 200 && status < 300,
        status,
        text: async () => JSON.stringify(data),
    };
}

function element(textContent = '') {
    const attributes = new Map();
    const listeners = {};
    const classes = new Set();
    return {
        textContent,
        innerHTML: '',
        value: '',
        disabled: false,
        dataset: {},
        style: {},
        classList: {
            toggle(name, force) {
                if (force === undefined ? !classes.has(name) : force) classes.add(name);
                else classes.delete(name);
            },
            add(...names) { names.forEach((name) => classes.add(name)); },
            remove(...names) { names.forEach((name) => classes.delete(name)); },
            contains(name) { return classes.has(name); },
        },
        setAttribute(name, value) { attributes.set(name, String(value)); },
        removeAttribute(name) { attributes.delete(name); },
        getAttribute(name) { return attributes.get(name) ?? null; },
        addEventListener(type, listener) { (listeners[type] ??= []).push(listener); },
        dispatch(type) { for (const listener of listeners[type] ?? []) listener({ currentTarget: this }); },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        closest(selector) { return selector === 'button' ? this : null; },
        focus() {},
    };
}

function retryPreview(overrides = {}) {
    return {
        project_id: 7,
        sprint_id: 31,
        predecessor_retry_attempt_id: null,
        next_ordinal: 2,
        story_ids: [101],
        task_ids: [71],
        preserved_history: 'Original execution evidence remains immutable.',
        repository_provenance: null,
        blockers: [],
        expected_state_fingerprint: fingerprint('9'),
        ...overrides,
    };
}

function retryHarness({
    previews = [retryPreview()],
    retryResponse = null,
    state: suppliedState = null,
    buttonAction = null,
    startResponse = null,
    afterStartState = null,
    afterRetryState = null,
    previewResponse = null,
} = {}) {
    const state = suppliedState ?? completedSprintRetryState();
    const action = buttonAction ?? state.actions[0];
    const directAction = action.request_kind === 'start_sprint_retry'
        ? 'start_sprint'
        : action.request_kind;
    const requests = [];
    const documentListeners = {};
    const elements = {
        'cockpit-primary-action-btn': element(),
        'cockpit-primary-action-label': element('Execute Stage Action'),
        'cockpit-action-stage-chip': element('Available'),
        'cockpit-action-description': element(),
        'project-error': element(),
        'human-action-kicker': element(),
        'human-action-title': element(),
        'human-action-description': element(),
        'human-action-retry-details': element(),
        'human-action-rationale-group': element(),
        'human-action-rationale': element(),
        'human-action-rationale-label': element(),
        'human-action-path-group': element(),
        'human-action-path': element(),
        'human-action-submit': element(),
        'human-action-error': element(),
        'human-action-dialog': element(),
    };
    const dialog = elements['human-action-dialog'];
    dialog.open = false;
    dialog.showModal = () => { dialog.open = true; };
    dialog.close = () => { dialog.open = false; dialog.dispatch('close'); };
    let retryButton = null;
    let retryStarted = false;
    let retryCreated = false;
    const fetch = async (url, options = {}) => {
        const method = options.method ?? 'GET';
        const request = {
            url: String(url),
            method,
            body: options.body ? JSON.parse(options.body) : null,
            headers: options.headers ?? {},
        };
        requests.push(request);
        if (request.url.endsWith('/sprint/31/retry-preview')) {
            return previewResponse
                ? await previewResponse(request)
                : response({ data: previews.shift() ?? retryPreview() });
        }
        if (request.url.endsWith('/sprint/retry') && method === 'POST') {
            retryCreated = true;
            return retryResponse ? await retryResponse(request) : response({
                data: { output: { retry_attempt_id: 101, status: 'Planned' } },
            });
        }
        if (request.url.endsWith('/sprint/start') && method === 'POST') {
            retryStarted = true;
            return startResponse ? await startResponse(request) : response({ data: {} });
        }
        const dashboardState = retryStarted && afterStartState
            ? afterStartState
            : (retryCreated && afterRetryState ? afterRetryState : state);
        if (request.url.endsWith('/position')) {
            return response({ data: dashboardState.position, actions: dashboardState.actions });
        }
        if (request.url.endsWith('/sprint/status')) {
            return response({ data: dashboardState.status });
        }
        if (request.url.endsWith('/sprints')) {
            return response({ data: dashboardState.history ?? { project_id: 7, execution_attempts: [] } });
        }
        if (request.url.endsWith('/projects/7')) {
            return response({ data: { id: 7, name: 'Retry Project' } });
        }
        return response({ data: {}, actions: [] });
    };
    const context = vm.createContext({
        AbortController,
        console,
        crypto: { randomUUID: () => 'retry-confirmation-uuid', subtle: webcrypto.subtle },
        document: {
            addEventListener(type, listener) { (documentListeners[type] ??= []).push(listener); },
            getElementById(id) { return elements[id] ?? null; },
            querySelector(selector) {
                return retryButton && selector === `button[data-direct-action="${directAction}"]`
                    ? retryButton
                    : null;
            },
            querySelectorAll() { return []; },
            createElement() {
                let text = '';
                return {
                    set textContent(value) { text = String(value ?? ''); },
                    get textContent() { return text; },
                    get innerHTML() {
                        return text.replaceAll('&', '&amp;').replaceAll('<', '&lt;')
                            .replaceAll('>', '&gt;');
                    },
                };
            },
        },
        fetch,
        TextEncoder,
        URLSearchParams,
        window: { addEventListener() {}, location: { href: '' }, setTimeout() {} },
    });
    vm.runInContext(source, context, { filename: sourcePath });
    vm.runInContext(`
        selectedProjectId = 7;
        lifecycleState.position = ${JSON.stringify(state.position)};
        lifecycleState.actions = ${JSON.stringify(state.actions)};
        lifecycleState.sprintStatus = { kind: 'ready', data: ${JSON.stringify(state.status)} };
    `, context);
    context.installInteractions();
    retryButton = element();
    retryButton.dataset = {
        directAction,
        sprintRetryTarget: '31',
        deliveryActionNode: action.node_id,
        deliveryActionInstance: action.instance_key,
        deliveryActionHasInstance: 'true',
        deliveryActionEndpoint: action.endpoint,
        deliveryActionTransport: action.transport,
    };
    return {
        context,
        dialog,
        elements,
        requests,
        retryButton,
        async open() {
            await Promise.all((documentListeners.click ?? []).map((listener) => listener({ target: retryButton })));
            await new Promise((resolve) => setImmediate(resolve));
            await new Promise((resolve) => setImmediate(resolve));
        },
        retryPosts() {
            return requests.filter((request) => request.method === 'POST'
                && request.url.endsWith('/sprint/retry'));
        },
        previewGets() {
            return requests.filter((request) => request.method === 'GET'
                && request.url.endsWith('/sprint/31/retry-preview'));
        },
    };
}

function plannedRetryStartState() {
    const state = completedSprintRetryState();
    state.status.current_retry = {
        retry_attempt_id: 101,
        ordinal: 2,
        status: 'planned',
        predecessor_retry_attempt_id: null,
        sprint_instance_key: 'retry:101:sprint:31',
    };
    state.status.effective_status = 'planned';
    state.status.start = null;
    state.status.tasks[0] = {
        ...state.status.tasks[0],
        status: 'To Do',
        instance_key: 'retry:101:task:71',
    };
    state.status.stories[0] = {
        ...state.status.stories[0],
        status: 'To Do',
        instance_key: 'retry:101:story:101',
    };
    state.position.decisions = [{
        node_id: 'execution.sprint.retry.start',
        instance_key: 'retry:101:sprint:31',
        request_kind: 'start_sprint_retry',
        category: 'available',
        recommendation_kind: 'required',
        reason_code: 'SPRINT_RETRY_READY_TO_START',
        decision_fingerprint: fingerprint('4'),
        fact_references: [{
            fact_type: 'sprint_retry',
            fact_id: '101',
            fingerprint: fingerprint('3'),
        }],
    }];
    state.actions = [{
        node_id: 'execution.sprint.retry.start',
        instance_key: 'retry:101:sprint:31',
        request_kind: 'start_sprint_retry',
        endpoint: 'sprint/start',
        transport: 'semantic',
    }];
    return state;
}

function activeRetryState() {
    const state = plannedRetryStartState();
    state.status.current_retry = {
        ...state.status.current_retry,
        status: 'active',
    };
    state.status.effective_status = 'active';
    state.status.start = {
        start_id: 102,
        retry_attempt_id: 101,
        contract_fingerprint: fingerprint('7'),
        decision_fingerprint: fingerprint('4'),
        started_by: 'dashboard-ui',
        started_at: '2026-09-09T10:30:00Z',
    };
    state.status.stories[0] = {
        ...state.status.stories[0],
        status: 'To Do',
        instance_key: 'retry:101:story:101',
    };
    state.position.decisions = [{
        node_id: 'execution.task.complete',
        instance_key: 'retry:101:task:71',
        request_kind: 'complete_task',
        category: 'available',
        recommendation_kind: 'required',
        reason_code: 'NEXT_TASK_READY',
        decision_fingerprint: fingerprint('5'),
        fact_references: [{ fact_type: 'task', fact_id: '71', fingerprint: fingerprint('f') }],
    }];
    state.actions = [{
        node_id: 'execution.task.complete',
        instance_key: 'retry:101:task:71',
        request_kind: 'complete_task',
        endpoint: 'sprint/task/complete',
        transport: 'semantic',
    }];
    state.history = {
        project_id: 7,
        execution_attempts: [{
            sprint_id: 31,
            retry_attempt_id: null,
            ordinal: 1,
            status: 'completed',
            predecessor_retry_attempt_id: null,
            sprint_instance_key: 'sprint:31',
            task_instance_keys: ['task:71'],
            start: state.status.original_start,
            task_completions: [{ task_id: 71, status: 'Done' }],
            story_completions: [],
            review: { review_id: 81 },
            closure: { closure_id: 91 },
            triage: [],
        }, {
            sprint_id: 31,
            retry_attempt_id: 101,
            ordinal: 2,
            status: 'active',
            predecessor_retry_attempt_id: null,
            sprint_instance_key: 'retry:101:sprint:31',
            task_instance_keys: ['retry:101:task:71'],
            start: state.status.start,
            task_completions: [],
            story_completions: [],
            review: null,
            closure: null,
            triage: [],
        }],
    };
    return state;
}

function completedRetryState() {
    const state = activeRetryState();
    state.status.current_retry = {
        ...state.status.current_retry,
        status: 'completed',
    };
    state.status.effective_status = 'completed';
    state.status.tasks[0] = {
        ...state.status.tasks[0],
        status: 'Done',
    };
    state.status.stories[0] = {
        ...state.status.stories[0],
        status: 'Done',
    };
    state.position.decisions = [];
    state.actions = [];
    state.history.execution_attempts[1] = {
        ...state.history.execution_attempts[1],
        status: 'completed',
        task_completions: [{ task_id: 71, status: 'Done' }],
        story_completions: [{ story_id: 101, status: 'Done' }],
    };
    return state;
}

test('opening the exact retry control previews by GET before any retry mutation', async () => {
    const harness = retryHarness();

    await harness.open();

    assert.equal(harness.previewGets().length, 1);
    assert.equal(harness.retryPosts().length, 0);
    assert.equal(harness.dialog.open, true);
    assert.match(harness.elements['human-action-description'].textContent, /Sprint #31/);
});

test('a blocked preview explains its blocker and exposes no confirmation', async () => {
    const harness = retryHarness({
        previews: [retryPreview({
            blockers: [{
                code: 'RETRY_ALREADY_LIVE',
                reason: 'A retry attempt is already active.',
                subject_type: 'retry_attempt',
                subject_id: 101,
            }],
        })],
    });

    await harness.open();

    assert.equal(harness.previewGets().length, 1);
    assert.equal(harness.retryPosts().length, 0);
    assert.match(harness.elements['human-action-retry-details'].innerHTML, /already active/);
    assert.equal(harness.elements['human-action-submit'].disabled, true);
});

test('a retry preview exposes every exact scope identifier and structured blocker subject', async () => {
    const harness = retryHarness({
        previews: [retryPreview({
            story_ids: [101, 102],
            task_ids: [71, 72],
            blockers: [{
                code: 'RETRY_ALREADY_LIVE',
                reason: 'A retry attempt is already active.',
                subject_type: 'retry_attempt',
                subject_id: 101,
            }, {
                code: 'RETRY_PROVENANCE_MISSING',
                reason: 'No prior retry provenance was recorded.',
                subject_type: 'repository_provenance',
                subject_id: null,
            }],
        })],
    });

    await harness.open();

    const details = harness.elements['human-action-retry-details'].innerHTML;
    assert.match(details, /Story #101/);
    assert.match(details, /Story #102/);
    assert.match(details, /Task #71/);
    assert.match(details, /Task #72/);
    assert.match(details, /RETRY_ALREADY_LIVE/);
    assert.match(details, /retry_attempt #101/);
    assert.match(details, /RETRY_PROVENANCE_MISSING/);
    assert.match(details, /repository_provenance/);
    assert.match(details, /no subject ID/);
    assert.equal(harness.retryPosts().length, 0);
});

test('retry preview separates its exact scope and every blocker into accessible rows', async () => {
    const harness = retryHarness({
        previews: [retryPreview({
        story_ids: [101, 102],
        task_ids: [71, 72],
        blockers: [{
            code: 'RETRY_ALREADY_LIVE',
            reason: 'A retry attempt is already active.',
            subject_type: 'retry_attempt',
            subject_id: 101,
        }, {
            code: 'RETRY_PROVENANCE_MISSING',
            reason: 'No prior retry provenance was recorded.',
            subject_type: 'repository_provenance',
            subject_id: null,
        }],
        })],
    });

    await harness.open();

    const markup = harness.elements['human-action-retry-details'].innerHTML;
    assert.match(markup, /<ul[^>]+aria-label="Retry scope"/);
    assert.match(markup, /<li[^>]*>Story #101<\/li>/);
    assert.match(markup, /<li[^>]*>Story #102<\/li>/);
    assert.match(markup, /<li[^>]*>Task #71<\/li>/);
    assert.match(markup, /<li[^>]*>Task #72<\/li>/);
    assert.match(markup, /<ul[^>]+aria-label="Retry blockers"/);
    assert.match(markup, /data-sprint-retry-blocker="true"/);
    assert.equal((markup.match(/data-sprint-retry-blocker=/g) ?? []).length, 2);
    assert.equal(
        harness.elements['human-action-retry-details'].classList.contains('hidden'),
        false,
    );
    harness.context.openHumanDialog({
        kind: 'goal-outcome',
        title: 'A different dialog',
        description: 'The retry details must not carry over.',
        field: 'none',
        required: false,
        hideRationale: true,
    });
    assert.equal(harness.elements['human-action-retry-details'].innerHTML, '');
    assert.equal(
        harness.elements['human-action-retry-details'].classList.contains('hidden'),
        true,
    );
});

test('retry preview details escape malicious blocker fields', async () => {
    const harness = retryHarness({
        previews: [retryPreview({
            blockers: [{
                code: 'x" data-injected="true <script>alert(1)</script>',
                reason: '<img src=x onerror=alert(1)>',
                subject_type: '<subject>',
                subject_id: null,
            }],
        })],
    });

    await harness.open();

    const markup = harness.elements['human-action-retry-details'].innerHTML;
    assert.match(markup, /x" data-injected="true &lt;script&gt;alert\(1\)&lt;\/script&gt;/);
    assert.match(markup, /&lt;img src=x onerror=alert\(1\)&gt;/);
    assert.match(markup, /&lt;subject&gt;/);
    assert.doesNotMatch(markup, /<script>|<img /);
});

test('a preview that arrives after Project selection changes cannot revive the stale retry dialog', async () => {
    let resolvePreview;
    const harness = retryHarness({
        previewResponse: () => new Promise((resolve) => { resolvePreview = resolve; }),
    });

    const opening = harness.open();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(harness.previewGets().length, 1);
    vm.runInContext('selectedProjectId = 8; lifecycleState.actions = [];', harness.context);
    resolvePreview(response({ data: retryPreview() }));
    await opening;
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(harness.previewGets().length, 1);
    assert.equal(harness.retryPosts().length, 0);
    assert.equal(harness.dialog.open, false);
});

test('a wrong-owner retry preview cannot open confirmation or submit', async () => {
    const harness = retryHarness({
        previewResponse: async () => response({ data: retryPreview({ project_id: 8 }) }),
    });

    await harness.open();

    assert.equal(harness.previewGets().length, 1);
    assert.equal(harness.retryPosts().length, 0);
    assert.equal(harness.dialog.open, false);
});

test('cancelling a previewed retry sends no POST', async () => {
    const harness = retryHarness();

    await harness.open();
    harness.context.closeHumanDialog();

    assert.equal(harness.previewGets().length, 1);
    assert.equal(harness.retryPosts().length, 0);
    assert.equal(harness.dialog.open, false);
});

test('confirming a previewed retry posts the captured target and fingerprint once', async () => {
    const preview = retryPreview();
    const harness = retryHarness({ previews: [preview], afterRetryState: plannedRetryStartState() });

    await harness.open();
    harness.elements['human-action-rationale'].value = 'Re-execute the approved work';
    await harness.context.submitHumanAction();

    const retryPosts = harness.retryPosts();
    assert.equal(retryPosts.length, 1);
    assert.equal(retryPosts[0].body.sprint_id, 31);
    assert.equal(retryPosts[0].body.expected_state_fingerprint, preview.expected_state_fingerprint);
    assert.equal(retryPosts[0].body.confirm, true);
    assert.equal(retryPosts[0].body.rationale, 'Re-execute the approved work');
    assert.equal(retryPosts[0].body.actor, 'dashboard-ui');
    assert.equal(typeof retryPosts[0].body.idempotency_key, 'string');
    assert.notEqual(retryPosts[0].body.idempotency_key, '');
});

test('a stale retry confirmation refreshes the preview and never resends automatically', async () => {
    const firstPreview = retryPreview({ expected_state_fingerprint: fingerprint('9') });
    const refreshedPreview = retryPreview({ expected_state_fingerprint: fingerprint('8') });
    const harness = retryHarness({
        previews: [firstPreview, refreshedPreview],
        retryResponse: async () => response({
            detail: {
                errors: [{
                    code: 'WORKFLOW_FACT_CONFLICT',
                    message: 'The retry preview is stale.',
                }],
            },
        }, 409),
    });

    await harness.open();
    harness.elements['human-action-rationale'].value = 'Re-execute the approved work';
    await harness.context.submitHumanAction();
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(harness.retryPosts().length, 1);
    assert.equal(harness.previewGets().length, 2);
    assert.equal(harness.dialog.open, true);
    assert.equal(harness.elements['human-action-rationale'].value, '');
    assert.match(harness.elements['human-action-description'].textContent, /Sprint #31/);
});

test('a pending retry submission ignores duplicate confirmation', async () => {
    let resolveRetry;
    const harness = retryHarness({
        retryResponse: () => new Promise((resolve) => { resolveRetry = resolve; }),
        afterRetryState: plannedRetryStartState(),
    });

    await harness.open();
    assert.equal(harness.previewGets().length, 1);
    assert.equal(harness.retryPosts().length, 0);
    assert.equal(harness.dialog.open, true);
    harness.elements['human-action-rationale'].value = 'Re-execute the approved work';
    const first = harness.context.submitHumanAction();
    const second = harness.context.submitHumanAction();
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(harness.retryPosts().length, 1);
    resolveRetry(response({ data: { output: { retry_attempt_id: 101, status: 'Planned' } } }));
    await first;
    await second;
});

test('an old retry response cannot close a dialog opened for a newly selected Project', async () => {
    let resolveRetry;
    const harness = retryHarness({
        retryResponse: () => new Promise((resolve) => { resolveRetry = resolve; }),
        afterRetryState: plannedRetryStartState(),
    });

    await harness.open();
    harness.elements['human-action-rationale'].value = 'Re-execute the approved work';
    const submission = harness.context.submitHumanAction();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(harness.retryPosts().length, 1);
    vm.runInContext(`
        selectedProjectId = 8;
        openHumanDialog({
            kind: 'goal-outcome',
            title: 'Project 8 decision',
            description: 'Keep this newer dialog open.',
            field: 'none',
            required: false,
            hideRationale: true,
        });
    `, harness.context);
    resolveRetry(response({ data: { output: { retry_attempt_id: 101, status: 'Planned' } } }));
    await submission;

    assert.equal(harness.dialog.open, true);
    assert.equal(harness.elements['human-action-title'].textContent, 'Project 8 decision');
});

test('a successful retry POST remains locked until reload confirms its exact created attempt', async () => {
    const harness = retryHarness();

    await harness.open();
    harness.elements['human-action-rationale'].value = 'Re-execute the approved work';
    await assert.rejects(
        harness.context.submitHumanAction(),
        /Controls remain locked/,
    );

    assert.equal(harness.retryPosts().length, 1);
    assert.equal(vm.runInContext('activeDeliveryUnreconciled', harness.context), true);
});

test('retry creation accepts its exact authoritative attempt after forward progress to active', async () => {
    const active = activeRetryState();
    const harness = retryHarness({ afterRetryState: active });

    await harness.open();
    harness.elements['human-action-rationale'].value = 'Re-execute the approved work';
    await harness.context.submitHumanAction();

    assert.equal(harness.retryPosts().length, 1);
    assert.equal(vm.runInContext('activeDeliveryUnreconciled', harness.context), false);
    assert.equal(vm.runInContext('activeSprintRetryMutation', harness.context), null);
    assert.equal(
        vm.runInContext('lifecycleState.sprintStatus.data.current_retry.retry_attempt_id', harness.context),
        101,
    );
    assert.equal(
        vm.runInContext('lifecycleState.sprintStatus.data.effective_status', harness.context),
        'active',
    );
});

test('dismissing a pending retry confirmation cannot dispatch another dashboard action', async () => {
    let resolveRetry;
    const harness = retryHarness({
        retryResponse: () => new Promise((resolve) => { resolveRetry = resolve; }),
    });

    await harness.open();
    harness.elements['human-action-rationale'].value = 'Re-execute the approved work';
    const retrySubmission = harness.context.submitHumanAction();
    await new Promise((resolve) => setImmediate(resolve));
    harness.context.closeHumanDialog();
    const competingButton = element();
    const competing = harness.context.runDirectAction(
        'refresh_repository_binding',
        competingButton,
        'repository/refresh',
    );
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(harness.requests.filter((request) => request.method === 'POST').length, 1);
    resolveRetry(response({ data: { output: { retry_attempt_id: 101, status: 'Planned' } } }));
    await retrySubmission;
    assert.equal(await competing, false);
});

test('current retry progress and preserved original history render', () => {
    const context = loadFrontend();
    const state = completedSprintRetryState();
    state.status.current_retry = {
        retry_attempt_id: 101,
        ordinal: 2,
        status: 'active',
        predecessor_retry_attempt_id: null,
        sprint_instance_key: 'retry:101:sprint:31',
    };
    state.status.effective_status = 'active';
    state.status.start = {
        start_id: 102,
        retry_attempt_id: 101,
        contract_fingerprint: fingerprint('7'),
        decision_fingerprint: fingerprint('6'),
        started_by: 'dashboard-ui',
        started_at: '2026-09-09T10:30:00Z',
    };
    state.status.tasks[0] = {
        ...state.status.tasks[0],
        status: 'To Do',
        instance_key: 'retry:101:task:71',
    };
    state.position.decisions = [{
        node_id: 'execution.task.complete',
        instance_key: 'retry:101:task:71',
        request_kind: 'complete_task',
        category: 'available',
        recommendation_kind: 'required',
        reason_code: 'NEXT_TASK_READY',
        decision_fingerprint: fingerprint('5'),
        fact_references: [{ fact_type: 'task', fact_id: '71', fingerprint: fingerprint('f') }],
    }];
    state.actions = [{
        node_id: 'execution.task.complete',
        instance_key: 'retry:101:task:71',
        request_kind: 'complete_task',
        endpoint: 'sprint/task/complete',
        transport: 'semantic',
    }];
    const history = {
        project_id: 7,
        execution_attempts: [{
            sprint_id: 31,
            retry_attempt_id: null,
            ordinal: 1,
            status: 'completed',
            predecessor_retry_attempt_id: null,
            sprint_instance_key: 'sprint:31',
            task_instance_keys: ['task:71'],
            start: state.status.original_start,
            task_completions: [{ task_id: 71, status: 'Done' }],
            story_completions: [{ story_id: 101, status: 'Done' }],
            review: { review_id: 81 },
            closure: { closure_id: 91 },
            triage: [],
        }, {
            sprint_id: 31,
            retry_attempt_id: 101,
            ordinal: 2,
            status: 'active',
            predecessor_retry_attempt_id: null,
            sprint_instance_key: 'retry:101:sprint:31',
            task_instance_keys: ['retry:101:task:71'],
            start: state.status.start,
            task_completions: [],
            story_completions: [],
            review: null,
            closure: null,
            triage: [],
        }],
    };

    const markup = context.sprintStatusMarkup(
        { kind: 'ready', data: state.status },
        state.position,
        state.actions,
        { sprintHistory: history },
    );

    assert.match(markup, /Attempt 2/);
    assert.match(markup, /Task #71/);
    assert.match(markup, /To Do/);
    assert.match(markup, /Execution attempt history/);
    assert.match(markup, /Attempt 1/);
    assert.match(markup, /Story #101/);
    assert.match(markup, /To Do/);
    const execution = context.sprintExecutionProjection(
        state.status,
        state.position,
        state.actions,
    );
    assert.equal(execution.kind, 'ready');
    assert.equal(execution.items.length, 1);
    assert.equal(execution.items[0].task.instance_key, 'retry:101:task:71');
    assert.equal(execution.items[0].decision.instance_key, 'retry:101:task:71');
    assert.equal(execution.items[0].action.instance_key, 'retry:101:task:71');
});

test('effective retry progress keeps terminal Tasks and Stories visible without actions', () => {
    const context = loadFrontend();
    const state = completedRetryState();

    const markup = context.sprintStatusMarkup(
        { kind: 'ready', data: state.status },
        state.position,
        state.actions,
        { sprintHistory: state.history },
    );

    assert.match(markup, /Attempt 2 is completed/);
    assert.match(markup, /Task #71/);
    assert.match(markup, /Story #101/);
    assert.match(markup, /Done/);
    assert.match(markup, /Execution attempt history/);
    assert.match(markup, /Attempt 1/);
});

test('execution history rejects wrong-owner and malformed retry identities', () => {
    const context = loadFrontend();
    const state = activeRetryState();
    const wrongOwner = { ...state.history, project_id: 8 };
    const malformedRetry = {
        ...state.history,
        execution_attempts: state.history.execution_attempts.map((attempt) => (
            attempt.retry_attempt_id === 101
                ? { ...attempt, sprint_instance_key: 'retry:999:sprint:31' }
                : attempt
        )),
    };

    const wrongOwnerMarkup = context.sprintStatusMarkup(
        { kind: 'ready', data: state.status },
        state.position,
        state.actions,
        { sprintHistory: wrongOwner },
    );
    const malformedRetryMarkup = context.sprintStatusMarkup(
        { kind: 'ready', data: state.status },
        state.position,
        state.actions,
        { sprintHistory: malformedRetry },
    );

    assert.doesNotMatch(wrongOwnerMarkup, /data-sprint-execution-history/);
    assert.doesNotMatch(malformedRetryMarkup, /data-sprint-execution-history/);
});

test('execution history rejects duplicate retry lineage and wrong task bindings', () => {
    const context = loadFrontend();
    const duplicate = activeRetryState();
    duplicate.status.current_retry = {
        ...duplicate.status.current_retry,
        ordinal: 3,
        predecessor_retry_attempt_id: 101,
    };
    duplicate.history.execution_attempts[1] = {
        ...duplicate.history.execution_attempts[1],
        status: 'completed',
    };
    duplicate.history.execution_attempts.push({
        ...duplicate.history.execution_attempts[1],
        ordinal: 3,
        status: 'active',
        predecessor_retry_attempt_id: 101,
    });
    const selfPredecessor = activeRetryState();
    selfPredecessor.status.current_retry = {
        ...selfPredecessor.status.current_retry,
        predecessor_retry_attempt_id: 101,
    };
    selfPredecessor.history.execution_attempts[1] = {
        ...selfPredecessor.history.execution_attempts[1],
        predecessor_retry_attempt_id: 101,
    };
    const wrongTaskKey = activeRetryState();
    wrongTaskKey.history.execution_attempts[1] = {
        ...wrongTaskKey.history.execution_attempts[1],
        task_instance_keys: ['retry:101:task:72'],
    };

    for (const state of [duplicate, selfPredecessor, wrongTaskKey]) {
        const markup = context.sprintStatusMarkup(
            { kind: 'ready', data: state.status },
            state.position,
            state.actions,
            { sprintHistory: state.history },
        );
        assert.doesNotMatch(markup, /data-sprint-execution-history/);
    }
});

test('retry creation remains locked after a different or cross-Project reload', async () => {
    const different = activeRetryState();
    different.status.current_retry = {
        ...different.status.current_retry,
        retry_attempt_id: 102,
        sprint_instance_key: 'retry:102:sprint:31',
    };
    different.status.start = {
        ...different.status.start,
        retry_attempt_id: 102,
    };
    different.status.tasks[0] = {
        ...different.status.tasks[0],
        instance_key: 'retry:102:task:71',
    };
    different.status.stories[0] = {
        ...different.status.stories[0],
        instance_key: 'retry:102:story:101',
    };
    different.position.decisions[0] = {
        ...different.position.decisions[0],
        instance_key: 'retry:102:task:71',
    };
    different.actions[0] = {
        ...different.actions[0],
        instance_key: 'retry:102:task:71',
    };
    different.history.execution_attempts[1] = {
        ...different.history.execution_attempts[1],
        retry_attempt_id: 102,
        sprint_instance_key: 'retry:102:sprint:31',
        task_instance_keys: ['retry:102:task:71'],
    };
    const crossProject = activeRetryState();
    crossProject.status.project_id = 8;

    for (const afterRetryState of [different, crossProject]) {
        const harness = retryHarness({ afterRetryState });
        await harness.open();
        harness.elements['human-action-rationale'].value = 'Re-execute the approved work';
        await assert.rejects(
            harness.context.submitHumanAction(),
            /Controls remain locked/,
        );
        assert.equal(harness.retryPosts().length, 1);
        assert.equal(vm.runInContext('activeDeliveryUnreconciled', harness.context), true);
    }
});

test('planned retry uses its server-scoped start action without original start fields', () => {
    const context = loadFrontend();
    const state = completedSprintRetryState();
    state.status.current_retry = {
        retry_attempt_id: 101,
        ordinal: 2,
        status: 'planned',
        predecessor_retry_attempt_id: null,
        sprint_instance_key: 'retry:101:sprint:31',
    };
    state.status.effective_status = 'planned';
    state.status.start = null;
    state.status.tasks[0] = {
        ...state.status.tasks[0],
        status: 'To Do',
        instance_key: 'retry:101:task:71',
    };
    state.position.decisions = [{
        node_id: 'execution.sprint.retry.start',
        instance_key: 'retry:101:sprint:31',
        request_kind: 'start_sprint_retry',
        category: 'available',
        recommendation_kind: 'required',
        reason_code: 'SPRINT_RETRY_READY_TO_START',
        decision_fingerprint: fingerprint('4'),
        fact_references: [{
            fact_type: 'sprint_retry',
            fact_id: '101',
            fingerprint: fingerprint('3'),
        }],
    }];
    state.actions = [{
        node_id: 'execution.sprint.retry.start',
        instance_key: 'retry:101:sprint:31',
        request_kind: 'start_sprint_retry',
        endpoint: 'sprint/start',
        transport: 'semantic',
    }];

    const markup = context.sprintStatusMarkup(
        { kind: 'ready', data: state.status },
        state.position,
        state.actions,
    );

    assert.match(markup, /Attempt 2/);
    assert.match(markup, /data-direct-action="start_sprint"/);
    assert.match(markup, /data-delivery-action-node="execution\.sprint\.retry\.start"/);
    assert.match(markup, /data-delivery-action-instance="retry:101:sprint:31"/);
    assert.match(markup, /Start Sprint/);
    assert.doesNotMatch(markup, /sprint_plan_artifact_id/);
});

test('retry start confirmation posts the exact server-scoped key and graph decision', async () => {
    const state = plannedRetryStartState();
    const harness = retryHarness({
        state,
        buttonAction: state.actions[0],
        afterStartState: activeRetryState(),
        startResponse: async () => response({ data: {} }),
    });

    await harness.open();

    assert.equal(harness.dialog.open, true);
    assert.match(harness.elements['human-action-description'].textContent, /Attempt 2/);
    const submission = harness.context.submitHumanAction();
    await new Promise((resolve) => setImmediate(resolve));
    const startPosts = harness.requests.filter((request) => request.method === 'POST'
        && request.url.endsWith('/sprint/start'));
    assert.equal(startPosts.length, 1);
    assert.equal(startPosts[0].body.instance_key, 'retry:101:sprint:31');
    assert.equal(startPosts[0].body.actor, 'dashboard-ui');
    assert.equal(typeof startPosts[0].body.idempotency_key, 'string');
    assert.equal(startPosts[0].headers['X-AgileForge-Expected-Decision'], fingerprint('4'));
    assert.equal(Object.hasOwn(startPosts[0].body, 'sprint_id'), false);
    assert.equal(harness.previewGets().length, 0);
    await submission;
    assert.equal(harness.dialog.open, false);
});

test('retry start accepts its exact authoritative attempt after forward progress to completed', async () => {
    const state = plannedRetryStartState();
    const completed = completedRetryState();
    const harness = retryHarness({
        state,
        buttonAction: state.actions[0],
        afterStartState: completed,
        startResponse: async () => response({ data: {} }),
    });

    await harness.open();
    await harness.context.submitHumanAction();

    assert.equal(
        harness.requests.filter((request) => request.method === 'POST'
            && request.url.endsWith('/sprint/start')).length,
        1,
    );
    assert.equal(vm.runInContext('activeDeliveryUnreconciled', harness.context), false);
    assert.equal(vm.runInContext('activeSprintMutation', harness.context), null);
    assert.equal(
        vm.runInContext('lifecycleState.sprintStatus.data.current_retry.retry_attempt_id', harness.context),
        101,
    );
    assert.equal(
        vm.runInContext('lifecycleState.sprintStatus.data.effective_status', harness.context),
        'completed',
    );
});

test('retry start remains locked after a cross-Project reload', async () => {
    const state = plannedRetryStartState();
    const crossProject = activeRetryState();
    crossProject.status.project_id = 8;
    const harness = retryHarness({
        state,
        buttonAction: state.actions[0],
        afterStartState: crossProject,
        startResponse: async () => response({ data: {} }),
    });

    await harness.open();
    await assert.rejects(
        harness.context.submitHumanAction(),
        /Controls remain locked/,
    );

    assert.equal(
        harness.requests.filter((request) => request.method === 'POST'
            && request.url.endsWith('/sprint/start')).length,
        1,
    );
    assert.equal(vm.runInContext('activeDeliveryUnreconciled', harness.context), true);
});

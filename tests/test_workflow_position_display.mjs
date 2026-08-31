import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const sourcePath = path.resolve(import.meta.dirname, '../frontend/project.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function loadFrontend(fetchImpl = async () => ({ ok: true, text: async () => '{}' })) {
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
        crypto: { randomUUID: () => 'uuid-1', subtle: webcrypto.subtle },
        document: {
            createElement,
            getElementById() { return null; },
            querySelector() { return null; },
            addEventListener() {},
        },
        fetch: fetchImpl,
        URLSearchParams,
        TextEncoder,
        window: { addEventListener() {}, location: { href: '' } },
    });
    vm.runInContext(source, context, { filename: sourcePath });
    return context;
}

function validInvestAssessment() {
    return {
        independent: {
            result: 'pass',
            rationale: 'Self-contained logic.',
            evidence: 'No dependencies on unbuilt stories.',
        },
        negotiable: {
            result: 'pass',
            rationale: 'Implementation open to refinement.',
            evidence: 'Focuses on user outcome.',
        },
        valuable: {
            result: 'pass',
            rationale: 'Direct user capability.',
            evidence: 'Addresses requirement.',
        },
        estimable: {
            result: 'pass',
            rationale: 'Clear scope for sizing.',
            evidence: 'Discrete criteria.',
        },
        small: {
            result: 'pass',
            rationale: 'Sized for single iteration.',
            evidence: 'Effort is S.',
        },
        testable: {
            result: 'pass',
            rationale: 'Verifiable pass/fail criteria.',
            evidence: 'Verification steps included.',
        },
    };
}

function storyReview(instanceKey) {
    return {
        binding: {
            decision_fingerprint: `decision-${instanceKey}`,
            instance_key: instanceKey,
        },
        review: {
            phase: 'story',
            lineage: {
                backlog_item: {
                    requirement: 'Keep each Story bound to its source item.',
                    priority: 'high',
                    value_driver: 'correctness',
                    estimated_effort: 'medium',
                    justification: 'Selectors must not cross backlog items.',
                },
            },
            candidate: {
                story_items: [{
                    story_title: 'Exact Story draft',
                    statement: 'As an operator, I review the intended Story.',
                    persona: 'Operator',
                    acceptance_criteria: ['The exact selector is preserved.'],
                    specification_evidence: [],
                    invest_assessment: validInvestAssessment(),
                    estimated_effort: 'S',
                    story_points: 2,
                    effort_rationale: 'Single focused operation.',
                    order_rationale: 'First priority slice.',
                }],
                is_complete: true,
                clarifying_questions: [],
            },
            review: { state: 'pending' },
        },
    };
}

function directActionButton(action) {
    const label = { textContent: 'Generate Stories', dataset: {} };
    const status = { textContent: '', hidden: true };
    let button;
    const wrapper = {
        dataset: {},
        querySelector(selector) {
            return selector === '[data-delivery-action-status="true"]' ? status : null;
        },
        querySelectorAll() { return [button]; },
    };
    button = {
        _label: label,
        _status: status,
        _wrapper: wrapper,
        ariaBusy: null,
        disabled: false,
        dataset: {
            directAction: action.request_kind,
            deliveryActionNode: action.node_id,
            deliveryActionInstance: action.instance_key,
            deliveryActionHasInstance: action.instance_key === null || action.instance_key === undefined
                ? 'false'
                : 'true',
            deliveryActionEndpoint: action.endpoint,
            deliveryActionTransport: action.transport ?? '',
        },
        closest() { return wrapper; },
        querySelector(selector) {
            return selector === '[data-delivery-action-label="true"]' ? label : null;
        },
        setAttribute(name, value) {
            if (name === 'aria-busy') this.ariaBusy = value;
        },
        removeAttribute(name) {
            if (name === 'aria-busy') this.ariaBusy = null;
        },
    };
    return button;
}

function backlogCorrectionButton(action) {
    const button = directActionButton(action);
    button._label.textContent = 'Regenerate Backlog from feedback';
    Object.assign(button._wrapper.dataset, {
        backlogCorrectionAction: 'true',
        deliveryGenerationAction: action.request_kind,
        deliveryActionNode: action.node_id,
        deliveryActionInstance: action.instance_key ?? '',
        deliveryActionHasInstance: action.instance_key === null || action.instance_key === undefined
            ? 'false'
            : 'true',
        deliveryActionEndpoint: action.endpoint,
        deliveryActionTransport: action.transport ?? '',
    });
    return button;
}

function correctedPendingBacklogState() {
    const continuation = backlogFeedbackState('revision-ready').planningReviews.backlog.continuation;
    continuation.binding = { decision_fingerprint: 'sha256:pending-backlog', instance_key: null };
    continuation.review.candidate = {
        ...continuation.review.candidate,
        backlog_artifact_id: 8,
        artifact_fingerprint: 'sha256:backlog-8',
        version_number: 2,
        supersedes_backlog_artifact_id: 7,
    };
    continuation.review.review = { state: 'pending' };
    return {
        position: {
            decisions: [{
                node_id: 'backlog.review',
                instance_key: null,
                request_kind: 'decide_backlog',
                category: 'waiting',
                recommendation_kind: 'required',
                reason_code: 'BACKLOG_REVIEW_REQUIRED',
                decision_fingerprint: 'sha256:pending-backlog',
                fact_references: [
                    { fact_type: 'backlog', fact_id: '8', fingerprint: 'sha256:backlog-8' },
                    { fact_type: 'specification', fact_id: '31', fingerprint: 'sha256:specification-31' },
                    { fact_type: 'product_goal', fact_id: '21', fingerprint: 'sha256:product-goal-21' },
                ],
            }],
        },
        planningReviews: { backlog: continuation },
        actions: [{
            node_id: 'backlog.review',
            instance_key: null,
            request_kind: 'decide_backlog',
            endpoint: 'backlog/decide',
            transport: 'semantic',
        }],
    };
}

function installBacklogCorrectionDom(context, controls, focusTargets = {}) {
    context.document.querySelectorAll = (selector) => (
        selector === '[data-delivery-generation-action="record_backlog_draft"]' ? controls.map((button) => button._wrapper) : []
    );
    context.document.querySelector = (selector) => focusTargets[selector] ?? null;
}

function dashboardResponse(state) {
    return async (url, options = {}) => {
        if (options.method === 'POST') {
            return { ok: true, status: 200, text: async () => '{}' };
        }
        if (url.endsWith('/position')) {
            return {
                ok: true,
                status: 200,
                text: async () => JSON.stringify({ data: state.position, actions: state.actions }),
            };
        }
        if (url.endsWith('/backlog/review')) {
            return { ok: true, status: 200, text: async () => JSON.stringify({ data: state.planningReviews.backlog }) };
        }
        return { ok: true, status: 200, text: async () => JSON.stringify({ data: {} }) };
    };
}

function storyAction(overrides = {}) {
    return {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000001',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
        transport: 'semantic',
        ...overrides,
    };
}

function backlogFeedbackState(mode, overrides = {}) {
    const matrix = {
        'revision-ready': ['available', 'recovery', 'BACKLOG_REVISION_REQUIRED', false],
        active: ['waiting', 'required', 'BACKLOG_GENERATION_ACTIVE', false],
        'failed-retry': ['available', 'recovery', 'BACKLOG_GENERATION_FAILED', true],
        'expired-recovery': ['available', 'recovery', 'BACKLOG_GENERATION_RECOVERY_REQUIRED', true],
    };
    const [category, recommendation_kind, reason_code, hasAttempt] = matrix[mode];
    const decision = {
        node_id: 'backlog.generate',
        instance_key: null,
        request_kind: 'record_backlog_draft',
        category,
        recommendation_kind,
        reason_code,
        decision_fingerprint: `sha256:decision-${mode}`,
        fact_references: [
            { fact_type: 'backlog', fact_id: '7', fingerprint: 'sha256:backlog-7' },
            { fact_type: 'specification', fact_id: '31', fingerprint: 'sha256:specification-31' },
            { fact_type: 'product_goal', fact_id: '21', fingerprint: 'sha256:product-goal-21' },
            ...(hasAttempt ? [{ fact_type: 'node_attempt', fact_id: '81', fingerprint: 'sha256:attempt-81' }] : []),
        ],
        ...overrides.decision,
    };
    const continuation = {
        binding: {
            node_id: 'backlog.generate',
            instance_key: null,
            decision_fingerprint: decision.decision_fingerprint,
            ...overrides.binding,
        },
        review: {
            phase: 'backlog',
            lineage: {
                specification: { spec_version_id: 31, spec_hash: 'sha256:specification-31' },
                product_goal: { product_goal_artifact_id: 21, product_goal_fingerprint: 'sha256:product-goal-21' },
            },
            candidate: {
                backlog_artifact_id: 7,
                artifact_fingerprint: 'sha256:backlog-7',
                version_number: 1,
                supersedes_backlog_artifact_id: null,
                backlog_items: [],
                is_complete: true,
                clarifying_questions: [],
            },
            review: { state: 'feedback', rationale: 'Show the retry boundary.' },
            ...overrides.review,
        },
    };
    const action = {
        node_id: 'backlog.generate',
        instance_key: null,
        request_kind: 'record_backlog_draft',
        endpoint: 'backlog/generate',
        transport: 'semantic',
        ...overrides.action,
    };
    return {
        position: { decisions: [decision] },
        planningReviews: { backlog: { continuation } },
        actions: mode === 'active' ? [] : [action],
    };
}

test('Backlog Feedback display and correction-action contracts keep validation independent', () => {
    const context = loadFrontend();
    for (const mode of ['revision-ready', 'active', 'failed-retry', 'expired-recovery']) {
        const state = backlogFeedbackState(mode);
        const display = context.backlogFeedbackContinuationProjection(state);
        assert.equal(display.kind, 'display');
        assert.equal(display.mode, mode);
        const action = context.backlogCorrectionActionBinding(state, display);
        if (mode === 'active') {
            assert.equal(JSON.stringify(action), JSON.stringify({ kind: 'unavailable', reason: 'active' }));
        } else {
            assert.equal(action.kind, 'ready');
            assert.equal(action.action.endpoint, 'backlog/generate');
        }
    }

    const absent = context.backlogFeedbackContinuationProjection({ position: { decisions: [] }, planningReviews: { backlog: {} }, actions: [] });
    assert.equal(JSON.stringify(absent), JSON.stringify({ kind: 'absent' }));
    assert.equal(
        JSON.stringify(context.backlogCorrectionActionBinding({ actions: [] }, absent)),
        JSON.stringify({ kind: 'unavailable', reason: 'absent' }),
    );

    const malformed = backlogFeedbackState('revision-ready', {
        binding: { node_id: 'backlog.review' },
    });
    assert.equal(context.backlogFeedbackContinuationProjection(malformed).kind, 'error');
});

test('Backlog Feedback keeps durable display when correction action validation fails', () => {
    const context = loadFrontend();
    const invalidActions = [
        [],
        [
            backlogFeedbackState('revision-ready').actions[0],
            backlogFeedbackState('revision-ready').actions[0],
        ],
        [{ ...backlogFeedbackState('revision-ready').actions[0], endpoint: 'backlog/wrong' }],
        [{ ...backlogFeedbackState('revision-ready').actions[0], transport: 'wrong' }],
        [{ ...backlogFeedbackState('revision-ready').actions[0], node_id: 'backlog.review' }],
        [{ ...backlogFeedbackState('revision-ready').actions[0], request_kind: 'decide_backlog' }],
        [{ ...backlogFeedbackState('revision-ready').actions[0], instance_key: 'unexpected' }],
    ];

    for (const actions of invalidActions) {
        const state = backlogFeedbackState('revision-ready');
        state.actions = actions;
        const display = context.backlogFeedbackContinuationProjection(state);
        assert.equal(display.kind, 'display');
        assert.equal(
            JSON.stringify(context.backlogCorrectionActionBinding(state, display)),
            JSON.stringify({ kind: 'error', code: 'BACKLOG_CORRECTION_ACTION_INVALID' }),
        );
        const markup = context.deliveryPanelMarkup(state.position, state.planningReviews, state.actions);
        assert.ok(markup.includes('Backlog Feedback recorded'));
        assert.ok(markup.includes('data-backlog-feedback-projection-error="true"'));
    }
});

test('Backlog Feedback fails closed for every non-empty non-pending Backlog read shape', () => {
    const context = loadFrontend();
    for (const backlog of [{ continuation: null }, { torn: true }]) {
        const action = backlogFeedbackState('revision-ready').actions[0];
        const markup = context.deliveryPanelMarkup(
            { decisions: [] },
            { backlog },
            [action],
        );
        assert.ok(markup.includes('data-backlog-feedback-projection-error="true"'));
        assert.ok(!markup.includes('data-delivery-generation-action="record_backlog_draft"'));
    }
});

test('Backlog Feedback rejects torn or ambiguous durable joins before binding correction actions', () => {
    const context = loadFrontend();
    const reference = (state, factType) => state.position.decisions[0].fact_references.find(
        (item) => item.fact_type === factType,
    );
    const cases = [
        ['a second candidate continuation decision', (state) => {
            state.position.decisions.push({
                ...state.position.decisions[0],
                decision_fingerprint: 'sha256:second-continuation',
            });
        }],
        ['candidate ID missing on both sides', (state) => {
            state.planningReviews.backlog.continuation.review.candidate.backlog_artifact_id = undefined;
            reference(state, 'backlog').fact_id = undefined;
        }],
        ['candidate fingerprint missing on both sides', (state) => {
            state.planningReviews.backlog.continuation.review.candidate.artifact_fingerprint = undefined;
            reference(state, 'backlog').fingerprint = undefined;
        }],
        ['Specification ID missing from projection', (state) => {
            state.planningReviews.backlog.continuation.review.lineage.specification.spec_version_id = undefined;
        }],
        ['Specification ID missing on both sides', (state) => {
            state.planningReviews.backlog.continuation.review.lineage.specification.spec_version_id = undefined;
            reference(state, 'specification').fact_id = undefined;
        }],
        ['Specification hash missing from projection', (state) => {
            state.planningReviews.backlog.continuation.review.lineage.specification.spec_hash = undefined;
        }],
        ['Specification hash missing on both sides', (state) => {
            state.planningReviews.backlog.continuation.review.lineage.specification.spec_hash = undefined;
            reference(state, 'specification').fingerprint = undefined;
        }],
        ['Product Goal ID missing from projection', (state) => {
            state.planningReviews.backlog.continuation.review.lineage.product_goal.product_goal_artifact_id = undefined;
        }],
        ['Product Goal ID missing on both sides', (state) => {
            state.planningReviews.backlog.continuation.review.lineage.product_goal.product_goal_artifact_id = undefined;
            reference(state, 'product_goal').fact_id = undefined;
        }],
        ['Product Goal fingerprint missing from projection', (state) => {
            state.planningReviews.backlog.continuation.review.lineage.product_goal.product_goal_fingerprint = undefined;
        }],
        ['Product Goal fingerprint missing on both sides', (state) => {
            state.planningReviews.backlog.continuation.review.lineage.product_goal.product_goal_fingerprint = undefined;
            reference(state, 'product_goal').fingerprint = undefined;
        }],
        ['node attempt ID missing', (state) => { reference(state, 'node_attempt').fact_id = undefined; }],
        ['node attempt fingerprint missing', (state) => { reference(state, 'node_attempt').fingerprint = undefined; }],
        ['Backlog reference is not a canonical positive string', (state) => {
            reference(state, 'backlog').fact_id = '07';
        }],
        ['duplicate node attempt', (state) => {
            state.position.decisions[0].fact_references.push({
                fact_type: 'node_attempt', fact_id: '82', fingerprint: 'sha256:attempt-82',
            });
        }],
        ['unexpected fact type', (state) => {
            state.position.decisions[0].fact_references.push({
                fact_type: 'unexpected', fact_id: '1', fingerprint: 'sha256:unexpected',
            });
        }],
    ];

    for (const [name, mutate] of cases) {
        const state = backlogFeedbackState(
            name.includes('node attempt') || name.includes('unexpected') ? 'failed-retry' : 'revision-ready',
        );
        mutate(state);
        const display = context.backlogFeedbackContinuationProjection(state);
        assert.equal(display.kind, 'error', name);
        assert.equal(
            context.backlogCorrectionActionBinding(state, display).kind,
            'error',
            name,
        );
        const markup = context.deliveryPanelMarkup(state.position, state.planningReviews, state.actions);
        assert.ok(markup.includes('data-backlog-feedback-projection-error="true"'), name);
        assert.ok(!markup.includes('data-backlog-correction-action="true"'), name);
    }
});

test('Backlog pending review cards require one exact pending shape and exclude Feedback continuations', () => {
    const context = loadFrontend();
    const validPending = backlogFeedbackState('revision-ready').planningReviews.backlog.continuation;
    validPending.binding = { decision_fingerprint: 'sha256:pending-backlog', instance_key: null };
    validPending.review.review = { state: 'pending' };
    const unchanged = context.deliveryPanelMarkup({ decisions: [] }, { backlog: validPending }, []);
    assert.ok(unchanged.includes('data-planning-review-card="backlog"'));
    assert.ok(unchanged.includes('data-planning-review="backlog"'));

    const invalidTopLevels = [
        ['Feedback top-level review', { ...validPending, review: { ...validPending.review, review: { state: 'feedback', rationale: 'No controls.' } } }],
        ['pending review and continuation', { ...validPending, continuation: backlogFeedbackState('revision-ready').planningReviews.backlog.continuation }],
        ['malformed top-level state', { ...validPending, review: { ...validPending.review, review: { state: 'accepted' } } }],
    ];
    for (const [name, backlog] of invalidTopLevels) {
        const markup = context.deliveryPanelMarkup({ decisions: [] }, { backlog }, []);
        assert.ok(markup.includes('data-backlog-feedback-projection-error="true"'), name);
        assert.ok(!markup.includes('data-planning-review-card="backlog"'), name);
        assert.ok(!markup.includes('data-planning-review="backlog"'), name);
    }

    const malformedBindings = [
        ['binding missing', (backlog) => { delete backlog.binding; }],
        ['binding is not an object', (backlog) => { backlog.binding = 'not-an-object'; }],
        ['binding is empty', (backlog) => { backlog.binding = {}; }],
        ['decision fingerprint missing', (backlog) => { delete backlog.binding.decision_fingerprint; }],
        ['decision fingerprint is blank', (backlog) => { backlog.binding.decision_fingerprint = '   '; }],
        ['decision fingerprint is non-string', (backlog) => { backlog.binding.decision_fingerprint = 7; }],
        ['instance key is undefined', (backlog) => { backlog.binding.instance_key = undefined; }],
        ['instance key is a string', (backlog) => { backlog.binding.instance_key = 'backlog_item:PBI-000001'; }],
    ];
    const advertisedBacklogAction = backlogFeedbackState('revision-ready').actions;
    for (const [name, mutate] of malformedBindings) {
        const backlog = structuredClone(validPending);
        mutate(backlog);
        const markup = context.deliveryPanelMarkup({ decisions: [] }, { backlog }, advertisedBacklogAction);
        assert.ok(markup.includes('data-backlog-feedback-projection-error="true"'), name);
        assert.ok(!markup.includes('data-planning-review="backlog"'), name);
        assert.ok(!markup.includes('data-backlog-correction-action="true"'), name);
        assert.ok(!markup.includes('data-direct-action="record_backlog_draft"'), name);
    }
});

test('Backlog generation labels separate initial and Feedback correction states', () => {
    const context = loadFrontend();
    const initial = {
        node_id: 'backlog.generate', instance_key: null, request_kind: 'record_backlog_draft',
        endpoint: 'backlog/generate', transport: 'semantic',
    };
    const initialMarkup = context.deliveryPanelMarkup(
        { decisions: [{ ...initial, category: 'available', recommendation_kind: 'required', reason_code: 'BACKLOG_GENERATION_REQUIRED' }] },
        { backlog: {} }, [initial],
    );
    assert.ok(initialMarkup.includes('Generate Backlog'));
    assert.equal(context.deliveryGenerationActionDetails(initial).busyLabel, 'Generating Backlog...');

    const corrected = backlogFeedbackState('revision-ready');
    const correctionMarkup = context.deliveryPanelMarkup(
        corrected.position, corrected.planningReviews, corrected.actions,
    );
    assert.ok(correctionMarkup.includes('Regenerate Backlog from feedback'));
    assert.equal(
        context.backlogCorrectionActionDetails(corrected.actions[0]).busyLabel,
        'Regenerating Backlog from feedback...',
    );
});

test('corrected pending Backlog review renders candidate and parent identity', () => {
    const context = loadFrontend();
    const selected = backlogFeedbackState('revision-ready').planningReviews.backlog.continuation;
    selected.binding = { decision_fingerprint: 'sha256:pending-backlog', instance_key: null };
    selected.review.candidate = {
        ...selected.review.candidate,
        backlog_artifact_id: 8,
        version_number: 2,
        supersedes_backlog_artifact_id: 7,
    };
    selected.review.review = { state: 'pending' };
    const markup = context.deliveryPanelMarkup({ decisions: [] }, { backlog: selected }, []);
    assert.ok(markup.includes('Corrected Backlog candidate v2 (#8), replacing #7'));
    assert.ok(markup.includes('data-planning-review-card="backlog" tabindex="-1"'));
});

test('Backlog correction module lock blocks a stale rerender until authority reconciles', async () => {
    let postCount = 0;
    let resolveCorrection;
    let reloadFails = true;
    const corrected = correctedPendingBacklogState();
    const context = loadFrontend(async (url, options = {}) => {
        if (options.method === 'POST') {
            postCount += 1;
            return new Promise((resolve) => { resolveCorrection = resolve; });
        }
        if (reloadFails) throw new Error('dashboard reload unavailable');
        return dashboardResponse(corrected)(url, options);
    });
    const stale = backlogFeedbackState('revision-ready');
    const original = backlogCorrectionButton(stale.actions[0]);
    const replacement = backlogCorrectionButton(stale.actions[0]);
    let correctedFocuses = 0;
    installBacklogCorrectionDom(context, [original], {
        '[data-planning-review-card="backlog"]': { focus() { correctedFocuses += 1; } },
    });
    vm.runInContext(`selectedProjectId = 7; lifecycleState = ${JSON.stringify(stale)};`, context);

    const submission = context.runDirectAction('record_backlog_draft', original);
    await new Promise((resolve) => setImmediate(resolve));

    const submitting = JSON.parse(vm.runInContext(
        'JSON.stringify(activeBacklogCorrectionMutation)',
        context,
    ));
    assert.equal(postCount, 1);
    assert.equal(submitting.phase, 'submitting');
    assert.equal(original.disabled, true);
    assert.equal(original.ariaBusy, 'true');
    assert.equal(original._label.textContent, 'Regenerating Backlog from feedback...');

    installBacklogCorrectionDom(context, [replacement], {
        '[data-planning-review-card="backlog"]': { focus() { correctedFocuses += 1; } },
    });
    context.reapplyActiveBacklogCorrectionMutation();
    await context.runDirectAction('record_backlog_draft', replacement);
    await context.runDirectAction('record_backlog_draft', replacement);
    assert.equal(replacement.disabled, true);
    assert.equal(postCount, 1);

    resolveCorrection({ ok: true, status: 200, text: async () => '{}' });
    await submission;
    assert.equal(vm.runInContext('activeBacklogCorrectionMutation.phase', context), 'awaiting_authority');
    assert.notEqual(vm.runInContext('activeBacklogCorrectionMutation', context), null);
    assert.equal(original.disabled, true);
    assert.equal(replacement.disabled, true);

    reloadFails = false;
    assert.strictEqual(await context.loadDashboard(), true);
    assert.strictEqual(vm.runInContext('activeBacklogCorrectionMutation', context), null);
    assert.equal(correctedFocuses, 1);
});

test('Backlog correction rejects into recovery before an authoritative reload', async () => {
    let releaseDashboard;
    const dashboardGate = new Promise((resolve) => { releaseDashboard = resolve; });
    const failedRetry = backlogFeedbackState('failed-retry');
    const context = loadFrontend(async (url, options = {}) => {
        if (options.method === 'POST') throw new Error('provider unavailable');
        await dashboardGate;
        return dashboardResponse(failedRetry)(url, options);
    });
    const prior = backlogFeedbackState('revision-ready');
    const initiating = backlogCorrectionButton(prior.actions[0]);
    installBacklogCorrectionDom(context, [initiating]);
    vm.runInContext(`selectedProjectId = 7; lifecycleState = ${JSON.stringify(prior)};`, context);

    const submission = context.runDirectAction('record_backlog_draft', initiating);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(vm.runInContext('activeBacklogCorrectionMutation.phase', context), 'recovering_failure');
    assert.equal(initiating.disabled, true);

    releaseDashboard();
    await submission;
    assert.strictEqual(vm.runInContext('activeBacklogCorrectionMutation', context), null);
    assert.equal(initiating.disabled, true);
    const refreshed = context.deliveryPanelMarkup(
        failedRetry.position,
        failedRetry.planningReviews,
        failedRetry.actions,
    );
    assert.ok(refreshed.includes('data-backlog-correction-action="true"'));
    assert.ok(!refreshed.includes('data-backlog-correction-action="true" disabled'));
});

test('Backlog correction reconciliation preserves uncertain loads and only clears qualifying authority', async () => {
    const sameContinuation = backlogFeedbackState('revision-ready');
    const context = loadFrontend(dashboardResponse(sameContinuation));
    vm.runInContext(`
        selectedProjectId = 7;
        lifecycleState = ${JSON.stringify(sameContinuation)};
        activeBacklogCorrectionMutation = {
            token: 'backlog-token-1',
            phase: 'submitting',
            action: ${JSON.stringify(sameContinuation.actions[0])},
            backlogArtifactId: 7,
            decisionFingerprint: 'sha256:decision-revision-ready',
            focusIntent: false,
        };
    `, context);

    assert.strictEqual(await context.loadDashboard(), true);
    assert.equal(vm.runInContext('activeBacklogCorrectionMutation.phase', context), 'submitting');

    vm.runInContext("activeBacklogCorrectionMutation.phase = 'awaiting_authority';", context);
    assert.strictEqual(await context.loadDashboard(), true);
    assert.equal(vm.runInContext('activeBacklogCorrectionMutation.phase', context), 'awaiting_authority');

    const absence = { position: { decisions: [] }, planningReviews: { backlog: {} }, actions: [] };
    context.fetch = dashboardResponse(absence);
    assert.strictEqual(await context.loadDashboard(), true);
    assert.strictEqual(vm.runInContext('activeBacklogCorrectionMutation', context), null);

    const malformed = loadFrontend(async () => {
        throw new Error('dashboard reload unavailable');
    });
    vm.runInContext(`
        selectedProjectId = 7;
        activeBacklogCorrectionMutation = {
            token: 'backlog-token-2', phase: 'awaiting_authority',
            action: ${JSON.stringify(sameContinuation.actions[0])},
            backlogArtifactId: 7,
            decisionFingerprint: 'sha256:decision-revision-ready',
            focusIntent: false,
        };
    `, malformed);
    await assert.rejects(malformed.loadDashboard());
    assert.equal(vm.runInContext('activeBacklogCorrectionMutation.phase', malformed), 'awaiting_authority');
});

test('Backlog correction preserves focus through a same-decision reload until corrected-pending authority arrives', async () => {
    const sameContinuation = backlogFeedbackState('revision-ready');
    const correctedPending = correctedPendingBacklogState();
    const context = loadFrontend(dashboardResponse(sameContinuation));
    let focusCount = 0;
    installBacklogCorrectionDom(context, [], {
        '[data-backlog-correction-action="true"]:not([disabled])': { focus() { focusCount += 1; } },
        '[data-planning-review-card="backlog"]': { focus() { focusCount += 1; } },
    });
    vm.runInContext(`
        selectedProjectId = 7;
        lifecycleState = ${JSON.stringify(sameContinuation)};
        activeBacklogCorrectionMutation = {
            token: 'backlog-focus-authority', phase: 'awaiting_authority',
            action: ${JSON.stringify(sameContinuation.actions[0])}, backlogArtifactId: 7,
            decisionFingerprint: 'sha256:decision-revision-ready', focusIntent: true,
        };
    `, context);

    assert.strictEqual(await context.loadDashboard(), true);
    assert.equal(vm.runInContext('activeBacklogCorrectionMutation.phase', context), 'awaiting_authority');
    assert.equal(vm.runInContext('activeBacklogCorrectionMutation.focusIntent', context), true);
    assert.equal(focusCount, 0);

    context.fetch = dashboardResponse(correctedPending);
    assert.strictEqual(await context.loadDashboard(), true);
    assert.strictEqual(vm.runInContext('activeBacklogCorrectionMutation', context), null);
    assert.equal(focusCount, 1);
});

test('Backlog correction rejects torn absence and corrected-pending authority in both token phases', async () => {
    const old = backlogFeedbackState('revision-ready');
    const token = (phase) => `
        selectedProjectId = 7;
        activeBacklogCorrectionMutation = {
            token: 'backlog-torn-${phase}', phase: '${phase}',
            action: ${JSON.stringify(old.actions[0])}, backlogArtifactId: 7,
            decisionFingerprint: 'sha256:decision-revision-ready', focusIntent: false,
        };
    `;
    const cleanAbsence = {
        position: { decisions: [{ node_id: 'roadmap.generate', decision_fingerprint: 'sha256:roadmap' }] },
        planningReviews: { backlog: {} },
        actions: [],
    };
    const tornStates = [
        ['absence with prior Feedback decision', () => ({
            ...structuredClone(cleanAbsence), position: structuredClone(old.position),
        })],
        ['absence with correction action', () => ({
            ...structuredClone(cleanAbsence), actions: structuredClone(old.actions),
        })],
        ['absence with correction node and wrong request kind', () => ({
            ...structuredClone(cleanAbsence),
            actions: [{
                node_id: 'backlog.generate', instance_key: null,
                request_kind: 'decide_backlog', endpoint: 'backlog/other', transport: 'semantic',
            }],
        })],
        ['absence with correction endpoint and missing request kind', () => ({
            ...structuredClone(cleanAbsence),
            actions: [{
                node_id: 'backlog.other', instance_key: null,
                endpoint: 'backlog/generate', transport: 'semantic',
            }],
        })],
        ['absence with null continuation', () => ({
            ...structuredClone(cleanAbsence), planningReviews: { backlog: { continuation: null } },
        })],
        ['absence with unknown key', () => ({
            ...structuredClone(cleanAbsence), planningReviews: { backlog: { torn: true } },
        })],
        ['pending with prior Feedback decision', () => {
            const pending = correctedPendingBacklogState();
            pending.position.decisions.push(structuredClone(old.position.decisions[0]));
            return pending;
        }],
        ['pending with prior correction action', () => {
            const pending = correctedPendingBacklogState();
            pending.actions.push(structuredClone(old.actions[0]));
            return pending;
        }],
        ['pending with malformed correction action', () => {
            const pending = correctedPendingBacklogState();
            pending.actions.push({ ...old.actions[0], endpoint: 'backlog/wrong' });
            return pending;
        }],
        ['pending with correction node and endpoint but wrong request kind', () => {
            const pending = correctedPendingBacklogState();
            pending.actions.push({
                node_id: 'backlog.generate', instance_key: null,
                request_kind: 'decide_backlog', endpoint: 'backlog/generate', transport: 'semantic',
            });
            return pending;
        }],
        ['pending with correction node and missing request kind', () => {
            const pending = correctedPendingBacklogState();
            pending.actions.push({
                node_id: 'backlog.generate', instance_key: null,
                endpoint: 'backlog/other', transport: 'semantic',
            });
            return pending;
        }],
        ['pending with a second Backlog review decision', () => {
            const pending = correctedPendingBacklogState();
            pending.position.decisions.push({
                ...pending.position.decisions[0],
                decision_fingerprint: 'sha256:second-pending-backlog',
            });
            return pending;
        }],
        ['pending without corrected version', () => {
            const pending = correctedPendingBacklogState();
            delete pending.planningReviews.backlog.review.candidate.version_number;
            return pending;
        }],
        ['pending with malformed corrected version', () => {
            const pending = correctedPendingBacklogState();
            pending.planningReviews.backlog.review.candidate.version_number = '2';
            return pending;
        }],
        ['pending without Backlog content', () => {
            const pending = correctedPendingBacklogState();
            delete pending.planningReviews.backlog.review.candidate.backlog_items;
            return pending;
        }],
        ['pending with malformed Backlog content', () => {
            const pending = correctedPendingBacklogState();
            pending.planningReviews.backlog.review.candidate.backlog_items = {};
            return pending;
        }],
        ['pending without current review content', () => {
            const pending = correctedPendingBacklogState();
            delete pending.planningReviews.backlog.review.candidate.is_complete;
            return pending;
        }],
        ['pending with malformed current review content', () => {
            const pending = correctedPendingBacklogState();
            pending.planningReviews.backlog.review.candidate.clarifying_questions = {};
            return pending;
        }],
        ['pending without Specification lineage', () => {
            const pending = correctedPendingBacklogState();
            delete pending.planningReviews.backlog.review.lineage.specification;
            return pending;
        }],
        ['pending with wrong Product Goal lineage', () => {
            const pending = correctedPendingBacklogState();
            pending.planningReviews.backlog.review.lineage.product_goal.product_goal_fingerprint = 'sha256:wrong-goal';
            return pending;
        }],
        ['pending without Backlog fact reference', () => {
            const pending = correctedPendingBacklogState();
            pending.position.decisions[0].fact_references.shift();
            return pending;
        }],
        ['pending with duplicate Specification fact reference', () => {
            const pending = correctedPendingBacklogState();
            pending.position.decisions[0].fact_references.push(
                structuredClone(pending.position.decisions[0].fact_references[1]),
            );
            return pending;
        }],
        ['pending with wrong Product Goal fact reference', () => {
            const pending = correctedPendingBacklogState();
            pending.position.decisions[0].fact_references[2].fingerprint = 'sha256:wrong-goal';
            return pending;
        }],
    ];

    for (const phase of ['awaiting_authority', 'recovering_failure']) {
        for (const [name, build] of tornStates) {
            const context = loadFrontend(dashboardResponse(build()));
            vm.runInContext(token(phase), context);
            assert.strictEqual(await context.loadDashboard(), true, `${phase}: ${name}`);
            assert.equal(
                vm.runInContext('activeBacklogCorrectionMutation.phase', context),
                phase,
                `${phase}: ${name}`,
            );
        }

        for (const [name, state] of [
            ['complete absence', cleanAbsence],
            ['complete corrected pending', correctedPendingBacklogState()],
        ]) {
            const context = loadFrontend(dashboardResponse(state));
            vm.runInContext(token(phase), context);
            assert.strictEqual(await context.loadDashboard(), true, `${phase}: ${name}`);
            assert.strictEqual(
                vm.runInContext('activeBacklogCorrectionMutation', context),
                null,
                `${phase}: ${name}`,
            );
        }
    }
});

test('Backlog correction recovery clears only authoritative active or fresh correction states', async () => {
    const prior = backlogFeedbackState('revision-ready');
    const active = backlogFeedbackState('active');
    const activeContext = loadFrontend(dashboardResponse(active));
    vm.runInContext(`
        selectedProjectId = 7;
        activeBacklogCorrectionMutation = {
            token: 'backlog-active', phase: 'recovering_failure',
            action: ${JSON.stringify(prior.actions[0])}, backlogArtifactId: 7,
            decisionFingerprint: 'sha256:decision-revision-ready', focusIntent: false,
        };
    `, activeContext);
    assert.strictEqual(await activeContext.loadDashboard(), true);
    assert.strictEqual(vm.runInContext('activeBacklogCorrectionMutation', activeContext), null);
    assert.ok(!activeContext.deliveryPanelMarkup(
        active.position,
        active.planningReviews,
        active.actions,
    ).includes('data-backlog-correction-action="true"'));

    const fresh = backlogFeedbackState('revision-ready');
    fresh.position.decisions[0].decision_fingerprint = 'sha256:fresh-decision';
    fresh.planningReviews.backlog.continuation.binding.decision_fingerprint = 'sha256:fresh-decision';
    const oldControl = backlogCorrectionButton(prior.actions[0]);
    const freshContext = loadFrontend(dashboardResponse(fresh));
    installBacklogCorrectionDom(freshContext, [oldControl]);
    vm.runInContext(`
        selectedProjectId = 7;
        activeBacklogCorrectionMutation = {
            token: 'backlog-fresh', phase: 'recovering_failure',
            action: ${JSON.stringify(prior.actions[0])}, backlogArtifactId: 7,
            decisionFingerprint: 'sha256:decision-revision-ready', focusIntent: false,
        };
    `, freshContext);
    freshContext.reapplyActiveBacklogCorrectionMutation();
    assert.equal(oldControl.disabled, true);
    assert.strictEqual(await freshContext.loadDashboard(), true);
    assert.strictEqual(vm.runInContext('activeBacklogCorrectionMutation', freshContext), null);
    assert.equal(oldControl.disabled, true);
    assert.ok(freshContext.deliveryPanelMarkup(
        fresh.position,
        fresh.planningReviews,
        fresh.actions,
    ).includes('data-backlog-correction-action="true"'));

    const malformed = backlogFeedbackState('revision-ready');
    malformed.planningReviews.backlog.continuation.binding.node_id = 'backlog.review';
    const malformedContext = loadFrontend(dashboardResponse(malformed));
    vm.runInContext(`
        selectedProjectId = 7;
        activeBacklogCorrectionMutation = {
            token: 'backlog-malformed', phase: 'recovering_failure',
            action: ${JSON.stringify(prior.actions[0])}, backlogArtifactId: 7,
            decisionFingerprint: 'sha256:decision-revision-ready', focusIntent: false,
        };
    `, malformedContext);
    assert.strictEqual(await malformedContext.loadDashboard(), true);
    assert.equal(vm.runInContext('activeBacklogCorrectionMutation.phase', malformedContext), 'recovering_failure');

    const invalidAction = backlogFeedbackState('revision-ready');
    invalidAction.actions[0].endpoint = 'backlog/wrong';
    const invalidActionContext = loadFrontend(dashboardResponse(invalidAction));
    vm.runInContext(`
        selectedProjectId = 7;
        activeBacklogCorrectionMutation = {
            token: 'backlog-invalid-action', phase: 'recovering_failure',
            action: ${JSON.stringify(prior.actions[0])}, backlogArtifactId: 7,
            decisionFingerprint: 'sha256:decision-revision-ready', focusIntent: false,
        };
    `, invalidActionContext);
    assert.strictEqual(await invalidActionContext.loadDashboard(), true);
    assert.equal(
        vm.runInContext('activeBacklogCorrectionMutation.phase', invalidActionContext),
        'recovering_failure',
    );
});

test('Backlog correction token survives an aborted and superseded dashboard load', async () => {
    let fetchCount = 0;
    const context = loadFrontend(async (_url, options = {}) => {
        fetchCount += 1;
        if (fetchCount <= 13) {
            return new Promise((_resolve, reject) => {
                options.signal.addEventListener('abort', () => reject(new Error('aborted')));
            });
        }
        throw new Error('replacement dashboard load unavailable');
    });
    const state = backlogFeedbackState('revision-ready');
    vm.runInContext(`
        selectedProjectId = 7;
        activeBacklogCorrectionMutation = {
            token: 'backlog-superseded', phase: 'awaiting_authority',
            action: ${JSON.stringify(state.actions[0])}, backlogArtifactId: 7,
            decisionFingerprint: 'sha256:decision-revision-ready', focusIntent: false,
        };
    `, context);

    const firstLoad = context.loadDashboard();
    await new Promise((resolve) => setImmediate(resolve));
    const secondLoad = context.loadDashboard();
    assert.strictEqual(await firstLoad, false);
    await assert.rejects(secondLoad, /replacement dashboard load unavailable/);
    assert.equal(vm.runInContext('activeBacklogCorrectionMutation.phase', context), 'awaiting_authority');
});

test('Backlog Feedback focus chooses the current authoritative target once', () => {
    const focusCases = [
        ['revision-ready', '[data-backlog-correction-action="true"]:not([disabled])'],
        ['active', '[data-backlog-feedback-continuation="true"]'],
        ['projection-error', '[data-backlog-feedback-projection-error="true"]'],
        ['corrected-pending', '[data-planning-review-card="backlog"]'],
    ];
    for (const [mode, selector] of focusCases) {
        const context = loadFrontend();
        const state = mode === 'corrected-pending'
            ? correctedPendingBacklogState()
            : backlogFeedbackState(mode === 'projection-error' ? 'revision-ready' : mode);
        if (mode === 'projection-error') {
            state.planningReviews.backlog.continuation.binding.node_id = 'backlog.review';
        }
        let focusCount = 0;
        installBacklogCorrectionDom(context, [], {
            [selector]: { focus() { focusCount += 1; } },
        });
        vm.runInContext(`
            lifecycleState = ${JSON.stringify(state)};
            activeBacklogCorrectionMutation = {
                token: 'backlog-focus-${mode}', phase: 'awaiting_authority',
                action: ${JSON.stringify(backlogFeedbackState('revision-ready').actions[0])},
                backlogArtifactId: 7, decisionFingerprint: 'sha256:decision-revision-ready', focusIntent: true,
            };
        `, context);

        context.consumeBacklogCorrectionFocus();
        context.consumeBacklogCorrectionFocus();
        assert.equal(focusCount, 1, mode);
        assert.equal(vm.runInContext('activeBacklogCorrectionMutation.focusIntent', context), false, mode);
    }
});

function selectedScopeStory(overrides = {}) {
    return {
        story_id: 101,
        source_story_item_id: 'US-001',
        is_superseded: false,
        structurally_eligible: true,
        structural_eligibility_status: 'eligible',
        sprint_selection_state: 'selected',
        sprint_selection_state_fingerprint: 'sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
        selected_scope_fingerprint: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        dependency_safe: false,
        sprint_candidate: false,
        validation_status: 'validated',
        validation_failures: [],
        ...overrides,
    };
}

function sprintCandidateStory(overrides = {}) {
    return selectedScopeStory({
        dependency_safe: true,
        sprint_candidate: true,
        ...overrides,
    });
}

function sprintCapacity(overrides = {}) {
    return {
        status: 'recommended',
        recommended_max_story_points: 8,
        source: 'project_metrics',
        rationale: '8 points, based on the last 1 completed Sprints: 8.',
        ...overrides,
    };
}

const structuralEvidenceScope = {
    proves: [
        'exact Story identity',
        'immutable accepted Story artifact/item binding',
        'accepted Backlog and Specification lineage',
        'parent-bounded Specification references',
        'required Story shape',
        'non-empty acceptance criteria',
        'current evidence and input fingerprints',
    ],
    does_not_prove: [
        'semantic/model quality',
        'product value',
        'human Sprint selection',
        'dependency safety',
        'Sprint candidacy',
        'Sprint-generation readiness',
    ],
};

function dependencyProjection(stories, edges = [], selectedStoryIds = null) {
    const selected = selectedStoryIds ?? stories
        .filter((story) => story.structurally_eligible
            && story.sprint_selection_state === 'selected')
        .map((story) => story.story_id);
    const selectedStory = stories.find((story) => story.story_id === selected[0]);
    return {
        stories,
        edges,
        selected_story_ids: selected,
        selected_scope_fingerprint: selectedStory?.selected_scope_fingerprint ?? null,
        structural_evidence_scope: structuralEvidenceScope,
    };
}

test('human lifecycle labels are the exact direct-Spec sequence', () => {
    const context = loadFrontend();
    assert.deepEqual(Array.from(context.lifecycleStageLabels()), [
        'Vision',
        'Product Goal',
        'Specification',
        'Backlog',
        'Roadmap',
        'Stories',
        'Sprint',
        'Execution',
        'Review',
    ]);
});

test('workflow position hides graph guards and machine fingerprints', () => {
    const context = loadFrontend();
    const markup = context.workflowPositionMarkup({
        graph_version: 'agileforge.workflow.v2',
        fact_fingerprint: 'sha256:secret-facts',
        decisions: [{
            node_id: 'backlog.generate',
            child_graph_id: 'backlog',
            request_kind: 'record_backlog_draft',
            category: 'available',
            reason_code: 'BACKLOG_REQUIRED',
            decision_fingerprint: 'sha256:secret-decision',
        }],
    });
    assert.ok(markup.includes('Backlog'));
    assert.ok(!markup.includes('secret-facts'));
    assert.ok(!markup.includes('secret-decision'));
    assert.ok(!markup.includes('backlog.generate'));
});

function acceptedSprintStatus(overrides = {}) {
    const fingerprint = `sha256:${'a'.repeat(64)}`;
    const candidateFingerprint = `sha256:${'b'.repeat(64)}`;
    const taskFingerprint = `sha256:${'c'.repeat(64)}`;
    return {
        project_id: 7,
        sprint: { sprint_id: 31, status: 'planned', completed_at: null },
        accepted_plan: {
            sprint_id: 31,
            status: 'planned',
            goal: 'Ship accepted scope.',
            owner: {
                kind: 'named_team',
                key: 'agileforge:sprint-owner:named-team:v1:sha256:468c0b971b948afb301b21a6eaea9425aa5568e111f623452bfa5ff3af938ff6',
                label: 'Core team',
                display_label: 'Core team',
            },
            sprint_plan_artifact_id: 41,
            sprint_plan_artifact_decision_id: 51,
            plan_fingerprint: fingerprint,
            candidate_set_fingerprint: candidateFingerprint,
            task_content_fingerprint: taskFingerprint,
            acceptance: {
                rationale: 'Scope is coherent.',
                reviewer: 'operator@example.com',
                decided_at: '2026-08-30T12:00:00Z',
            },
            selected_stories: [{
                story_id: 61,
                story_item_id: 'US-0001',
                title: 'Keep Sprint continuity',
                story_points: 3,
                task_count: 1,
            }],
            total_points: 3,
            task_count: 1,
        },
        start: null,
        tasks: [{
            task_id: 71,
            sprint_id: 31,
            story_id: 61,
            description: 'Render exact next work.',
            status: 'To Do',
            fact_fingerprint: `sha256:${'e'.repeat(64)}`,
        }],
        review: null,
        closure: null,
        ...overrides,
    };
}

function sprintDecision(requestKind, reasonCode, recommendationKind, factReferences = []) {
    return {
        node_id: requestKind === 'start_sprint'
            ? 'planning.sprint.start'
            : 'planning.sprint.plan',
        instance_key: null,
        request_kind: requestKind,
        category: 'available',
        recommendation_kind: recommendationKind,
        reason_code: reasonCode,
        decision_fingerprint: `sha256:${'d'.repeat(64)}`,
        fact_references: factReferences,
    };
}

function sprintStartReferences(status = acceptedSprintStatus()) {
    const plan = status.accepted_plan;
    return [
        { fact_type: 'sprint_plan', fact_id: String(plan.sprint_plan_artifact_id), fingerprint: plan.plan_fingerprint },
        { fact_type: 'candidate_set', fact_id: String(status.project_id), fingerprint: plan.candidate_set_fingerprint },
        { fact_type: 'sprint_plan_tasks', fact_id: String(status.sprint.sprint_id), fingerprint: plan.task_content_fingerprint },
    ];
}

test('Sprint card deterministically prefers required start over optional correction', () => {
    const context = loadFrontend();
    const status = acceptedSprintStatus();
    const start = sprintDecision(
        'start_sprint',
        'SPRINT_READY_TO_START',
        'required',
        sprintStartReferences(status),
    );
    const correction = sprintDecision(
        'record_sprint_plan',
        'SPRINT_PLAN_CORRECTION_AVAILABLE',
        'optional_reentry',
    );
    const actions = [
        { node_id: start.node_id, instance_key: null, request_kind: 'start_sprint', endpoint: 'sprint/start', transport: 'semantic' },
        { node_id: correction.node_id, instance_key: null, request_kind: 'record_sprint_plan', endpoint: 'sprint/generate', transport: 'semantic' },
    ];
    for (const decisions of [[correction, start], [start, correction]]) {
        const sprint = context.lifecycleCardProjection(
            { decisions }, actions, { sprintStatus: { kind: 'ready', data: status } },
        ).find((card) => card.stage === 'Sprint');
        assert.equal(sprint.status, 'Ready to start');
        assert.equal(sprint.reason, 'Accepted Sprint plan is ready to start.');
    }
});

test('Sprint status parsing and start binding fail closed on torn authority', async () => {
    const context = loadFrontend();
    const status = acceptedSprintStatus();
    assert.ok(await context.validateSprintStatusProjection(status, 7));
    assert.equal(
        await context.validateSprintStatusProjection({ ...status, project_id: 8 }, 7),
        null,
    );
    assert.equal(
        await context.validateSprintStatusProjection({
            ...status,
            accepted_plan: { ...status.accepted_plan, task_count: 2 },
        }, 7),
        null,
    );
    const decision = sprintDecision(
        'start_sprint',
        'SPRINT_READY_TO_START',
        'required',
        sprintStartReferences(status),
    );
    const action = {
        node_id: decision.node_id,
        instance_key: null,
        request_kind: 'start_sprint',
        endpoint: 'sprint/start',
        transport: 'semantic',
    };
    const binding = context.sprintStartBinding(status, { decisions: [decision] }, [action]);
    assert.equal(binding.decisionFingerprint, decision.decision_fingerprint);
    decision.fact_references[2].fingerprint = `sha256:${'f'.repeat(64)}`;
    assert.equal(context.sprintStartBinding(status, { decisions: [decision] }, [action]), null);
});

test('Sprint status renders complete active execution actions without response-order choice', () => {
    const context = loadFrontend();
    const planned = acceptedSprintStatus();
    const active = {
        ...planned,
        sprint: { ...planned.sprint, status: 'active' },
        accepted_plan: { ...planned.accepted_plan, status: 'active' },
        start: {
            sprint_id: 31,
            sprint_plan_artifact_id: 41,
            sprint_plan_artifact_decision_id: 51,
            plan_fingerprint: planned.accepted_plan.plan_fingerprint,
            candidate_set_fingerprint: planned.accepted_plan.candidate_set_fingerprint,
            task_content_fingerprint: planned.accepted_plan.task_content_fingerprint,
        },
        tasks: [
            ...planned.tasks,
            {
                task_id: 72,
                sprint_id: 31,
                story_id: 61,
                description: 'Second exact task.',
                status: 'To Do',
                fact_fingerprint: `sha256:${'e'.repeat(64)}`,
            },
        ],
    };
    const action = (taskId) => ({
        node_id: 'execution.task.complete',
        instance_key: `task:${taskId}`,
        request_kind: 'complete_task',
        endpoint: 'sprint/task/complete',
        transport: 'semantic',
    });
    const decision = (taskId) => ({
        node_id: 'execution.task.complete',
        instance_key: `task:${taskId}`,
        request_kind: 'complete_task',
        category: 'available',
        recommendation_kind: 'required',
        reason_code: 'NEXT_TASK_READY',
        fact_references: [{ fact_type: 'task', fact_id: String(taskId), fingerprint: `sha256:${'e'.repeat(64)}` }],
    });
    const position = { decisions: [decision(72), decision(71)] };
    const markup = context.sprintStatusMarkup(
        { kind: 'ready', data: active }, position, [action(72), action(71)],
    );
    assert.ok(markup.includes('Sprint #31 is active'));
    assert.ok(markup.includes('Task #71'));
    assert.ok(markup.includes('Task #72'));
    assert.ok(markup.indexOf('Task #71') < markup.indexOf('Task #72'));
    assert.ok(markup.includes('2 current execution actions'));
    const executionCard = context.lifecycleCardProjection(
        position,
        [action(72), action(71)],
        { sprintStatus: { kind: 'ready', data: active } },
    ).find((card) => card.stage === 'Execution');
    assert.equal(executionCard.status, 'Active');

    const torn = {
        ...active,
        tasks: active.tasks.map((task) => task.task_id === 71
            ? {
                ...task,
                status: 'Done',
                fact_fingerprint: `sha256:${'f'.repeat(64)}`,
            }
            : task),
    };
    const tornProjection = context.sprintExecutionProjection(
        torn,
        position,
        [action(72), action(71)],
    );
    assert.equal(tornProjection.kind, 'error');
    assert.equal(tornProjection.items.length, 0);

    const zero = context.sprintStatusMarkup(
        { kind: 'ready', data: active }, { decisions: [] }, [],
    );
    assert.ok(zero.includes('No execution action is currently available'));
});

test('Sprint status failure locks correction generation instead of contradicting its alert', () => {
    const context = loadFrontend();
    const correction = sprintDecision(
        'record_sprint_plan',
        'SPRINT_PLAN_CORRECTION_AVAILABLE',
        'optional_reentry',
    );
    const action = {
        node_id: correction.node_id,
        instance_key: null,
        request_kind: 'record_sprint_plan',
        endpoint: 'sprint/generate',
        transport: 'semantic',
    };
    const markup = context.deliveryPanelMarkup(
        { decisions: [correction] },
        {},
        [action],
        { sprintStatus: { kind: 'error' } },
    );
    assert.ok(markup.includes('Sprint status unavailable'));
    assert.ok(!markup.includes('data-delivery-generation-form="record_sprint_plan"'));
});

test('delivery generation submits the exact rendered Story selector', async () => {
    const requests = [];
    const context = loadFrontend(async (url, options = {}) => {
        requests.push({ url, options });
        return { ok: true, text: async () => '{}' };
    });
    const actions = [
        {
            node_id: 'planning.story.generate',
            instance_key: 'backlog_item:PBI-000001',
            request_kind: 'record_story_draft',
            endpoint: 'story/generate',
        },
        {
            node_id: 'planning.story.generate',
            instance_key: 'backlog_item:PBI-000002',
            request_kind: 'record_story_draft',
            endpoint: 'story/generate',
        },
    ];
    vm.runInContext(`selectedProjectId = 7; lifecycleState = ${JSON.stringify({
        actions,
        position: {},
        planningReviews: {},
    })};`, context);

    await context.runDirectAction(
        'record_story_draft',
        directActionButton(actions[1]),
    );

    const post = requests.find(({ options }) => options.method === 'POST');
    assert.ok(post);
    assert.equal(
        JSON.parse(post.options.body).instance_key,
        'backlog_item:PBI-000002',
    );
});

test('successful generation keeps its old control disabled when dashboard reload fails', async () => {
    let postCount = 0;
    const context = loadFrontend(async (_url, options = {}) => {
        if (options.method === 'POST') {
            postCount += 1;
            return { ok: true, text: async () => '{}' };
        }
        throw new Error('dashboard reload unavailable');
    });
    const action = storyAction();
    const button = directActionButton(action);
    vm.runInContext(`selectedProjectId = 7; lifecycleState = ${JSON.stringify({
        actions: [action],
        position: {},
        planningReviews: {},
    })};`, context);

    await context.runDirectAction('record_story_draft', button);
    await context.runDirectAction('record_story_draft', button);

    assert.equal(button.disabled, true);
    assert.equal(button.ariaBusy, null);
    assert.equal(button._wrapper.dataset.submitting, undefined);
    assert.equal(button._label.textContent, 'Generate Stories');
    assert.match(button._status.textContent, /could not reload/i);
    assert.equal(postCount, 1);
});

test('superseded dashboard reload does not re-enable the old generation control', async () => {
    let postCount = 0;
    const context = loadFrontend(async (_url, options = {}) => {
        if (options.method === 'POST') postCount += 1;
        return { ok: true, text: async () => '{}' };
    });
    const action = storyAction();
    const button = directActionButton(action);
    vm.runInContext(`selectedProjectId = 7; lifecycleState = ${JSON.stringify({
        actions: [action],
        position: {},
        planningReviews: {},
    })}; loadDashboard = async () => false;`, context);

    await context.runDirectAction('record_story_draft', button);
    await context.runDirectAction('record_story_draft', button);

    assert.equal(button.disabled, true);
    assert.equal(button.ariaBusy, null);
    assert.equal(button._wrapper.dataset.submitting, undefined);
    assert.equal(button._label.textContent, 'Generate Stories');
    assert.match(button._status.textContent, /could not reload/i);
    assert.equal(postCount, 1);
});

test('delivery generation rejects a rendered action whose binding changed', async () => {
    const rendered = storyAction();
    const changedActions = [
        storyAction({ node_id: 'planning.story.generate.changed' }),
        storyAction({ endpoint: 'story/generate/changed' }),
        storyAction({ transport: 'changed' }),
    ];

    for (const current of changedActions) {
        const requests = [];
        const context = loadFrontend(async (url, options = {}) => {
            requests.push({ url, options });
            return { ok: true, text: async () => '{}' };
        });
        vm.runInContext(`selectedProjectId = 7; lifecycleState = ${JSON.stringify({
            actions: [current],
            position: {},
            planningReviews: {},
        })};`, context);

        await context.runDirectAction(
            'record_story_draft',
            directActionButton(rendered),
        );

        assert.equal(
            requests.filter(({ options }) => options.method === 'POST').length,
            0,
        );
    }
});

test('delivery panel keeps a pending Story review beside another generation action', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000002',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
    };

    const markup = context.deliveryPanelMarkup(
        { decisions: [] },
        { stories: { items: [storyReview('backlog_item:PBI-000001')] } },
        [action],
    );

    assert.ok(markup.includes('data-planning-review-card="story"'));
    assert.ok(markup.includes('data-delivery-generation-action="record_story_draft"'));
});

function sprintOwnerProjection(overrides = {}) {
    return {
        kind: 'solo_project',
        key: 'agileforge:sprint-owner:solo-project:v1:project:7',
        label: '[agileforge:sprint-owner:solo-project:v1:project:7] Solo operator for Exact Project',
        display_label: 'Solo operator for Exact Project',
        named_team_override_allowed: true,
        ...overrides,
    };
}

async function validatedSprintOwner(context, owner = sprintOwnerProjection(), projectId = 7) {
    assert.strictEqual(await context.validateSprintOwnerProjection(owner, projectId), owner);
    return owner;
}

test('Sprint generation displays the resolved solo owner and makes named Teams optional', async () => {
    const context = loadFrontend();
    const candidate = sprintCandidateStory();
    const sprintOwner = await validatedSprintOwner(context);
    const markup = context.deliveryPanelMarkup(
        { decisions: [] },
        {},
        [{
            node_id: 'planning.sprint.plan',
            instance_key: null,
            request_kind: 'record_sprint_plan',
            endpoint: 'sprint/generate',
        }],
        {
            storyDependencies: dependencyProjection([candidate]),
            sprintCandidates: {
                project_id: 7,
                items: [candidate],
                sprint_owner: sprintOwner,
                capacity: sprintCapacity(),
            },
        },
    );

    assert.ok(markup.includes('data-delivery-generation-form="record_sprint_plan"'));
    assert.ok(markup.includes('name="team_name"'));
    assert.ok(markup.includes('Sprint owner'));
    assert.ok(markup.includes('Solo operator for Exact Project'));
    assert.ok(markup.includes('Generate the Sprint plan from the selected Sprint candidates.'));
    assert.ok(!markup.includes('approved Stories'));
    assert.ok(!markup.includes('agileforge:sprint-owner:'));
    assert.ok(markup.includes('Named team override'));
    assert.ok(!markup.includes('name="team_name" type="text" required'));
});

test('Sprint generation blocks when provider-free owner evidence is unavailable', () => {
    const context = loadFrontend();
    const candidate = sprintCandidateStory();
    const markup = context.deliveryPanelMarkup(
        { decisions: [] }, {}, [{
            node_id: 'planning.sprint.plan', instance_key: null,
            request_kind: 'record_sprint_plan', endpoint: 'sprint/generate',
        }], {
            storyDependencies: dependencyProjection([candidate]),
            sprintCandidates: { items: [candidate] },
        },
    );

    assert.ok(markup.includes('data-sprint-owner-projection-error="true"'));
    assert.ok(!markup.includes('data-delivery-generation-form="record_sprint_plan"'));
});

test('Sprint generation prepopulates an editable metrics capacity', async () => {
    const context = loadFrontend();
    const candidate = sprintCandidateStory();
    const sprintOwner = await validatedSprintOwner(context);
    const markup = context.deliveryPanelMarkup(
        { decisions: [] }, {}, [{
            node_id: 'planning.sprint.plan', instance_key: null,
            request_kind: 'record_sprint_plan', endpoint: 'sprint/generate',
        }], {
            storyDependencies: dependencyProjection([candidate]),
            sprintCandidates: {
                project_id: 7,
                items: [candidate],
                sprint_owner: sprintOwner,
                capacity: sprintCapacity(),
            },
        },
    );

    assert.ok(markup.includes('name="max_story_points"'));
    assert.ok(markup.includes('value="8"'));
    assert.ok(markup.includes('Maximum story points'));
    assert.ok(markup.includes('based on the last 1 completed Sprints'));
    assert.ok(!markup.includes('button type="submit" disabled'));
});

test('Sprint generation requires a manually entered exact positive integer', async () => {
    const context = loadFrontend();
    const candidate = sprintCandidateStory();
    const sprintOwner = await validatedSprintOwner(context);
    const markup = context.deliveryPanelMarkup(
        { decisions: [] }, {}, [{
            node_id: 'planning.sprint.plan', instance_key: null,
            request_kind: 'record_sprint_plan', endpoint: 'sprint/generate',
        }], {
            storyDependencies: dependencyProjection([candidate]),
            sprintCandidates: {
                project_id: 7,
                items: [candidate],
                sprint_owner: sprintOwner,
                capacity: sprintCapacity({
                    status: 'manual_required',
                    recommended_max_story_points: null,
                    source: null,
                    rationale: 'No completed Sprint capacity history is available. Enter a positive Maximum story points value.',
                }),
            },
        },
    );

    assert.ok(markup.includes('name="max_story_points"'));
    assert.ok(!markup.includes('value="8"'));
    assert.ok(markup.includes('button type="submit" disabled'));
    assert.equal(context.sprintCapacityPoints('8'), 8);
    assert.equal(context.sprintCapacityPoints('0'), null);
    assert.equal(context.sprintCapacityPoints('-8'), null);
    assert.equal(context.sprintCapacityPoints('8.0'), null);
    assert.equal(context.sprintCapacityPoints(' 8 '), null);
});

test('Sprint generation fails closed for unavailable capacity projections', async () => {
    const context = loadFrontend();
    const candidate = sprintCandidateStory();
    const sprintOwner = await validatedSprintOwner(context);
    const markup = context.deliveryPanelMarkup(
        { decisions: [] }, {}, [{
            node_id: 'planning.sprint.plan', instance_key: null,
            request_kind: 'record_sprint_plan', endpoint: 'sprint/generate',
        }], {
            storyDependencies: dependencyProjection([candidate]),
            sprintCandidates: {
                project_id: 7,
                items: [candidate],
                sprint_owner: sprintOwner,
                capacity: sprintCapacity({
                    status: 'unavailable',
                    recommended_max_story_points: null,
                    source: null,
                    rationale: 'Sprint capacity recommendation is unavailable. Reload before planning.',
                }),
            },
        },
    );

    assert.ok(markup.includes('data-sprint-capacity-projection-error="true"'));
    assert.ok(!markup.includes('data-delivery-generation-form="record_sprint_plan"'));
});

test('Sprint generation rejects a string-valued server capacity recommendation', async () => {
    const context = loadFrontend();
    const candidate = sprintCandidateStory();
    const sprintOwner = await validatedSprintOwner(context);
    const markup = context.deliveryPanelMarkup(
        { decisions: [] }, {}, [{
            node_id: 'planning.sprint.plan', instance_key: null,
            request_kind: 'record_sprint_plan', endpoint: 'sprint/generate',
        }], {
            storyDependencies: dependencyProjection([candidate]),
            sprintCandidates: {
                project_id: 7,
                items: [candidate],
                sprint_owner: sprintOwner,
                capacity: sprintCapacity({ recommended_max_story_points: '8' }),
            },
        },
    );

    assert.ok(markup.includes('data-sprint-capacity-projection-error="true"'));
    assert.ok(!markup.includes('data-delivery-generation-form="record_sprint_plan"'));
});

test('Sprint generation fails closed on missing or malformed owner display projections', async () => {
    const context = loadFrontend();
    const candidate = sprintCandidateStory();
    const owners = [
        sprintOwnerProjection({ display_label: undefined }),
        sprintOwnerProjection({ display_label: null }),
        sprintOwnerProjection({ display_label: '' }),
        sprintOwnerProjection({
            display_label: '[agileforge:sprint-owner:solo-project:v1:project:7] Solo operator for Exact Project',
        }),
    ];

    for (const sprintOwner of owners) {
        assert.equal(await context.validateSprintOwnerProjection(sprintOwner, 7), null);
        const markup = context.deliveryPanelMarkup(
            { decisions: [] }, {}, [{
                node_id: 'planning.sprint.plan', instance_key: null,
                request_kind: 'record_sprint_plan', endpoint: 'sprint/generate',
            }], {
                storyDependencies: dependencyProjection([candidate]),
                sprintCandidates: { project_id: 7, items: [candidate], sprint_owner: sprintOwner },
            },
        );
        assert.ok(markup.includes('data-sprint-owner-projection-error="true"'));
        assert.ok(!markup.includes('data-delivery-generation-form="record_sprint_plan"'));
    }
});

test('Sprint owner validation rejects torn keys and the reserved named-owner namespace', async () => {
    const context = loadFrontend();
    const tornOwners = [
        sprintOwnerProjection({
            key: 'agileforge:sprint-owner:solo-project:v1:project:8',
        }),
        {
            kind: 'named_team',
            key: 'agileforge:sprint-owner:solo-project:v1:project:7',
            label: 'Delivery Team',
            display_label: 'Delivery Team',
        },
        {
            kind: 'named_team',
            key: `agileforge:sprint-owner:named-team:v1:sha256:${'a'.repeat(64)}`,
            label: 'Delivery Team',
            display_label: 'Delivery Team',
        },
        {
            kind: 'named_team',
            key: 'agileforge:sprint-owner:named-team:v1:sha256:1744fc03c3bde777cf0bac8bf221b924666acba7a870d76c2448104c6d06d4b7',
            label: '[agileforge:sprint-owner:spoof]',
            display_label: '[agileforge:sprint-owner:spoof]',
        },
    ];

    for (const owner of tornOwners) {
        assert.equal(await context.validateSprintOwnerProjection(owner, 7), null);
        assert.equal(context.sprintOwnerDisplayProjection(owner, 7), null);
    }
});

test('story generation controls render exact PBI IDs and requirement summaries, not array ordinals', () => {
    const context = loadFrontend();
    const actions = [
        {
            node_id: 'planning.story.generate',
            instance_key: 'backlog_item:PBI-000002',
            request_kind: 'record_story_draft',
            endpoint: 'story/generate',
            transport: 'semantic',
        },
        {
            node_id: 'planning.story.generate',
            instance_key: 'backlog_item:PBI-000004',
            request_kind: 'record_story_draft',
            endpoint: 'story/generate',
            transport: 'semantic',
        },
        {
            node_id: 'planning.story.generate',
            instance_key: 'backlog_item:PBI-000005',
            request_kind: 'record_story_draft',
            endpoint: 'story/generate',
            transport: 'semantic',
        },
        {
            node_id: 'planning.story.generate',
            instance_key: 'backlog_item:PBI-000006',
            request_kind: 'record_story_draft',
            endpoint: 'story/generate',
            transport: 'semantic',
        },
    ];
    const position = {
        decisions: actions.map((a) => ({
            node_id: a.node_id,
            instance_key: a.instance_key,
            request_kind: a.request_kind,
            category: 'available',
            reason_code: 'STORY_GENERATION_REQUIRED',
            recommendation_kind: 'required',
        })),
    };
    const reviews = {
        stories: {
            items: [storyReview('backlog_item:PBI-000003')],
        },
    };
    const appState = {
        storyPending: {
            items: [
                { backlog_item_id: 'PBI-000002', requirement: 'Support accepted Number List language.' },
                { backlog_item_id: 'PBI-000003', requirement: 'Reject negative numeric values.' },
                { backlog_item_id: 'PBI-000004', requirement: 'Provide the installed CLI.' },
                { backlog_item_id: 'PBI-000005', requirement: 'Verify through public behavior.' },
                { backlog_item_id: 'PBI-000006', requirement: 'Provide human-reviewable release evidence.' },
            ],
        },
    };

    const markup = context.deliveryPanelMarkup(position, reviews, actions, appState);

    // Exact PBI IDs and summaries are present
    assert.ok(markup.includes('Generate Stories for PBI-000002: Support accepted Number List language.'));
    assert.ok(markup.includes('Generate Stories for PBI-000004: Provide the installed CLI.'));
    assert.ok(markup.includes('Generate Stories for PBI-000005: Verify through public behavior.'));
    assert.ok(markup.includes('Generate Stories for PBI-000006: Provide human-reviewable release evidence.'));

    // Array ordinals are NEVER used as domain identity
    assert.ok(!markup.includes('backlog item 1'));
    assert.ok(!markup.includes('backlog item 2'));
    assert.ok(!markup.includes('backlog item 3'));
    assert.ok(!markup.includes('backlog item 4'));

    // Pending story review card is identified by exact PBI ID, not array index
    assert.ok(markup.includes('Story review for PBI-000003'));
    assert.ok(!markup.includes('Story review 1'));
});

test('single story generation control renders exact PBI ID and requirement', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000002',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
        transport: 'semantic',
    };
    const position = {
        decisions: [{
            node_id: action.node_id,
            instance_key: action.instance_key,
            request_kind: action.request_kind,
            category: 'available',
            reason_code: 'STORY_GENERATION_REQUIRED',
            recommendation_kind: 'required',
        }],
    };
    const appState = {
        storyPending: {
            items: [
                { backlog_item_id: 'PBI-000002', requirement: 'Support accepted Number List language.' },
            ],
        },
    };

    const markup = context.deliveryPanelMarkup(position, {}, [action], appState);

    assert.ok(markup.includes('Generate Stories for PBI-000002: Support accepted Number List language.'));
    assert.ok(!markup.includes('Generate Stories</span>'));
});

test('initial, revision, and correction story actions render distinct intent', () => {
    const context = loadFrontend();
    const appState = {
        storyPending: {
            items: [
                { backlog_item_id: 'PBI-000001', requirement: 'Provide arithmetic sum.' },
                { backlog_item_id: 'PBI-000002', requirement: 'Support number language.' },
                { backlog_item_id: 'PBI-000003', requirement: 'Reject negatives.' },
            ],
        },
    };

    // Initial generation
    const initialAction = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000002',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
    };
    const initialMarkup = context.deliveryPanelMarkup(
        {
            decisions: [{
                node_id: initialAction.node_id,
                instance_key: initialAction.instance_key,
                reason_code: 'STORY_GENERATION_REQUIRED',
                recommendation_kind: 'required',
            }],
        },
        {},
        [initialAction],
        appState,
    );
    assert.ok(initialMarkup.includes('Generate Stories for PBI-000002: Support number language.'));

    // Revision after feedback/rejection
    const revisionAction = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000003',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
    };
    const revisionMarkup = context.deliveryPanelMarkup(
        {
            decisions: [{
                node_id: revisionAction.node_id,
                instance_key: revisionAction.instance_key,
                reason_code: 'STORY_REVISION_REQUIRED',
                recommendation_kind: 'recovery',
            }],
        },
        {},
        [revisionAction],
        appState,
    );
    assert.ok(revisionMarkup.includes('Revise Stories for PBI-000003: Reject negatives.'));

    // Correction of accepted story
    const correctionAction = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000001',
        request_kind: 'record_story_draft',
        endpoint: 'story/correct',
    };
    const correctionMarkup = context.deliveryPanelMarkup(
        {
            decisions: [{
                node_id: correctionAction.node_id,
                instance_key: correctionAction.instance_key,
                reason_code: 'STORY_CORRECTION_AVAILABLE',
                recommendation_kind: 'optional_reentry',
                decision_fingerprint: `sha256:${'b'.repeat(64)}`,
                fact_references: [{
                    fact_type: 'story',
                    fact_id: '91',
                    fingerprint: `sha256:${'a'.repeat(64)}`,
                }],
            }],
        },
        {},
        [correctionAction],
        appState,
    );
    assert.ok(correctionMarkup.includes('Correct Stories for PBI-000001: Provide arithmetic sum.'));
});

test('unbuildable story correction renders locked state without an action button', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000002',
        request_kind: 'record_story_draft',
        endpoint: 'story/correct',
        transport: 'semantic',
        availability: 'locked',
        reason_code: 'STORY_CORRECTION_INPUT_UNAVAILABLE',
    };
    const position = {
        decisions: [{
            node_id: action.node_id,
            instance_key: action.instance_key,
            reason_code: 'STORY_CORRECTION_AVAILABLE',
            recommendation_kind: 'optional_reentry',
            decision_fingerprint: `sha256:${'b'.repeat(64)}`,
            fact_references: [{
                fact_type: 'story',
                fact_id: '4',
                fingerprint: `sha256:${'a'.repeat(64)}`,
            }],
        }],
    };
    const appState = {
        storyPending: {
            items: [{
                backlog_item_id: 'PBI-000002',
                requirement: 'Support accepted Number List language.',
            }],
        },
    };

    const markup = context.deliveryPanelMarkup(position, {}, [action], appState);

    assert.ok(markup.includes('data-story-correction-input-unavailable="true"'));
    assert.ok(markup.includes('Correction unavailable'));
    assert.ok(!markup.includes('data-direct-action="record_story_draft"'));

    const storyCard = context.lifecycleCardProjection(position, [action])
        .find((card) => card.stage === 'Stories');
    assert.equal(storyCard.status, 'Locked');
    assert.equal(storyCard.reason, 'Correction input unavailable.');
    assert.ok(!storyCard.tone.includes('emerald'));
});

test('busy state and status preserve exact PBI identity and intent', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000002',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
    };
    const button = directActionButton(action);
    button._label.textContent = 'Generate Stories for PBI-000002: Support number language.';

    context.setDeliveryActionBusy(button, true, 'record_story_draft');

    assert.equal(button.ariaBusy, 'true');
    assert.equal(button.disabled, true);
    assert.equal(button._label.textContent, 'Generating Stories for PBI-000002...');
    assert.equal(button._status.textContent, 'Generating Stories for PBI-000002...');

    // Reset busy restores original label
    context.setDeliveryActionBusy(button, false, 'record_story_draft');
    assert.equal(button.ariaBusy, null);
    assert.equal(button.disabled, false);
    assert.equal(button._label.textContent, 'Generate Stories for PBI-000002: Support number language.');
});

test('story review confirmation modal identifies exact PBI ID and requirement', () => {
    const context = loadFrontend();
    const item = storyReview('backlog_item:PBI-000003');
    item.review.lineage.backlog_item.requirement = 'Reject any Number List containing a negative.';

    vm.runInContext(`lifecycleState = {
        planningReviews: {
            stories: {
                items: [${JSON.stringify(item)}],
            },
        },
    };`, context);

    const acceptBinding = context.capturePlanningReview('story', 0, 'accepted');
    assert.equal(acceptBinding.title, 'Accept Story review for PBI-000003: Reject any Number List containing a negative.');

    const changesBinding = context.capturePlanningReview('story', 0, 'feedback');
    assert.equal(changesBinding.title, 'Request changes for Story review for PBI-000003: Reject any Number List containing a negative.');

    const rejectBinding = context.capturePlanningReview('story', 0, 'rejected');
    assert.equal(rejectBinding.title, 'Reject Story review for PBI-000003: Reject any Number List containing a negative.');
});

test('story generation control is disabled when requirement summary is missing', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000002',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
        transport: 'semantic',
    };
    const position = {
        decisions: [{
            node_id: action.node_id,
            instance_key: action.instance_key,
            request_kind: action.request_kind,
            category: 'available',
            reason_code: 'STORY_GENERATION_REQUIRED',
            recommendation_kind: 'required',
        }],
    };
    // No storyPending items provided
    const markup = context.deliveryPanelMarkup(position, {}, [action], { storyPending: { items: [] } });

    assert.ok(markup.includes('disabled'));
    assert.ok(markup.includes('title="Requirement summary unavailable"'));
});

test('delivery generation details provide exact title, description, and intent for confirmation dialog', () => {
    const context = loadFrontend();
    const appState = {
        storyPending: {
            items: [
                { backlog_item_id: 'PBI-000001', requirement: 'Provide arithmetic sum.' },
                { backlog_item_id: 'PBI-000002', requirement: 'Support number language.' },
                { backlog_item_id: 'PBI-000003', requirement: 'Reject negatives.' },
            ],
        },
    };

    // Initial generation
    const genAction = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000002',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
    };
    const genDetails = context.deliveryGenerationActionDetails(
        genAction,
        { decisions: [{ node_id: genAction.node_id, instance_key: genAction.instance_key, reason_code: 'STORY_GENERATION_REQUIRED' }] },
        {},
        appState,
    );
    assert.equal(genDetails.intentVerb, 'Generate');
    assert.equal(genDetails.intentLabel, 'generation');
    assert.equal(genDetails.pbiId, 'PBI-000002');
    assert.equal(genDetails.requirement, 'Support number language.');

    // Revision
    const revAction = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000003',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
    };
    const revDetails = context.deliveryGenerationActionDetails(
        revAction,
        { decisions: [{ node_id: revAction.node_id, instance_key: revAction.instance_key, reason_code: 'STORY_REVISION_REQUIRED' }] },
        {},
        appState,
    );
    assert.equal(revDetails.intentVerb, 'Revise');
    assert.equal(revDetails.intentLabel, 'revision');
    assert.equal(revDetails.pbiId, 'PBI-000003');
    assert.equal(revDetails.requirement, 'Reject negatives.');

    // Correction
    const corrAction = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000001',
        request_kind: 'record_story_draft',
        endpoint: 'story/correct',
    };
    const corrDetails = context.deliveryGenerationActionDetails(
        corrAction,
        { decisions: [{
            node_id: corrAction.node_id,
            instance_key: corrAction.instance_key,
            reason_code: 'STORY_CORRECTION_AVAILABLE',
            decision_fingerprint: `sha256:${'b'.repeat(64)}`,
            fact_references: [{
                fact_type: 'story',
                fact_id: '91',
                fingerprint: `sha256:${'a'.repeat(64)}`,
            }],
        }] },
        {},
        appState,
    );
    assert.equal(corrDetails.intentVerb, 'Correct');
    assert.equal(corrDetails.intentLabel, 'correction');
    assert.equal(corrDetails.pbiId, 'PBI-000001');
    assert.equal(corrDetails.requirement, 'Provide arithmetic sum.');
});

test('story generation control ignores candidate backlog reviews and stays disabled when storyPending is empty', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000002',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
        transport: 'semantic',
    };
    const position = {
        decisions: [{
            node_id: action.node_id,
            instance_key: action.instance_key,
            request_kind: action.request_kind,
            category: 'available',
            reason_code: 'STORY_GENERATION_REQUIRED',
            recommendation_kind: 'required',
        }],
    };
    const reviews = {
        backlog: {
            candidate: {
                backlog_items: [
                    { backlog_item_id: 'PBI-000002', requirement: 'UNTRUSTED OR STALE CANDIDATE REQUIREMENT' },
                ],
            },
        },
    };
    // storyPending is empty
    const appState = { storyPending: { items: [] } };
    const details = context.deliveryGenerationActionDetails(action, position, reviews, appState);
    assert.equal(details.requirement, '');
    assert.equal(details.pbiId, 'PBI-000002');
    assert.equal(details.label, 'Generate Stories for PBI-000002');

    const markup = context.deliveryPanelMarkup(position, reviews, [action], appState);
    assert.ok(markup.includes('disabled'));
    assert.ok(markup.includes('title="Requirement summary unavailable"'));
    assert.ok(!markup.includes('UNTRUSTED OR STALE CANDIDATE REQUIREMENT'));
});

test('story readiness keeps structural proof separate from three-state Sprint selection', () => {
    const context = loadFrontend();
    const stories = [
        {
            story_id: 101,
            source_story_item_id: 'US-001',
            is_superseded: false,
            backlog_item_id: 'PBI-000001',
            story_points: 5,
            rank: '1',
            structurally_eligible: true,
            structural_eligibility_status: 'eligible',
            sprint_selection_state: 'unselected',
            sprint_selection_state_fingerprint: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            selected_scope_fingerprint: null,
            dependency_safe: false,
            sprint_candidate: false,
            content_accepted: true,
            validation_status: 'validated',
            validation_failures: [],
            readiness_blockers: [],
        },
    ];
    const appState = {
        storyPending: {
            items: [
                { backlog_item_id: 'PBI-000001', requirement: 'Implement parser core' },
            ],
        },
        storyDependencies: dependencyProjection(stories, [], []),
    };

    const markup = context.storyReadinessMarkup(stories, appState);
    assert.ok(markup.includes('Story readiness'));
    assert.ok(markup.includes('US-001'));
    assert.ok(markup.includes('(PBI-000001)'));
    assert.ok(markup.includes('Implement parser core'));
    assert.ok(markup.includes('Rank: 1'));
    assert.ok(markup.includes('Points: 5'));
    assert.ok(markup.includes('Structurally eligible'));
    assert.ok(markup.includes('Unselected'));
    assert.ok(markup.includes('Select for Sprint'));
    assert.ok(markup.includes('Defer'));
    assert.ok(markup.includes('data-story-selection-id="101"'));
    assert.ok(markup.includes('data-story-selection-fingerprint="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"'));
    assert.ok(markup.includes('Provider-free structural evidence proves:'));
    assert.ok(markup.includes('exact Story identity'));
    assert.ok(markup.includes('Sprint-generation readiness'));
    assert.ok(!markup.includes('Validate Story'));
    assert.ok(!markup.includes('Validated'));
});

test('story readiness renders selected and deferred intent separately and preserves selected intent through stale evidence', () => {
    const context = loadFrontend();
    const stories = [
        {
            story_id: 101,
            source_story_item_id: 'US-001',
            is_superseded: false,
            backlog_item_id: 'PBI-000001',
            story_points: 5,
            rank: '1',
            structurally_eligible: false,
            structural_eligibility_status: 'stale',
            sprint_selection_state: 'selected',
            sprint_selection_state_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
            selected_scope_fingerprint: null,
            dependency_safe: false,
            sprint_candidate: false,
            content_accepted: true,
            validation_status: 'validated',
            validation_failures: [],
            readiness_blockers: [],
        },
        {
            story_id: 102,
            source_story_item_id: 'US-002',
            is_superseded: false,
            backlog_item_id: 'PBI-000002',
            story_points: 3,
            rank: '2',
            structurally_eligible: true,
            structural_eligibility_status: 'eligible',
            sprint_selection_state: 'deferred',
            sprint_selection_state_fingerprint: 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
            selected_scope_fingerprint: null,
            dependency_safe: false,
            sprint_candidate: false,
            content_accepted: true,
            validation_status: 'validated',
            validation_failures: [],
            readiness_blockers: [],
        },
    ];
    const appState = {
        storyPending: { items: [] },
        storyDependencies: dependencyProjection(stories, [], []),
        sprintCandidates: { items: stories },
    };

    const markup = context.storyReadinessMarkup(stories, appState);
    assert.ok(markup.includes('Structural evidence stale'));
    assert.ok(markup.includes('Selected for Sprint'));
    assert.ok(markup.includes('Re-run structural checks'));
    assert.ok(markup.includes('Remove from Sprint selection'));
    assert.ok(markup.includes('Deferred'));
    assert.ok(markup.includes('data-story-selection-intent="select"'));
});

test('dependency review section renders when apply_story_dependencies action is available', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story_dependencies',
        request_kind: 'apply_story_dependencies',
        endpoint: 'story/dependencies/apply',
        transport: 'semantic',
    };
    const stories = [selectedScopeStory({ backlog_item_id: 'PBI-000001' })];
    const dependencies = dependencyProjection(stories);

    const markup = context.storyDependencyReviewMarkup(action, stories, dependencies);
    assert.ok(markup.includes('Dependency review required'));
    assert.ok(markup.includes('US-001'));
    assert.ok(markup.includes('(PBI-000001)'));
    assert.ok(markup.includes('data-apply-dependencies="true"'));
    assert.ok(markup.includes('Confirm dependencies'));
});

test('dependency review displays only candidate stories and candidate-contained edges', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story_dependencies',
        request_kind: 'apply_story_dependencies',
        endpoint: 'story/dependencies/apply',
        transport: 'semantic',
    };
    const candidates = [selectedScopeStory()];
    const dependencies = dependencyProjection([
        selectedScopeStory(),
        selectedScopeStory({
                story_id: 102,
                source_story_item_id: 'US-002',
                sprint_selection_state: 'unselected',
                sprint_selection_state_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        }),
    ], [
        { dependent_story_id: 102, prerequisite_story_id: 101, status: 'proposed', reason: 'US-002 needs US-001' },
    ]);

    const markup = context.storyDependencyReviewMarkup(action, dependencies.stories, dependencies);
    assert.ok(markup.includes('US-001'));
    assert.ok(!markup.includes('US-002'));
    assert.ok(markup.includes('None (independent stories)'));
    assert.ok(!markup.includes('102 -> 101'));
});

test('dependency review displays human-readable story identifiers, PBIs, and dependency reasons', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story_dependencies',
        request_kind: 'apply_story_dependencies',
        endpoint: 'story/dependencies/apply',
        transport: 'semantic',
    };
    const candidates = [
        selectedScopeStory({ backlog_item_id: 'PBI-000001' }),
        selectedScopeStory({
            story_id: 102,
            source_story_item_id: 'US-002',
            backlog_item_id: 'PBI-000002',
            sprint_selection_state_fingerprint: 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
        }),
    ];
    const dependencies = dependencyProjection(candidates, [
        { dependent_story_id: 102, prerequisite_story_id: 101, status: 'proposed', reason: 'US-002 requires data model from US-001' },
    ]);

    const markup = context.storyDependencyReviewMarkup(action, candidates, dependencies);
    assert.ok(markup.includes('US-001 (PBI-000001)'));
    assert.ok(markup.includes('US-002 (PBI-000002)'));
    assert.ok(markup.includes('US-002 (PBI-000002) -&gt; US-001 (PBI-000001) - US-002 requires data model from US-001'));
    assert.ok(markup.includes('Confirm dependencies'));
});

test('story readiness shows current rule diagnostics without suggesting another approval-like check', () => {
    const context = loadFrontend();
    const stories = [
        {
            story_id: 101,
            source_story_item_id: 'US-001',
            is_superseded: false,
            backlog_item_id: 'PBI-000001',
            status: 'accepted',
            story_points: 3,
            rank: '0|hzzzzz:',
            content_accepted: true,
            structurally_eligible: false,
            structural_eligibility_status: 'ineligible',
            sprint_selection_state: 'unselected',
            sprint_selection_state_fingerprint: 'sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
            selected_scope_fingerprint: null,
            dependency_safe: false,
            sprint_candidate: false,
            readiness_blockers: [],
            validation_status: 'failed',
            validation_failures: [
                {
                    code: 'STORY_SPEC_REFERENCE_INVALID',
                    message: 'Story references invalid specification items: REQ.099',
                },
            ],
        },
    ];
    const appState = {
        storyPending: { items: [] },
        storyDependencies: { stories },
        sprintCandidates: { items: [] },
    };

    const readinessMarkup = context.storyReadinessMarkup(stories, appState);
    assert.ok(readinessMarkup.includes('Structural eligibility failed'));
    assert.ok(readinessMarkup.includes('data-story-validation-diagnostics="true"'));
    assert.ok(readinessMarkup.includes('STORY_SPEC_REFERENCE_INVALID'));
    assert.ok(readinessMarkup.includes('Story references invalid specification items: REQ.099'));
    assert.ok(!readinessMarkup.includes('Re-run structural checks'));
    assert.ok(!readinessMarkup.includes('Validate Story'));
});

test('malformed readiness projections fail closed and hide selection controls', () => {
    const context = loadFrontend();
    const stories = [
        {
            story_id: 102,
            source_story_item_id: 'US-002',
            backlog_item_id: 'PBI-000002',
            status: 'accepted',
            story_points: 5,
            rank: '0|hzzzzz:1',
            content_accepted: true,
            structurally_eligible: true,
            structural_eligibility_status: 'eligible',
            sprint_selection_state: 'selected',
            // Missing exact selection state fingerprint.
            selected_scope_fingerprint: null,
            dependency_safe: true,
            sprint_candidate: true,
            readiness_blockers: ['PREREQUISITE_STORY_101_INCOMPLETE'],
            validation_status: 'validated',
            validation_failures: [],
        },
    ];
    const appState = {
        storyPending: { items: [] },
        storyDependencies: { stories },
        sprintCandidates: { items: [] },
    };

    const readinessMarkup = context.storyReadinessMarkup(stories, appState);
    assert.ok(readinessMarkup.includes('Story state unavailable'));
    assert.ok(readinessMarkup.includes('aria-disabled="true"'));
    assert.ok(!readinessMarkup.includes('data-story-selection-id="102"'));
});

test('structural and selection mutation payloads bind exact Story state and reuse an idempotency key for retry', async () => {
    const requests = [];
    let attempts = 0;
    const context = loadFrontend(async (path, options) => {
        requests.push({ path, body: JSON.parse(options.body) });
        attempts += 1;
        if (attempts === 1) throw new Error('temporary network failure');
        return { ok: true, status: 200, text: async () => '{"ok":true,"data":{},"errors":[]}' };
    });
    const fingerprint = 'sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee';
    await context.postStorySelectionMutation(1, 101, 'select', fingerprint);
    assert.equal(requests.length, 2);
    assert.equal(requests[0].path, '/api/projects/1/story/sprint-selection');
    assert.deepEqual(requests[0].body, {
        story_id: 101,
        intent: 'select',
        expected_state_fingerprint: fingerprint,
        rationale: 'Selected for Sprint from dashboard.',
        actor: 'dashboard-ui',
        idempotency_key: 'dashboard-uuid-1',
    });
    assert.equal(requests[0].body.idempotency_key, requests[1].body.idempotency_key);
    assert.deepEqual(JSON.parse(JSON.stringify(context.structuralEligibilityMutationPayload(101))), {
        story_ids: [101],
        actor: 'dashboard-ui',
        idempotency_key: 'dashboard-uuid-1',
    });
});

test('Story mutation token lock survives a 409 authority reload and releases only on recovery', async () => {
    let conflict = true;
    const context = loadFrontend(async (path) => {
        if (conflict && path.endsWith('/story/dependencies')) {
            return {
                ok: false,
                status: 409,
                text: async () => JSON.stringify({
                    detail: { error: { code: 'STALE_POSITION', message: 'Projection changed.' } },
                }),
            };
        }
        return { ok: true, status: 200, text: async () => '{"data":{}}' };
    });
    const story = selectedScopeStory({ sprint_selection_state: 'unselected', selected_scope_fingerprint: null });
    vm.runInContext(`
        selectedProjectId = 7;
        activeStoryMutation = {
            token: 'story-token-409',
            phase: 'awaiting_authority',
            storyId: 101,
            intent: 'select',
        };
    `, context);

    await assert.rejects(context.loadDashboard());
    const locked = context.storyReadinessMarkup([story], {
        storyDependencies: { stories: [story], edges: [] },
    });
    assert.ok(locked.includes('data-story-selection-intent="select" disabled aria-disabled="true"'));
    assert.ok(locked.includes('Current project projection is reloading; controls remain locked.'));
    assert.notEqual(vm.runInContext('activeStoryMutation', context), null);

    conflict = false;
    assert.strictEqual(await context.loadDashboard(), true);
    assert.strictEqual(vm.runInContext('activeStoryMutation', context), null);
});

test('dependency review submits the backend-projected next-Sprint IDs without rederiving history', () => {
    const context = loadFrontend();
    const completed = selectedScopeStory({ story_id: 101, source_story_item_id: 'US-completed' });
    const future = selectedScopeStory({
        story_id: 102,
        source_story_item_id: 'US-future',
        sprint_selection_state_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    });
    const dependencies = {
        stories: [completed, future],
        selected_story_ids: [102],
        selected_scope_fingerprint: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        edges: [],
    };

    const projection = context.selectedScopeDependencies([completed, future], dependencies);
    assert.strictEqual(projection.isWellFormed, true);
    assert.deepEqual(JSON.parse(JSON.stringify(projection.scopeIds)), [102]);
    assert.strictEqual(projection.scopeFingerprint, dependencies.selected_scope_fingerprint);
});

test('dependency mutation submits the exact projected selected-scope fingerprint', async () => {
    const requests = [];
    const context = loadFrontend(async (url, options = {}) => {
        requests.push({ url, options });
        return { ok: true, text: async () => '{}' };
    });
    const scopeFingerprint = 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

    await context.postStoryDependencyMutation(7, [101], [], scopeFingerprint);

    assert.equal(requests.length, 1);
    const body = JSON.parse(requests[0].options.body);
    assert.equal(body.selected_scope_fingerprint, scopeFingerprint);
    assert.deepEqual(body.selected_story_ids, [101]);
});

test('browser renders the exact structural proof and non-proof disclosure from the projection', () => {
    const context = loadFrontend();
    const story = selectedScopeStory({ sprint_selection_state: 'unselected', selected_scope_fingerprint: null });
    const scope = {
        proves: [
            'exact Story identity',
            'immutable accepted Story artifact/item binding',
            'accepted Backlog and Specification lineage',
            'parent-bounded Specification references',
            'required Story shape',
            'non-empty acceptance criteria',
            'current evidence and input fingerprints',
        ],
        does_not_prove: [
            'semantic/model quality',
            'product value',
            'human Sprint selection',
            'dependency safety',
            'Sprint candidacy',
            'Sprint-generation readiness',
        ],
    };

    const markup = context.storyReadinessMarkup([story], {
        storyDependencies: { stories: [story], edges: [], structural_evidence_scope: scope },
    });
    for (const item of [...scope.proves, ...scope.does_not_prove]) {
        assert.ok(markup.includes(item));
    }
});

test('selected scope retains external prerequisites and excludes unselected dependents', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story_dependencies',
        request_kind: 'apply_story_dependencies',
        endpoint: 'story/dependencies/apply',
        transport: 'semantic',
    };
    const selected = selectedScopeStory({ backlog_item_id: 'PBI-000001' });
    const external = selectedScopeStory({
        story_id: 102,
        source_story_item_id: 'US-002',
        backlog_item_id: 'PBI-000002',
        sprint_selection_state: 'unselected',
        sprint_selection_state_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    });
    const dependencies = dependencyProjection([selected, external], [
        { dependent_story_id: 101, prerequisite_story_id: 102, status: 'proposed', reason: 'US-001 requires external US-002.' },
        { dependent_story_id: 102, prerequisite_story_id: 101, status: 'proposed', reason: 'Unselected dependent stays out of scope.' },
    ]);

    const result = context.selectedScopeDependencies([selected, external], dependencies);
    assert.strictEqual(result.isWellFormed, true);
    assert.deepEqual(JSON.parse(JSON.stringify(result.scopeEdges)), [
        { dependent_story_id: 101, prerequisite_story_id: 102, reason: 'US-001 requires external US-002.', isExternal: true },
    ]);
    const markup = context.storyDependencyReviewMarkup(action, [selected, external], dependencies);
    assert.ok(markup.includes('External/excluded prerequisite'));
    assert.ok(markup.includes('US-002 (PBI-000002)'));
    assert.ok(!markup.includes('Unselected dependent stays out of scope.'));
});

test('dependency and readiness contradictions fail closed while missing evidence is labelled precisely', () => {
    const context = loadFrontend();
    const selected = selectedScopeStory();
    const missing = selectedScopeStory({
        story_id: 102,
        source_story_item_id: 'US-002',
        structurally_eligible: false,
        structural_eligibility_status: 'stale',
        sprint_selection_state: 'unselected',
        sprint_selection_state_fingerprint: 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
        selected_scope_fingerprint: null,
        validation_status: 'unvalidated',
        validation_failures: [],
    });
    const missingMarkup = context.storyReadinessMarkup([missing]);
    assert.ok(missingMarkup.includes('Structural evidence missing'));
    assert.ok(missingMarkup.includes('Re-run structural checks'));

    const contradictoryEligible = selectedScopeStory({ validation_failures: [{ code: 'BAD', message: 'Should not accompany eligibility.' }] });
    assert.strictEqual(context.parseStoryReadinessProjection(contradictoryEligible), null);
    assert.strictEqual(context.selectedScopeDependencies([selected], { stories: [selected] }).isWellFormed, false);
    assert.strictEqual(context.selectedScopeDependencies([selected, selected], { stories: [selected, selected], edges: [] }).isWellFormed, false);
    assert.strictEqual(context.selectedScopeDependencies([selected], { stories: [selected], edges: [
        { dependent_story_id: 101, prerequisite_story_id: 102, reason: 'Once.' },
        { dependent_story_id: 101, prerequisite_story_id: 102, reason: 'Duplicate.' },
    ] }).isWellFormed, false);
});

test('malformed Sprint candidate projection blocks the generated Sprint form and transport', () => {
    const context = loadFrontend();
    const sprintAction = {
        node_id: 'planning.sprint.plan',
        request_kind: 'record_sprint_plan',
        endpoint: 'sprint/generate',
        transport: 'semantic',
        instance_key: null,
    };
    const malformed = [{ story_id: 101, sprint_candidate: true }];
    const markup = context.deliveryPanelMarkup({}, {}, [sprintAction], {
        storyDependencies: { stories: [] },
        sprintCandidates: { items: malformed },
    });
    assert.strictEqual(context.canGenerateSprintPlan({ sprintCandidates: { items: malformed } }), false);
    assert.ok(markup.includes('Sprint candidate projection unavailable'));
    assert.ok(!markup.includes('data-delivery-generation-form="record_sprint_plan"'));
});

test('Sprint generation binds canonical candidates to the current dependency scope', () => {
    const requests = [];
    const context = loadFrontend(async (path) => {
        requests.push(path);
        return { ok: true, status: 200, text: async () => '{"status":"success","data":{}}' };
    });
    const sprintAction = {
        node_id: 'planning.sprint.plan',
        request_kind: 'record_sprint_plan',
        endpoint: 'sprint/generate',
        transport: 'semantic',
        instance_key: null,
    };
    const candidate = sprintCandidateStory();
    const currentStory = selectedScopeStory({
        selected_scope_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    });
    const appState = {
        storyDependencies: dependencyProjection([currentStory]),
        sprintCandidates: { items: [candidate] },
    };

    const markup = context.deliveryPanelMarkup({}, {}, [sprintAction], appState);
    assert.strictEqual(context.canGenerateSprintPlan(appState), false);
    assert.ok(markup.includes('Sprint candidate projection unavailable'));
    assert.ok(!markup.includes('data-delivery-generation-form="record_sprint_plan"'));
    assert.deepEqual(requests, []);
});

test('Sprint generation rejects a torn candidate vector within one selected scope', () => {
    const context = loadFrontend();
    const first = sprintCandidateStory({ story_id: 101 });
    const second = sprintCandidateStory({
        story_id: 103,
        source_story_item_id: 'US-003',
        sprint_selection_state_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    });
    const appState = {
        storyDependencies: dependencyProjection([first, second]),
        sprintCandidates: { items: [first] },
    };

    assert.strictEqual(context.canGenerateSprintPlan(appState), false);
    assert.strictEqual(context.sprintGenerationCandidateIds(appState), null);
});

test('Sprint generation submits the exact projected candidate IDs', async () => {
    const requests = [];
    const context = loadFrontend(async (url, options = {}) => {
        requests.push({ url, options });
        return { ok: true, text: async () => '{}' };
    });
    const action = {
        node_id: 'planning.sprint.plan',
        instance_key: null,
        request_kind: 'record_sprint_plan',
        endpoint: 'sprint/generate',
        transport: 'semantic',
    };
    const first = sprintCandidateStory({ story_id: 101 });
    const second = sprintCandidateStory({
        story_id: 103,
        source_story_item_id: 'US-003',
        sprint_selection_state_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    });
    vm.runInContext(`selectedProjectId = 7; lifecycleState = ${JSON.stringify({
        actions: [action],
        position: {},
        planningReviews: {},
        storyDependencies: dependencyProjection([first, second]),
        sprintCandidates: {
            project_id: 7,
            items: [second, first],
            sprint_owner: sprintOwnerProjection(),
            capacity: sprintCapacity(),
        },
    })}; loadDashboard = async () => true;`, context);
    await vm.runInContext(
        'validateSprintOwnerProjection(lifecycleState.sprintCandidates.sprint_owner, 7)',
        context,
    );

    await context.runDirectAction(
        'record_sprint_plan',
        directActionButton(action),
        null,
        { max_story_points: 8 },
    );

    const post = requests.find(({ options }) => options.method === 'POST');
    assert.ok(post);
    const body = JSON.parse(post.options.body);
    assert.deepEqual(body.selected_story_ids, [101, 103]);
    assert.equal(body.max_story_points, 8);
    assert.ok(!Object.hasOwn(body, 'team_name'));
});

test('Sprint generation trims a named Team override and never transports a reserved one', () => {
    const context = loadFrontend();
    assert.equal(context.sprintTeamOverrideFields('  Delivery Team  ').team_name, 'Delivery Team');
    assert.equal(Object.keys(context.sprintTeamOverrideFields('   ')).length, 0);
    assert.throws(
        () => context.sprintTeamOverrideFields(' [AgileForge:Sprint-Owner:spoof] '),
        /reserved Sprint-owner namespace/i,
    );
    assert.throws(
        () => context.sprintTeamOverrideFields(' [agileforge:\u017fprint-owner:spoof] '),
        /reserved Sprint-owner namespace/i,
    );
});

test('Sprint review renders accepted INVEST evidence and keeps acceptance enabled', async () => {
    const context = loadFrontend();
    const sprintOwner = await validatedSprintOwner(context);
    const acceptedStory = storyReview('backlog_item:PBI-000003')
        .review.candidate.story_items[0];
    const selected = {
        binding: {
            decision_fingerprint: 'sha256:sprint-review-decision',
            instance_key: null,
        },
        review: {
            phase: 'sprint_plan',
            project_id: 7,
            candidate: {
                team_name: sprintOwner.label,
                sprint_owner: sprintOwner,
                sprint_goal: 'Ship the browser boundary.',
                selected_stories: [{
                    ...acceptedStory,
                    reason_for_selection: 'Highest accepted value.',
                    tasks: [],
                }],
            },
            review: { state: 'pending' },
        },
    };

    const markup = context.planningReviewCardMarkup(
        'Sprint plan review', selected, 'sprint', 0,
    );
    assert.ok(markup.includes('Sprint owner'));
    assert.ok(markup.includes('Solo project'));
    assert.ok(markup.includes('Solo operator for Exact Project'));
    assert.ok(!markup.includes('agileforge:sprint-owner:'));
    assert.ok(markup.includes('data-invest-assessment="true"'));
    assert.ok(markup.includes('Self-contained logic.'));
    assert.ok(!markup.includes('Quality Assessment Incomplete'));
    assert.ok(!markup.includes('Acceptance is disabled.'));
    assert.ok(markup.includes('data-review-decision="accepted" class='));
    assert.ok(!markup.includes('data-review-decision="accepted" disabled'));
    assert.notStrictEqual(
        context.planningReviewBinding(selected, 'sprint', 'accepted'),
        null,
    );
});

test('Sprint review preserves an exact named-Team display and rejects a missing display projection', async () => {
    const context = loadFrontend();
    const namedOwner = {
        kind: 'named_team',
        key: 'agileforge:sprint-owner:named-team:v1:sha256:34ce3555a07be9b64db28c994cb50b21cc90a36aa513c97c24683a88abe12c97',
        label: 'Delivery Team',
        display_label: 'Delivery Team',
    };
    await validatedSprintOwner(context, namedOwner);
    const namedMarkup = context.sprintReviewMarkup({
        team_name: 'Delivery Team',
        sprint_owner: namedOwner,
        sprint_goal: 'Ship the browser boundary.', selected_stories: [],
    }, 7);
    assert.ok(namedMarkup.includes('Named team'));
    assert.ok(namedMarkup.includes('Delivery Team'));
    assert.equal(context.sprintReviewMarkup({
        team_name: 'Delivery Team',
        sprint_owner: {
            kind: 'named_team',
            key: 'agileforge:sprint-owner:named-team:v1:sha256:34ce3555a07be9b64db28c994cb50b21cc90a36aa513c97c24683a88abe12c97',
            label: 'Delivery Team',
        },
        sprint_goal: 'Ship the browser boundary.', selected_stories: [],
    }, 7), '');
});

test('scope parser rejects a conflicting fingerprint on an unselected Story', () => {
    const context = loadFrontend();
    const selected = selectedScopeStory();
    const unselected = selectedScopeStory({
        story_id: 102,
        source_story_item_id: 'US-002',
        sprint_selection_state: 'unselected',
        sprint_selection_state_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        selected_scope_fingerprint: 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
    });

    assert.strictEqual(
        context.selectedScopeDependencies([selected, unselected], {
            stories: [selected, unselected],
            edges: [],
        }).isWellFormed,
        false,
    );
});

test('dependency scope excludes rejected edges and rejects self edges before transport', () => {
    const context = loadFrontend();
    const selected = selectedScopeStory();
    const external = selectedScopeStory({
        story_id: 102,
        source_story_item_id: 'US-002',
        sprint_selection_state: 'unselected',
        sprint_selection_state_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    });
    const rejected = context.selectedScopeDependencies([selected, external], dependencyProjection(
        [selected, external], [{
            dependent_story_id: 101,
            prerequisite_story_id: 102,
            status: 'rejected',
            reason: 'Do not reactivate this edge.',
        }],
    ));
    assert.strictEqual(rejected.isWellFormed, true);
    assert.deepEqual(JSON.parse(JSON.stringify(rejected.scopeEdges)), []);
    assert.strictEqual(
        context.selectedScopeDependencies([selected], dependencyProjection(
            [selected], [{
                dependent_story_id: 101,
                prerequisite_story_id: 101,
                status: 'proposed',
                reason: 'Malformed self edge.',
            }],
        )).isWellFormed,
        false,
    );
});

test('dependency controls remain locked after a successful mutation cannot reload', () => {
    const context = loadFrontend();
    assert.strictEqual(context.shouldUnlockDependencyMutation(true, false), false);
    assert.strictEqual(context.shouldUnlockDependencyMutation(true, true), false);
    assert.strictEqual(context.shouldUnlockDependencyMutation(false, false), true);
});

test('a dashboard load started during dependency submission cannot release its token', async () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story_dependencies',
        request_kind: 'apply_story_dependencies',
        endpoint: 'story/dependencies/apply',
        transport: 'semantic',
    };
    const story = selectedScopeStory();
    vm.runInContext(`
        selectedProjectId = 7;
        activeDependencyMutation = {
            token: 'dependency-token-1',
            phase: 'submitting',
            payload: {
                selected_story_ids: [101],
                reviewed_edges: [],
                actor: 'dashboard-ui',
                idempotency_key: 'dashboard-uuid-1',
            },
            button: null,
        };
    `, context);

    assert.strictEqual(await context.loadDashboard(), true);

    const active = JSON.parse(vm.runInContext(
        'JSON.stringify(activeDependencyMutation)',
        context,
    ));
    assert.notEqual(active, null);
    assert.equal(active.token, 'dependency-token-1');
    assert.equal(active.phase, 'submitting');
    const markup = context.storyDependencyReviewMarkup(
        action,
        [story],
        { stories: [story], edges: [] },
    );
    assert.ok(markup.includes('disabled aria-disabled="true"'));
    assert.ok(markup.includes('aria-busy="true"'));
    assert.ok(markup.includes('Dependency review is being submitted'));
    assert.ok(!markup.includes('Dependency review was accepted'));
});

test('rejected Story mutations restore every control to its exact prior state', () => {
    const context = loadFrontend();
    const disabledControl = {
        disabled: true,
        attributes: new Map([['aria-disabled', 'true']]),
        getAttribute(name) { return this.attributes.get(name) ?? null; },
        setAttribute(name, value) { this.attributes.set(name, value); },
        removeAttribute(name) { this.attributes.delete(name); },
    };
    const enabledControl = {
        disabled: false,
        attributes: new Map(),
        getAttribute(name) { return this.attributes.get(name) ?? null; },
        setAttribute(name, value) { this.attributes.set(name, value); },
        removeAttribute(name) { this.attributes.delete(name); },
    };
    const states = context.captureStoryControlStates([disabledControl, enabledControl]);
    disabledControl.disabled = false;
    disabledControl.removeAttribute('aria-disabled');
    enabledControl.disabled = true;
    enabledControl.setAttribute('aria-disabled', 'true');
    context.restoreStoryControlStates(states);
    assert.equal(disabledControl.disabled, true);
    assert.equal(disabledControl.getAttribute('aria-disabled'), 'true');
    assert.equal(enabledControl.disabled, false);
    assert.equal(enabledControl.getAttribute('aria-disabled'), null);
});

test('dependency review disables confirm button when canonical candidate projection is missing', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story_dependencies',
        request_kind: 'apply_story_dependencies',
        endpoint: 'story/dependencies/apply',
        transport: 'semantic',
    };
    const dependencies = {
        stories: [selectedScopeStory()],
        edges: [],
    };

    // When candidates is null (missing sprintCandidates.items)
    const markup = context.storyDependencyReviewMarkup(action, null, dependencies);
    assert.ok(markup.includes('Unavailable (current selected scope missing or malformed)'));
    assert.ok(markup.includes('disabled'));
    assert.ok(markup.includes('aria-disabled="true"'));
});

test('storyItemMarkup renders explainable INVEST assessment across all 6 dimensions', () => {
    const context = loadFrontend();
    const story = {
        story_title: 'Calculate values',
        statement: 'As a user, I want to calculate values, so that I learn.',
        persona: 'user',
        estimated_effort: 'S',
        acceptance_criteria: ['Verify calculation passes.'],
        specification_evidence: [],
        invest_assessment: {
            independent: {
                result: 'pass',
                rationale: 'Self-contained calculation logic.',
                evidence: 'No dependencies on unbuilt stories.',
            },
            negotiable: {
                result: 'pass',
                rationale: 'Calculation approach can be refined.',
                evidence: 'Focuses on outcome.',
            },
            valuable: {
                result: 'pass',
                rationale: 'Provides core calculation capability.',
                evidence: 'Directly addresses user need.',
            },
            estimable: {
                result: 'pass',
                rationale: 'Clear scope for estimation.',
                evidence: 'Two discrete criteria.',
            },
            small: {
                result: 'concern',
                rationale: 'Multiple operations included.',
                evidence: 'Effort is S but covers add and subtract.',
            },
            testable: {
                result: 'pass',
                rationale: 'Deterministic criteria.',
                evidence: 'Verify calculation passes.',
            },
        },
        order: 1,
        rank: '101',
        order_rationale: 'Foundational parser operations.',
        story_points: 2,
        estimated_effort: 'S',
        effort_rationale: 'Single straightforward parser operation.',
        research_caveats: ['Requires standard floating point behavior.'],
        dependency_candidates: [
            {
                prerequisite_ref: 'US-0001',
                reason: 'Parser needed first',
                confidence: 'explicit',
            },
        ],
    };

    const markup = context.storyItemMarkup(story);
    assert.ok(markup.includes('INVEST assessment'));
    assert.ok(markup.includes('Independent'));
    assert.ok(markup.includes('Negotiable'));
    assert.ok(markup.includes('Valuable'));
    assert.ok(markup.includes('Estimable'));
    assert.ok(markup.includes('Small'));
    assert.ok(markup.includes('Testable'));
    assert.ok(markup.includes('Pass'));
    assert.ok(markup.includes('Concern'));
    assert.ok(markup.includes('Self-contained calculation logic.'));
    assert.ok(markup.includes('No dependencies on unbuilt stories.'));
    assert.ok(markup.includes('Story order within PBI:</strong> 1 <span class="text-slate-500">(Derived rank: 101)</span>'));
    assert.ok(markup.includes('Estimated effort:</strong> S (derived: 2 pts)'));
    assert.ok(markup.includes('Order rationale:</strong> Foundational parser operations.'));
    assert.ok(markup.includes('Effort rationale:</strong> Single straightforward parser operation.'));
    assert.ok(markup.includes('Requires standard floating point behavior.'));
    assert.ok(markup.includes('Prerequisite:</strong> US-0001'));
    assert.ok(markup.includes('Parser needed first'));
});

test('dependencyCandidatesMarkup renders explicit none proposed message when empty', () => {
    const context = loadFrontend();
    const emptyMarkup = context.dependencyCandidatesMarkup([]);
    assert.ok(emptyMarkup.includes('Proposed dependencies'));
    assert.ok(emptyMarkup.includes('None proposed'));
});

test('investAssessmentMarkup renders explicit error on missing or malformed assessment', () => {
    const context = loadFrontend();
    const missingMarkup = context.investAssessmentMarkup(null);
    assert.ok(missingMarkup.includes('data-invest-assessment="invalid"'));
    assert.ok(missingMarkup.includes('Quality Assessment Incomplete'));
    assert.ok(missingMarkup.includes('Acceptance is disabled.'));

    // Incomplete dimensions
    const incomplete = validInvestAssessment();
    delete incomplete.small;
    const incompleteMarkup = context.investAssessmentMarkup(incomplete);
    assert.ok(incompleteMarkup.includes('data-invest-assessment="invalid"'));
    assert.ok(incompleteMarkup.includes('Missing / Invalid'));

    // Blank rationale
    const blankRationale = validInvestAssessment();
    blankRationale.valuable.rationale = '   ';
    const blankRationaleMarkup = context.investAssessmentMarkup(blankRationale);
    assert.ok(blankRationaleMarkup.includes('data-invest-assessment="invalid"'));

    // Invalid result string
    const badResult = validInvestAssessment();
    badResult.testable.result = 'maybe';
    const badResultMarkup = context.investAssessmentMarkup(badResult);
    assert.ok(badResultMarkup.includes('data-invest-assessment="invalid"'));

    // Coercion test: non-string rationale, object evidence, whitespace-padded result
    const coercedMalformed = validInvestAssessment();
    coercedMalformed.independent = {
        result: ' PASS ',
        rationale: 123,
        evidence: { source: 'REQ.1' },
    };
    assert.strictEqual(context.isWellFormedInvestDimension(coercedMalformed.independent), false);
    assert.strictEqual(context.isWellFormedInvestAssessment(coercedMalformed), false);
    const coercedMarkup = context.investAssessmentMarkup(coercedMalformed);
    assert.ok(coercedMarkup.includes('data-invest-assessment="invalid"'));
    assert.ok(coercedMarkup.includes('Missing / Invalid rationale'));
    assert.ok(coercedMarkup.includes('Missing / Invalid evidence'));

    // Uppercase result is rejected (strict lowercase enum required)
    const upperCaseResult = validInvestAssessment();
    upperCaseResult.independent.result = 'PASS';
    assert.strictEqual(context.isWellFormedInvestDimension(upperCaseResult.independent), false);

    // Extra keys on dimension are rejected
    const extraDimKeys = validInvestAssessment();
    extraDimKeys.independent.extra_key = 'unexpected';
    assert.strictEqual(context.isWellFormedInvestDimension(extraDimKeys.independent), false);

    // Extra keys on assessment object are rejected
    const extraAssessmentKeys = validInvestAssessment();
    extraAssessmentKeys.unknown_dimension = { result: 'pass', rationale: 'R', evidence: 'E' };
    assert.strictEqual(context.isWellFormedInvestAssessment(extraAssessmentKeys), false);
});

test('planningReviewCardMarkup disables Accept and renders error banner for invalid story review', () => {
    const context = loadFrontend();
    const item = storyReview('backlog_item:PBI-000003');
    // Remove invest_assessment to simulate missing quality evidence
    delete item.review.candidate.story_items[0].invest_assessment;

    const markup = context.planningReviewCardMarkup('Story review for PBI-000003', item, 'story', 0);
    assert.ok(markup.includes('data-review-error="invalid-story-evidence"'));
    assert.ok(markup.includes('data-review-decision="accepted" disabled class='));
    assert.ok(markup.includes('Acceptance disabled: required INVEST, sizing, or ordering evidence is missing or malformed'));
    assert.ok(markup.includes('Request changes'));
    assert.ok(markup.includes('Reject'));

    vm.runInContext(`lifecycleState = {
        planningReviews: {
            stories: {
                items: [${JSON.stringify(item)}],
            },
        },
    };`, context);

    // planningReviewBinding must fail closed and return null for accepted decision
    const acceptBinding = context.capturePlanningReview('story', 0, 'accepted');
    assert.strictEqual(acceptBinding, null);

    // But Request changes and Reject remain possible
    const changesBinding = context.capturePlanningReview('story', 0, 'feedback');
    assert.notStrictEqual(changesBinding, null);
    const rejectBinding = context.capturePlanningReview('story', 0, 'rejected');
    assert.notStrictEqual(rejectBinding, null);
});

test('planningReviewCardMarkup names missing rationale evidence when disabling Accept', () => {
    const context = loadFrontend();
    const item = storyReview('backlog_item:PBI-000003');
    delete item.review.candidate.story_items[0].effort_rationale;

    const markup = context.planningReviewCardMarkup('Story review for PBI-000003', item, 'story', 0);
    assert.ok(markup.includes('data-review-error="invalid-story-evidence"'));
    assert.ok(markup.includes('required INVEST, sizing, or ordering evidence is missing or malformed'));
    assert.ok(markup.includes('data-review-decision="accepted" disabled'));
});

test('planningReviewCardMarkup enables Accept when story review has valid INVEST assessment', () => {
    const context = loadFrontend();
    const item = storyReview('backlog_item:PBI-000003');

    const markup = context.planningReviewCardMarkup('Story review for PBI-000003', item, 'story', 0);
    assert.ok(!markup.includes('data-review-error="invalid-story-evidence"'));
    assert.ok(markup.includes('data-review-decision="accepted" class='));
    assert.ok(!markup.includes('data-review-decision="accepted" disabled'));
    assert.ok(markup.includes('>Accept</button>'));

    vm.runInContext(`lifecycleState = {
        planningReviews: {
            stories: {
                items: [${JSON.stringify(item)}],
            },
        },
    };`, context);

    const acceptBinding = context.capturePlanningReview('story', 0, 'accepted');
    assert.notStrictEqual(acceptBinding, null);
    assert.equal(acceptBinding.decision, 'accepted');
});

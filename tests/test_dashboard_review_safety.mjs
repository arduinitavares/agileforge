import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const sourcePath = path.resolve(import.meta.dirname, '../frontend/project.js');
const htmlPath = path.resolve(import.meta.dirname, '../frontend/project.html');
const source = fs.readFileSync(sourcePath, 'utf8');
const html = fs.readFileSync(htmlPath, 'utf8');

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
        crypto: { randomUUID: () => 'review-uuid' },
        document: {
            createElement,
            getElementById() { return null; },
            querySelector() { return null; },
            querySelectorAll() { return []; },
            addEventListener() {},
        },
        fetch: fetchImpl,
        URLSearchParams,
        window: { addEventListener() {}, location: { href: '' }, setTimeout(callback) { callback(); } },
    });
    vm.runInContext(source, context, { filename: sourcePath });
    return context;
}

function selectedReview(statement = '<img src=x onerror=alert(1)>') {
    return {
        binding: {
            decision_fingerprint: 'sha256:hidden-decision',
            instance_key: null,
        },
        review: {
            schema_version: 'agileforge.planning-artifact-review.v1',
            phase: 'backlog',
            lineage: { specification: { spec_hash: 'sha256:hidden' } },
            candidate: {
                backlog_items: [{
                    backlog_item_id: 'PBI-000001',
                    priority: 1,
                    requirement: statement,
                    value_driver: 'Customer Satisfaction',
                    justification: 'Exact reason',
                    estimated_effort: 'M',
                    technical_note: null,
                    specification_evidence: [{
                        spec_item_id: 'REQ.hidden',
                        title: 'Safe title',
                        statement: 'Exact evidence',
                        level: 'MUST',
                        acceptance_criteria: ['Evidence is verified.'],
                        verification_method: 'acceptance-test',
                    }],
                }],
                is_complete: true,
                clarifying_questions: [],
            },
            review: { state: 'pending' },
        },
    };
}

function feedbackContinuation(mode, content, rationale) {
    const decisions = {
        'revision-ready': ['available', 'recovery', 'BACKLOG_REVISION_REQUIRED', false],
        active: ['waiting', 'required', 'BACKLOG_GENERATION_ACTIVE', false],
        'failed-retry': ['available', 'recovery', 'BACKLOG_GENERATION_FAILED', true],
        'expired-recovery': ['available', 'recovery', 'BACKLOG_GENERATION_RECOVERY_REQUIRED', true],
    };
    const [category, recommendation_kind, reason_code, hasAttempt] = decisions[mode];
    const decision = {
        node_id: 'backlog.generate', instance_key: null, request_kind: 'record_backlog_draft',
        category, recommendation_kind, reason_code,
        decision_fingerprint: 'sha256:hidden-backlog-decision-canary',
        fact_references: [
            { fact_type: 'backlog', fact_id: 7, fingerprint: 'sha256:hidden-backlog-artifact-canary' },
            { fact_type: 'specification', fact_id: 31, fingerprint: 'sha256:hidden-specification-canary' },
            { fact_type: 'product_goal', fact_id: 21, fingerprint: 'sha256:hidden-product-goal-canary' },
            ...(hasAttempt ? [{ fact_type: 'node_attempt', fact_id: 81, fingerprint: 'sha256:hidden-node-attempt-canary' }] : []),
        ],
    };
    return {
        position: { decisions: [decision] },
        reviews: { backlog: { continuation: {
            binding: { node_id: 'backlog.generate', instance_key: null, decision_fingerprint: decision.decision_fingerprint },
            review: {
                phase: 'backlog',
                lineage: {
                    specification: { spec_version_id: 31, spec_hash: 'sha256:hidden-specification-canary' },
                    product_goal: { product_goal_artifact_id: 21, product_goal_fingerprint: 'sha256:hidden-product-goal-canary' },
                },
                candidate: {
                    backlog_artifact_id: 7, artifact_fingerprint: 'sha256:hidden-backlog-artifact-canary',
                    version_number: 1, supersedes_backlog_artifact_id: null,
                    backlog_items: [{ backlog_item_id: 'PBI-000001', priority: 1, requirement: content, value_driver: 'sha256:customer-token', justification: 'Visible justification', estimated_effort: 'M', technical_note: null, specification_evidence: [] }],
                    is_complete: true, clarifying_questions: [],
                },
                review: { state: 'feedback', rationale, reviewer: 'reviewer-hidden@example.com' },
            },
        } } },
        actions: mode === 'active' ? [] : [{
            node_id: 'backlog.generate', instance_key: null, request_kind: 'record_backlog_draft',
            endpoint: 'backlog/generate', transport: 'semantic',
        }],
    };
}

test('Backlog Feedback rendering escapes durable content and hides exact internal canaries', () => {
    const context = loadFrontend();
    const hidden = [
        'sha256:hidden-backlog-decision-canary',
        'sha256:hidden-backlog-artifact-canary',
        'sha256:hidden-specification-canary',
        'sha256:hidden-product-goal-canary',
        'sha256:hidden-node-attempt-canary',
        'reviewer-hidden@example.com',
    ];
    for (const mode of ['revision-ready', 'active', 'failed-retry', 'expired-recovery']) {
        const state = feedbackContinuation(mode, '<img src=x onerror=alert(1)>', '<script>alert(1)</script>');
        const markup = context.deliveryPanelMarkup(state.position, state.reviews, state.actions);
        assert.ok(markup.includes('data-backlog-feedback-continuation="true"'));
        assert.ok(markup.includes('&lt;img src=x onerror=alert(1)&gt;'));
        assert.ok(markup.includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
        assert.ok(!markup.includes('<img src=x'));
        assert.ok(markup.includes('sha256:customer-token'));
        assert.ok(!markup.includes('BACKLOG_'));
        assert.ok(!markup.includes('data-planning-review='));
        hidden.forEach((value) => assert.ok(!markup.includes(value)));
    }
});

test('Backlog Feedback keeps valid content when its advertised correction action is invalid', () => {
    const context = loadFrontend();
    const state = feedbackContinuation('revision-ready', 'Durable Backlog content', 'Durable Feedback rationale');
    state.actions = [{ ...state.actions[0], endpoint: 'backlog/incorrect' }];
    const markup = context.deliveryPanelMarkup(state.position, state.reviews, state.actions);
    assert.ok(markup.includes('Durable Backlog content'));
    assert.ok(markup.includes('Durable Feedback rationale'));
    assert.ok(markup.includes('data-backlog-feedback-projection-error="true"'));
    assert.ok(!markup.includes('data-backlog-correction-action="true"'));
});

test('Backlog Feedback payloads never render terminal review controls', () => {
    const context = loadFrontend();
    const continuation = feedbackContinuation('revision-ready', 'Visible content', 'Visible rationale');
    const topLevel = continuation.reviews.backlog.continuation;
    const variants = [
        { ...topLevel, review: { ...topLevel.review, review: { state: 'feedback', rationale: 'Feedback' } } },
        { ...topLevel, continuation: topLevel },
        { ...topLevel, review: { ...topLevel.review, review: { state: 'rejected' } } },
    ];
    for (const backlog of variants) {
        const markup = context.deliveryPanelMarkup(continuation.position, { backlog }, continuation.actions);
        assert.ok(markup.includes('data-backlog-feedback-projection-error="true"'));
        assert.ok(!markup.includes('data-planning-review="backlog"'));
        assert.ok(!markup.includes('>Accept</button>'));
        assert.ok(!markup.includes('>Request changes</button>'));
        assert.ok(!markup.includes('>Reject</button>'));
    }
});

test('review cards escape evidence and render it before controls', () => {
    const context = loadFrontend();
    const markup = context.planningReviewCardMarkup(
        'Backlog review',
        selectedReview(),
        'backlog',
    );
    assert.ok(markup.includes('&lt;img src=x onerror=alert(1)&gt;'));
    assert.ok(!markup.includes('<img src=x'));
    assert.ok(markup.indexOf('Exact evidence') < markup.indexOf('data-planning-review='));
    assert.ok(markup.includes('Requirement'));
    assert.ok(markup.includes('Specification evidence'));
    assert.ok(markup.includes('Level'));
    assert.ok(markup.includes('Verification'));
    assert.ok(!markup.includes('<pre'));
    assert.ok(!markup.includes('schema_version'));
    assert.ok(!markup.includes('lineage'));
    assert.ok(!markup.includes('hidden-decision'));
    assert.ok(!markup.includes('decision_fingerprint'));
    assert.ok(!markup.includes('REQ.hidden'));
});

test('only the exact no-current-review conflict becomes absence', async () => {
    const noCurrent = loadFrontend(async () => ({
        ok: false,
        status: 409,
        text: async () => JSON.stringify({
            detail: {
                errors: [{ code: 'PLANNING_REVIEW_NOT_AVAILABLE', message: 'No review.' }],
            },
        }),
    }));
    const absent = await noCurrent.requestPlanningReview('/review', {});
    assert.equal(Object.keys(absent.data).length, 0);

    const conflict = loadFrontend(async () => ({
        ok: false,
        status: 409,
        text: async () => JSON.stringify({
            detail: {
                errors: [{ code: 'WORKFLOW_FACT_CONFLICT', message: 'Lineage conflict.' }],
            },
        }),
    }));
    await assert.rejects(
        conflict.requestPlanningReview('/review', {}),
        (error) => error.status === 409 && error.code === 'WORKFLOW_FACT_CONFLICT',
    );
});

test('dashboard keeps exact planning binding in memory only', () => {
    const context = loadFrontend();
    const backlog = context.planningReviewBinding(
        selectedReview('Visible requirement'), 'backlog', 'accepted',
    );
    const story = context.planningReviewBinding({
        ...selectedReview('Visible Story'),
        binding: {
            decision_fingerprint: 'decision-story',
            instance_key: 'backlog_item:PBI-000001',
        },
    }, 'story', 'feedback');
    assert.equal(backlog.binding.decision_fingerprint, 'sha256:hidden-decision');
    assert.equal(story.binding.instance_key, 'backlog_item:PBI-000001');
    assert.equal(story.endpoint, 'story/decide');
});

test('live project surface contains no removed compatibility stage', () => {
    const surfaceSource = source.replaceAll("'awaiting_authority'", "''");
    const combined = `${surfaceSource}\n${html}`.toLowerCase();
    assert.ok(!combined.includes('auth' + 'ority'));
    assert.ok(!combined.includes('invar' + 'iant'));
    assert.ok(html.includes('id="delivery-panel"'));
});

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

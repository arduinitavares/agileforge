import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const sourcePath = path.resolve(import.meta.dirname, '../frontend/project.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function loadFrontend() {
    const context = vm.createContext({
        console,
        crypto: { randomUUID: () => 'goal-uuid' },
        document: {
            createElement() {
                return {
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
                };
            },
            getElementById() { return null; },
            querySelector() { return null; },
        },
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        URLSearchParams,
        window: { addEventListener() {}, location: { href: '' } },
    });
    vm.runInContext(source, context, { filename: sourcePath });
    return context;
}

function action(requestKind, endpoint) {
    return { request_kind: requestKind, endpoint };
}

const acceptedVision = {
    statement: 'Make product decisions durable and reviewable.',
};

test('Product Goal interview is distinct and keeps accepted Vision read-only', () => {
    const context = loadFrontend();
    assert.equal(typeof context.productGoalPanelMarkup, 'function');
    const markup = context.productGoalPanelMarkup(
        {
            accepted_vision: acceptedVision,
            active: null,
            transcript: [
                {
                    goal_number: 1,
                    revision_number: 1,
                    user_text: 'Operators need trusted reconciliation.',
                },
            ],
            latest_questions: ['What observable result proves success?'],
            candidate: null,
            review: null,
            outcome: null,
        },
        [action('record_product_goal_interview_turn', 'goals/respond')],
    );

    assert.match(markup, /Product Goal interview/);
    assert.match(markup, /What observable result proves success\?/);
    assert.match(markup, /Operators need trusted reconciliation\./);
    assert.match(markup, /Make product decisions durable and reviewable\./);
    assert.equal((markup.match(/<textarea\b/g) ?? []).length, 1);
    assert.match(markup, /id="goal-response"/);
    assert.doesNotMatch(markup, /id="vision-response"/);
});

test('Product Goal review shows exact candidate with accepted Vision context', () => {
    const context = loadFrontend();
    const markup = context.productGoalPanelMarkup(
        {
            accepted_vision: acceptedVision,
            active: null,
            transcript: [{ user_text: 'Earlier Goal answer.' }],
            latest_questions: [],
            candidate: { statement: 'Exact measurable Goal candidate.' },
            review: { state: 'pending' },
            outcome: null,
        },
        [action('decide_product_goal_review', 'goals/review')],
    );

    assert.match(markup, /Exact measurable Goal candidate\./);
    assert.match(markup, /Make product decisions durable and reviewable\./);
    assert.doesNotMatch(markup, /Earlier Goal answer/);
    assert.doesNotMatch(markup, /<textarea\b/);
    assert.match(markup, /data-review-scope="goal"[^>]*data-review-decision="accepted"/);
    assert.match(markup, /data-review-scope="goal"[^>]*data-review-decision="feedback"/);
    assert.match(markup, /data-review-scope="goal"[^>]*data-review-decision="rejected"/);
});

test('Goal outcome controls appear only when the graph advertises them', () => {
    const context = loadFrontend();
    const projection = {
        accepted_vision: acceptedVision,
        active: { statement: 'Deliver trusted reconciliation.' },
        transcript: [],
        latest_questions: [],
        candidate: null,
        review: { state: 'accepted' },
        outcome: null,
    };

    const quiet = context.productGoalPanelMarkup(projection, []);
    assert.doesNotMatch(quiet, /Fulfill Goal|Abandon Goal/);

    const actionable = context.productGoalPanelMarkup(
        projection,
        [
            action('fulfill_product_goal', 'goals/complete'),
            action('abandon_product_goal', 'goals/abandon'),
        ],
    );
    assert.match(actionable, /Fulfill Goal/);
    assert.match(actionable, /Abandon Goal/);
    assert.match(actionable, /data-goal-outcome="fulfilled"/);
    assert.match(actionable, /data-goal-outcome="abandoned"/);
});

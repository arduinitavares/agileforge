import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const sourcePath = path.resolve(import.meta.dirname, '../frontend/project.js');
const source = fs.readFileSync(sourcePath, 'utf8');
const starterQuestions = JSON.parse(fs.readFileSync(
    path.resolve(import.meta.dirname, 'fixtures/product_goal_starter_questions.json'),
    'utf8',
));

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

function goalProjection(overrides = {}) {
    return {
        accepted_vision: acceptedVision,
        active: null,
        transcript: [],
        latest_questions: [],
        effective_questions: null,
        candidate: null,
        review: null,
        outcome: null,
        ...overrides,
    };
}

function renderGoal(projection, actions = [action('record_product_goal_interview_turn', 'goals/respond')]) {
    const context = loadFrontend();
    assert.equal(typeof context.productGoalPanelMarkup, 'function');
    const realInterviewFormMarkup = context.interviewFormMarkup;
    let formArguments = null;
    context.interviewFormMarkup = (...args) => {
        formArguments = args;
        return realInterviewFormMarkup(...args);
    };
    const markup = context.productGoalPanelMarkup(projection, actions);
    return { markup, formArguments };
}

test('Product Goal passes the shared starter questions to the real interview renderer unchanged', () => {
    assert.deepEqual(starterQuestions, {
        questions: [
            'What valuable outcome should this Project achieve next?',
            'What observable result will prove success?',
            'What boundary keeps this Goal focused?',
        ],
        source: 'builtin_starter',
    });
    const { markup, formArguments } = renderGoal(goalProjection({
        effective_questions: starterQuestions,
        latest_questions: ['Legacy question must not select the interview prompts.'],
    }));

    assert.equal(formArguments[0], 'goal');
    assert.strictEqual(formArguments[1], starterQuestions.questions);
    assert.deepEqual(formArguments[1], starterQuestions.questions);
    let previousIndex = -1;
    for (const question of starterQuestions.questions) {
        const questionIndex = markup.indexOf(question, previousIndex + 1);
        assert.ok(questionIndex > previousIndex, `Missing or reordered starter: ${question}`);
        previousIndex = questionIndex;
    }
    assert.doesNotMatch(markup, /Legacy question/);
    assert.match(markup, /id="goal-response"/);
});

test('Product Goal interview is distinct and keeps accepted Vision read-only', () => {
    const effectiveQuestions = {
        questions: ['What observable result proves success?', 'Which boundary keeps the pilot focused?'],
        source: 'generated',
    };
    const projection = goalProjection({
        transcript: [
            {
                goal_number: 1,
                revision_number: 1,
                user_text: 'Operators need trusted reconciliation.',
            },
        ],
        latest_questions: ['Persisted legacy question must not replace projected follow-ups.'],
        effective_questions: effectiveQuestions,
    });
    const { markup, formArguments } = renderGoal(projection);

    assert.strictEqual(formArguments[1], effectiveQuestions.questions);
    assert.deepEqual(formArguments[1], effectiveQuestions.questions);
    assert.strictEqual(formArguments[2], projection.transcript);
    assert.equal(formArguments[3], 'Product Goal');
    assert.match(markup, /Product Goal interview/);
    assert.match(markup, /What observable result proves success\?/);
    assert.match(markup, /Which boundary keeps the pilot focused\?/);
    assert.doesNotMatch(markup, /Persisted legacy question/);
    assert.match(markup, /Operators need trusted reconciliation\./);
    assert.match(markup, /Make product decisions durable and reviewable\./);
    assert.match(markup, /data-interview-scope="goal"/);
    assert.match(markup, /id="goal-response"/);
    assert.doesNotMatch(markup, /id="vision-response"/);
});

for (const [label, effectiveQuestions] of [
    ['null', null],
    ['missing', undefined],
    ['missing question list', { source: 'generated' }],
    ['null question list', { questions: null, source: 'generated' }],
    ['string question list', { questions: 'A malformed question list.', source: 'generated' }],
    ['object question list', { questions: { 0: 'An array-like question list.' }, source: 'generated' }],
    ['empty question list', { questions: [], source: 'generated' }],
]) {
    test(`Product Goal ${label} effective questions keep the graph-advertised response form without fallback prompts`, () => {
        const projection = goalProjection({
            effective_questions: effectiveQuestions,
            latest_questions: ['Legacy question must not recreate the former fallback.'],
        });
        if (label === 'missing') delete projection.effective_questions;
        const { markup, formArguments } = renderGoal(projection);

        assert.deepEqual(Array.from(formArguments[1]), []);
        assert.match(markup, /No open questions recorded\./);
        assert.match(markup, /data-interview-scope="goal"/);
        assert.match(markup, /id="goal-response"/);
        assert.doesNotMatch(markup, /Legacy question/);
        for (const question of starterQuestions.questions) {
            assert.ok(!markup.includes(question), `Unexpected local starter: ${question}`);
        }
    });
}

test('Product Goal renders projected questions with HTML special characters escaped', () => {
    const questions = ['Can <script>alert("goal")</script> & "success" be observed?'];
    const { markup, formArguments } = renderGoal(goalProjection({
        effective_questions: { questions, source: 'generated' },
        latest_questions: ['Legacy plain-text question.'],
    }));

    assert.strictEqual(formArguments[1], questions);
    assert.ok(markup.includes('Can &lt;script&gt;alert(&quot;goal&quot;)&lt;/script&gt; &amp; &quot;success&quot; be observed?'));
    assert.doesNotMatch(markup, /<script>|Legacy plain-text question/);
});

test('Product Goal review shows exact candidate with accepted Vision context', () => {
    const { markup, formArguments } = renderGoal(
        goalProjection({
            transcript: [{ user_text: 'Earlier Goal answer.' }],
            effective_questions: starterQuestions,
            candidate: { statement: 'Exact measurable Goal candidate.' },
            review: { state: 'pending' },
        }),
        [action('decide_product_goal_review', 'goals/review')],
    );

    assert.equal(formArguments, null);
    assert.match(markup, /Exact measurable Goal candidate\./);
    assert.match(markup, /Make product decisions durable and reviewable\./);
    assert.doesNotMatch(markup, /Earlier Goal answer/);
    assert.doesNotMatch(markup, /<textarea\b/);
    assert.match(markup, /data-review-scope="goal"[^>]*data-review-decision="accepted"/);
    assert.match(markup, /data-review-scope="goal"[^>]*data-review-decision="feedback"/);
    assert.match(markup, /data-review-scope="goal"[^>]*data-review-decision="rejected"/);
});

test('Goal outcome controls appear only when the graph advertises them', () => {
    const projection = goalProjection({
        active: { statement: 'Deliver trusted reconciliation.' },
        effective_questions: starterQuestions,
        review: { state: 'accepted' },
    });

    const quiet = renderGoal(projection, []);
    assert.equal(quiet.formArguments, null);
    assert.match(quiet.markup, /Active Product Goal/);
    assert.doesNotMatch(quiet.markup, /Fulfill Goal|Abandon Goal|<textarea\b/);

    const actionable = renderGoal(
        projection,
        [
            action('fulfill_product_goal', 'goals/complete'),
            action('abandon_product_goal', 'goals/abandon'),
        ],
    );
    assert.equal(actionable.formArguments, null);
    assert.match(actionable.markup, /Deliver trusted reconciliation\./);
    assert.doesNotMatch(actionable.markup, /<textarea\b/);
    assert.match(actionable.markup, /Fulfill Goal/);
    assert.match(actionable.markup, /Abandon Goal/);
    assert.match(actionable.markup, /data-goal-outcome="fulfilled"/);
    assert.match(actionable.markup, /data-goal-outcome="abandoned"/);
});

test('Product Goal without accepted Vision keeps its prerequisite panel and no response form', () => {
    const { markup, formArguments } = renderGoal(goalProjection({
        accepted_vision: null,
        effective_questions: starterQuestions,
    }), []);

    assert.equal(formArguments, null);
    assert.match(markup, /Accept the Project Vision before setting a Product Goal\./);
    assert.doesNotMatch(markup, /<textarea\b|Product Goal interview/);
});

test('Projected questions do not make a response form available without the graph action', () => {
    const { markup, formArguments } = renderGoal(goalProjection({
        effective_questions: starterQuestions,
    }), []);

    assert.equal(formArguments, null);
    assert.match(markup, /Product Goal is waiting for the current lifecycle state\./);
    assert.doesNotMatch(markup, /<textarea\b/);
});

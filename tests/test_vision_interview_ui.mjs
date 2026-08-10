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
        crypto: { randomUUID: () => 'vision-uuid' },
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

const respondAction = {
    request_kind: 'record_vision_interview_turn',
    endpoint: 'vision/respond',
};
const reviewAction = {
    request_kind: 'decide_vision_review',
    endpoint: 'vision/review',
};

test('Vision interview shows focused questions, transcript, and one response field', () => {
    const context = loadFrontend();
    assert.equal(typeof context.visionPanelMarkup, 'function');
    const markup = context.visionPanelMarkup(
        {
            current: null,
            transcript: [
                {
                    turn_number: 1,
                    user_text: 'Product teams need durable review.',
                    statement: 'A durable product workflow.',
                },
            ],
            latest_questions: [
                'Who benefits first?',
                'What alternative do they use today?',
            ],
            candidate: null,
            review: null,
        },
        [respondAction],
    );

    assert.match(markup, /Who benefits first\?/);
    assert.match(markup, /What alternative do they use today\?/);
    assert.match(markup, /Product teams need durable review\./);
    assert.equal((markup.match(/<textarea\b/g) ?? []).length, 1);
    assert.match(markup, /id="vision-response"/);
    assert.doesNotMatch(markup, /JSON|fingerprint|graph|model/i);
});

test('Vision review shows only the exact immutable candidate and its own controls', () => {
    const context = loadFrontend();
    const candidate = 'Exact <Vision> candidate, unchanged.';
    const markup = context.visionPanelMarkup(
        {
            current: null,
            transcript: [{ turn_number: 1, user_text: 'Earlier answer.' }],
            latest_questions: [],
            candidate: { statement: candidate },
            review: { state: 'pending' },
        },
        [reviewAction],
    );

    assert.match(markup, /Exact &lt;Vision&gt; candidate, unchanged\./);
    assert.equal((markup.match(/Exact &lt;Vision&gt; candidate, unchanged\./g) ?? []).length, 1);
    assert.doesNotMatch(markup, /Earlier answer/);
    assert.doesNotMatch(markup, /<textarea\b/);
    assert.match(markup, /data-review-scope="vision"[^>]*data-review-decision="accepted"/);
    assert.match(markup, /data-review-scope="vision"[^>]*data-review-decision="feedback"/);
    assert.match(markup, /data-review-scope="vision"[^>]*data-review-decision="rejected"/);
});

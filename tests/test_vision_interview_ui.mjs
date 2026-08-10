import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const sourcePath = path.resolve(import.meta.dirname, '../frontend/project.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function loadFrontend({ fetchImpl, elements = {} } = {}) {
    const documentListeners = {};
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
            addEventListener(type, listener) { documentListeners[type] = listener; },
            getElementById(id) { return elements[id] ?? null; },
            querySelector() { return null; },
        },
        fetch: fetchImpl ?? (async () => ({ ok: true, text: async () => '{}' })),
        URLSearchParams,
        window: { addEventListener() {}, location: { href: '' } },
    });
    context.__documentListeners = documentListeners;
    vm.runInContext(source, context, { filename: sourcePath });
    return context;
}

const respondAction = {
    request_kind: 'record_vision_interview_turn',
    endpoint: 'vision/respond',
};
const bootstrapAction = {
    request_kind: 'generate_vision_bootstrap',
    endpoint: 'vision/bootstrap',
};
const reviewAction = {
    request_kind: 'decide_vision_review',
    endpoint: 'vision/review',
};

function reviewMaterial({ complete = false } = {}) {
    return {
        statement: 'A <strong>durable</strong> Vision.',
        components: [
            {
                name: 'project_name',
                value: 'AgileForge <img src=x onerror=alert(1)>',
                source_kinds: ['evidence', 'human', 'inference'],
            },
            {
                name: 'competitors',
                value: complete ? 'Mutable chat history' : null,
                source_kinds: complete ? ['evidence'] : [],
            },
        ],
        assumptions: [
            {
                text: 'Teams can adopt <script>unsafe()</script>.',
                affected_components: ['key_benefit'],
            },
        ],
        conflicts: [
            {
                text: 'The <em>alternative</em> is disputed.',
                status: complete ? 'resolved' : 'unresolved',
                affected_components: ['competitors'],
                resolution: complete ? 'Use mutable chat history.' : null,
            },
        ],
        questions: complete ? [] : [
            {
                question_id: 'question-secret-id',
                text: 'Which <svg onload=alert(1)> alternative is used?',
                affected_components: ['competitors'],
            },
        ],
    };
}

test('initial Vision state offers one generation action without fallback input', () => {
    let fetchCalls = 0;
    const context = loadFrontend({ fetchImpl: async () => {
        fetchCalls += 1;
        return { ok: true, text: async () => '{}' };
    } });
    const markup = context.visionPanelMarkup(
        {
            bootstrap_available: true,
            current: null,
            draft: null,
            transcript: [],
            candidate: null,
            review: null,
        },
        [bootstrapAction],
        {
            project: {
                name: 'Project <script>unsafe()</script>',
                description: 'Durable & reviewable context',
            },
            repository: null,
        },
    );

    assert.equal((markup.match(/Generate Vision draft/g) ?? []).length, 1);
    assert.match(markup, /data-direct-action="generate_vision_bootstrap"/);
    assert.match(markup, /Project &lt;script&gt;unsafe\(\)&lt;\/script&gt;/);
    assert.match(markup, /Durable &amp; reviewable context/);
    assert.match(markup, /Not attached/);
    assert.doesNotMatch(markup, /<textarea\b/);
    assert.doesNotMatch(markup, /Who should benefit|What problem should|What principles should/);
    assert.equal(fetchCalls, 0);
});

test('incomplete Vision draft shows safe provenance, questions, and one response field', () => {
    const context = loadFrontend();
    assert.equal(typeof context.visionPanelMarkup, 'function');
    const markup = context.visionPanelMarkup(
        {
            bootstrap_available: false,
            current: null,
            draft: reviewMaterial(),
            transcript: [{ user_text: 'Product teams need durable review.' }],
            candidate: null,
            review: null,
        },
        [respondAction],
    );

    assert.match(markup, /A &lt;strong&gt;durable&lt;\/strong&gt; Vision\./);
    assert.match(markup, /AgileForge &lt;img src=x onerror=alert\(1\)&gt;/);
    assert.match(markup, /Project evidence/);
    assert.match(markup, /Human input/);
    assert.match(markup, /Inferred/);
    assert.match(markup, /Teams can adopt &lt;script&gt;unsafe\(\)&lt;\/script&gt;\./);
    assert.match(markup, /The &lt;em&gt;alternative&lt;\/em&gt; is disputed\./);
    assert.match(markup, /Which &lt;svg onload=alert\(1\)&gt; alternative is used\?/);
    assert.match(markup, /Product teams need durable review\./);
    assert.equal((markup.match(/<textarea\b/g) ?? []).length, 1);
    assert.match(markup, /id="vision-response"/);
    assert.doesNotMatch(markup, /question-secret-id|JSON|fingerprint|graph|model|evidence_id/i);
    assert.doesNotMatch(markup, /<script>|<img|<svg|<em>/i);
});

test('complete Vision candidate shows provenance and review controls without internals', () => {
    const context = loadFrontend();
    const candidate = {
        ...reviewMaterial({ complete: true }),
        review_fingerprint: 'sha256:never-render-this',
    };
    const markup = context.visionPanelMarkup(
        {
            bootstrap_available: false,
            current: null,
            transcript: [{ turn_number: 1, user_text: 'Earlier answer.' }],
            draft: null,
            candidate,
            review: { state: 'pending', rationale: null },
        },
        [reviewAction],
    );

    assert.match(markup, /A &lt;strong&gt;durable&lt;\/strong&gt; Vision\./);
    assert.match(markup, /Mutable chat history/);
    assert.match(markup, /Project evidence/);
    assert.doesNotMatch(markup, /<textarea\b/);
    assert.doesNotMatch(markup, /sha256:never-render-this|question-secret-id|fingerprint/i);
    assert.match(markup, /data-review-scope="vision"[^>]*data-review-decision="accepted"/);
    assert.match(markup, /data-review-scope="vision"[^>]*data-review-decision="feedback"/);
    assert.match(markup, /data-review-scope="vision"[^>]*data-review-decision="rejected"/);
});

test('Vision feedback shows rationale with the revised draft context', () => {
    const context = loadFrontend();
    const revised = reviewMaterial();
    revised.statement = 'A revised <Vision> statement.';
    const markup = context.visionPanelMarkup(
        {
            bootstrap_available: false,
            current: null,
            draft: revised,
            transcript: [],
            candidate: {
                ...reviewMaterial({ complete: true }),
                review_fingerprint: 'sha256:hidden',
            },
            review: {
                state: 'rejected',
                rationale: 'Narrow the <target> audience.',
            },
        },
        [respondAction],
    );

    assert.match(markup, /Review response:/);
    assert.match(markup, /Narrow the &lt;target&gt; audience\./);
    assert.match(markup, /A revised &lt;Vision&gt; statement\./);
    assert.equal((markup.match(/<textarea\b/g) ?? []).length, 1);
});

test('accepted Vision retains revision and an open revision offers generation', () => {
    const context = loadFrontend();
    const accepted = {
        bootstrap_available: false,
        current: { statement: 'Accepted <Vision>.' },
        draft: null,
        transcript: [],
        candidate: null,
        review: { state: 'accepted', rationale: 'Reviewed.' },
    };
    const acceptedMarkup = context.visionPanelMarkup(
        accepted,
        [{ request_kind: 'begin_vision_revision', endpoint: 'vision/revision' }],
    );
    const revisionMarkup = context.visionPanelMarkup(
        { ...accepted, bootstrap_available: true },
        [bootstrapAction],
    );

    assert.match(acceptedMarkup, /Accepted &lt;Vision&gt;\./);
    assert.match(acceptedMarkup, /data-vision-revision="true"/);
    assert.match(revisionMarkup, /Accepted &lt;Vision&gt;\./);
    assert.match(revisionMarkup, /Generate Vision draft/);
    assert.doesNotMatch(revisionMarkup, /<textarea\b|data-vision-revision/);
});

test('Vision response submits ordinary text without question identity', async () => {
    const requests = [];
    const submit = { disabled: false };
    const context = loadFrontend({
        elements: {
            'vision-response': { value: 'Clarify the target team.' },
        },
        fetchImpl: async (...args) => {
            requests.push(args);
            return {
                ok: false,
                text: async () => JSON.stringify({ message: 'Expected test stop.' }),
            };
        },
    });
    vm.runInContext(`
        selectedProjectId = 9;
        lifecycleState.actions = [{
            request_kind: 'record_vision_interview_turn',
            endpoint: 'vision/respond',
        }];
    `, context);
    context.installInteractions();
    const form = {
        dataset: { interviewScope: 'vision' },
        querySelector() { return submit; },
    };

    await context.__documentListeners.submit({
        target: form,
        preventDefault() {},
    });

    assert.equal(requests.length, 1);
    assert.equal(requests[0][0], '/api/projects/9/vision/respond');
    const body = JSON.parse(requests[0][1].body);
    assert.equal(body.text, 'Clarify the target team.');
    assert.deepEqual(
        Object.keys(body).sort(),
        ['actor', 'idempotency_key', 'text'],
    );
    assert.equal(submit.disabled, false);
});

test('Vision generation disables immediately and ignores a second submission', async () => {
    const pendingResponses = [];
    const context = loadFrontend({
        fetchImpl: (...args) => new Promise((resolve) => {
            pendingResponses.push({ args, resolve });
        }),
    });
    vm.runInContext(`
        selectedProjectId = 7;
        lifecycleState.actions = [{
            request_kind: 'generate_vision_bootstrap',
            endpoint: 'vision/bootstrap',
        }];
    `, context);
    const button = { disabled: false };

    const first = context.runDirectAction('generate_vision_bootstrap', button);
    const second = context.runDirectAction('generate_vision_bootstrap', button);

    assert.equal(button.disabled, true);
    assert.equal(pendingResponses.length, 1);
    assert.equal(pendingResponses[0].args[0], '/api/projects/7/vision/bootstrap');
    pendingResponses[0].resolve({
        ok: false,
        text: async () => JSON.stringify({ message: 'Expected test stop.' }),
    });
    await Promise.all([first, second]);
    assert.equal(button.disabled, false);
});

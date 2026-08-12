import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const sourcePath = path.resolve(import.meta.dirname, '../frontend/project.js');
const htmlPath = path.resolve(import.meta.dirname, '../frontend/project.html');
const source = fs.readFileSync(sourcePath, 'utf8');
const html = fs.readFileSync(htmlPath, 'utf8');

function createElement() {
    return {
        _textContent: '',
        innerHTML: '',
        classList: { add() {}, remove() {}, toggle() {} },
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
}

function loadFrontend(fetchImpl = async () => ({
    ok: true,
    text: async () => '{}',
})) {
    const controls = new Map();
    const document = {
        title: '',
        createElement,
        getElementById(id) {
            if (!controls.has(id)) controls.set(id, createElement());
            return controls.get(id);
        },
        querySelector() { return null; },
        addEventListener() {},
    };
    const context = vm.createContext({
        AbortController,
        console,
        crypto: { randomUUID: () => 'review-uuid' },
        document,
        fetch: fetchImpl,
        URLSearchParams,
        window: {
            addEventListener() {},
            location: { href: '' },
            setTimeout(callback) { callback(); },
        },
    });
    vm.runInContext(source, context, { filename: sourcePath });
    return { context, controls };
}

function action(requestKind, endpoint) {
    return {
        request_kind: requestKind,
        endpoint,
        transport: 'semantic',
    };
}

function deferred() {
    let resolve;
    const promise = new Promise((done) => { resolve = done; });
    return { promise, resolve };
}

function responseFor(url, projectName) {
    let payload = { data: {} };
    if (url === '/api/projects/41') {
        payload = { data: { name: projectName, description: `${projectName} description` } };
    } else if (url.endsWith('/position')) {
        payload = {
            data: { decisions: [] },
            actions: [action('decide_vision_review', 'vision/review')],
        };
    }
    return {
        ok: true,
        text: async () => JSON.stringify(payload),
    };
}

test('Authority renders every advertised recovery step', () => {
    const { context } = loadFrontend();

    const feedback = context.authorityPanelMarkup(
        { pending_authority: null, accepted_authority: { invariants: [] } },
        [action('record_authority_feedback', 'authority/feedback')],
    );
    const repair = context.authorityPanelMarkup(
        { pending_authority: null, accepted_authority: { invariants: [] } },
        [action('repair_authority', 'authority/repair')],
    );

    assert.match(feedback, /data-authority-feedback="true"/);
    assert.match(feedback, />Record feedback</);
    assert.doesNotMatch(feedback, /Authority accepted/);
    assert.match(repair, /data-direct-action="repair_authority"/);
    assert.match(repair, />Recompile</);
    assert.doesNotMatch(repair, /Authority accepted/);
});

test('lifecycle stages omit mandatory Discovery', () => {
    const { context } = loadFrontend();

    assert.deepEqual(
        JSON.parse(JSON.stringify(context.lifecycleStageLabels())),
        [
            'Vision',
            'Product Goal',
            'Specification',
            'Authority',
            'Backlog',
            'Roadmap',
            'Stories',
            'Sprint',
            'Execution',
            'Review',
        ],
    );
});

test('Specification authoring is host-owned and renders the exact v2 review packet', () => {
    const { context } = loadFrontend();

    const author = context.specificationPanelMarkup(
        { candidate: null, review: null },
        [action('author_specification', 'specifications/author')],
    );
    assert.match(author, /data-direct-action="author_specification"/);
    assert.match(author, />Author Specification</);
    assert.doesNotMatch(author, /textarea|type="file"|canonical_content/i);

    const rendered = context.specificationPanelMarkup(
        {
            candidate: {
                candidate_fingerprint: 'sha256:spec-seen',
                rendered_markdown: '# Exact Specification\n\n<unsafe>',
            },
            review: { state: 'pending' },
        },
        [action('decide_specification', 'specifications/decision')],
    );
    assert.match(rendered, /# Exact Specification/);
    assert.match(rendered, /&lt;unsafe&gt;/);
    assert.doesNotMatch(rendered, /<unsafe>/);

    const binding = context.captureReviewBinding(
        {
            actions: [action('decide_specification', 'specifications/decision')],
            specification: {
                candidate: {
                    candidate_fingerprint: 'sha256:spec-seen',
                    fingerprint: 'sha256:obsolete-alias',
                },
            },
        },
        'specification',
        'accepted',
    );
    assert.equal(binding.expectedCandidate, 'sha256:spec-seen');
});

test('review binding keeps the exact action and candidate seen at dialog open', () => {
    const { context } = loadFrontend();
    assert.equal(typeof context.captureReviewBinding, 'function');
    assert.equal(typeof context.reviewSubmission, 'function');
    const state = {
        actions: [action('decide_vision_review', 'vision/review')],
        vision: { candidate: { review_fingerprint: 'sha256:vision-seen' } },
    };

    const binding = context.captureReviewBinding(state, 'vision', 'accepted');
    state.actions[0].endpoint = 'vision/replacement-review';
    state.vision.candidate.review_fingerprint = 'sha256:vision-replacement';
    const submission = context.reviewSubmission(binding, 'Reviewed as shown.');

    assert.equal(submission.action.endpoint, 'vision/review');
    assert.equal(submission.expectedCandidate, 'sha256:vision-seen');
    assert.deepEqual(JSON.parse(JSON.stringify(submission.fields)), {
        decision: 'accepted',
        rationale: 'Reviewed as shown.',
    });
});

test('an older delayed dashboard load cannot replace the newest coherent snapshot', async () => {
    const calls = [];
    const { context } = loadFrontend((url, options = {}) => {
        const pending = deferred();
        calls.push({ url, options, ...pending });
        return pending.promise;
    });
    vm.runInContext('selectedProjectId = 41', context);

    const older = context.loadDashboard();
    const newest = context.loadDashboard();
    assert.equal(calls.length, 14);

    calls.slice(7).forEach((call) => call.resolve(responseFor(call.url, 'Newest')));
    await newest;
    calls.slice(0, 7).forEach((call) => call.resolve(responseFor(call.url, 'Older')));
    await older;

    assert.equal(calls[0].options.signal.aborted, true);
    assert.equal(calls.some((call) => call.url.endsWith('/discovery')), false);
    assert.equal(vm.runInContext("'discovery' in lifecycleState", context), false);
    assert.equal(vm.runInContext('lifecycleState.project.name', context), 'Newest');
    assert.equal(context.document.title, 'Newest | AgileForge');
});

test('FastAPI validation arrays render field locations and messages on project actions', () => {
    const { context } = loadFrontend();
    const message = context.responseErrorMessage(
        {
            detail: [
                { loc: ['body', 'rationale'], msg: 'Field required' },
                { loc: ['body', 'decision'], msg: 'Input should be accepted or rejected' },
            ],
        },
        'The requested action failed.',
    );

    assert.equal(
        message,
        'Rationale: Field required. Decision: Input should be accepted or rejected.',
    );
});

test('native human action dialog has an accessible name and description', () => {
    const dialog = html.match(/<dialog id="human-action-dialog"[\s\S]*?<\/dialog>/)?.[0];
    assert.ok(dialog);
    assert.match(dialog, /aria-labelledby="human-action-title"/);
    assert.match(dialog, /aria-describedby="human-action-description"/);
});

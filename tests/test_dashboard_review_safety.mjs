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

test('Authority review renders the complete deterministic artifact before controls', () => {
    const { context } = loadFrontend();
    const projection = {
        pending_authority: {
            authority_id: 91,
            authority_fingerprint: 'sha256:exact-authority-binding',
            spec_version_id: 23,
            compiler_version: 'COMPILER-PROVENANCE-SENTINEL',
            prompt_hash: 'PROMPT-PROVENANCE-SENTINEL',
            compiled_at: '2026-08-11T12:00:00Z',
            artifact: {
                schema_version: 'agileforge.compiled_authority.v3',
                scope_themes: ['SCOPE-THEME-SENTINEL'],
                domain: 'DOMAIN-SENTINEL',
                invariants: [{
                    id: 'INVARIANT-ID-SENTINEL',
                    type: 'REQUIRED_FIELD',
                    source_item_id: 'SOURCE-ITEM-SENTINEL',
                    source_level: 'MUST',
                    parameters: { field_name: 'artifact_id' },
                }],
                eligible_feature_rules: [{ rule: 'ELIGIBLE-RULE-SENTINEL' }],
                rejected_features: ['REJECTED-FEATURE-SENTINEL'],
                gaps: ['GAP-SENTINEL'],
                assumptions: [{ kind: 'ASSUMPTION-KIND-SENTINEL' }],
                source_map: [{
                    invariant_id: 'INVARIANT-ID-SENTINEL',
                    excerpt: 'SOURCE-MAP-EXCERPT-SENTINEL',
                    location: 'SOURCE-ITEM-SENTINEL',
                }],
                authority_quality: {
                    review_groups: [{ reason: 'QUALITY-METADATA-SENTINEL' }],
                },
                compiler_version: 'ARTIFACT-COMPILER-SENTINEL',
                prompt_hash: 'ARTIFACT-PROMPT-SENTINEL',
                future_metadata: '<FUTURE-METADATA-SENTINEL>',
            },
            findings: [{ message: 'REVIEW-FINDING-SENTINEL' }],
        },
        findings: [{ message: 'REVIEW-FINDING-SENTINEL' }],
    };

    const rendered = context.authorityPanelMarkup(
        projection,
        [action('decide_authority', 'authority/decision')],
    );
    const controlsAt = rendered.indexOf('data-review-decision="accepted"');
    assert.notEqual(controlsAt, -1);
    for (const sentinel of [
        'COMPILER-PROVENANCE-SENTINEL',
        'PROMPT-PROVENANCE-SENTINEL',
        'SCOPE-THEME-SENTINEL',
        'DOMAIN-SENTINEL',
        'INVARIANT-ID-SENTINEL',
        'SOURCE-ITEM-SENTINEL',
        'MUST',
        'ELIGIBLE-RULE-SENTINEL',
        'REJECTED-FEATURE-SENTINEL',
        'ASSUMPTION-KIND-SENTINEL',
        'SOURCE-MAP-EXCERPT-SENTINEL',
        'QUALITY-METADATA-SENTINEL',
        'ARTIFACT-COMPILER-SENTINEL',
        'ARTIFACT-PROMPT-SENTINEL',
        'FUTURE-METADATA-SENTINEL',
        'REVIEW-FINDING-SENTINEL',
    ]) {
        const renderedAt = rendered.indexOf(sentinel);
        assert.notEqual(renderedAt, -1, `${sentinel} must be rendered`);
        assert.ok(renderedAt < controlsAt, `${sentinel} must precede controls`);
    }
    assert.match(rendered, /&lt;FUTURE-METADATA-SENTINEL&gt;/);
    assert.doesNotMatch(rendered, /<FUTURE-METADATA-SENTINEL>/);
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

test('Specification source registration exposes only normal path fields', () => {
    const { context } = loadFrontend();

    const registration = context.specificationPanelMarkup(
        { candidate: null, review: null },
        [action('register_specification_source', 'specifications/source')],
    );
    assert.match(registration, /data-specification-source-form="true"/);
    assert.match(registration, /name="source_path"/);
    assert.match(registration, /name="adr_paths"/);
    assert.match(registration, /name="preparation_capability"/);
    assert.match(registration, /grill-with-docs/);
    assert.match(registration, /attestation/);
    assert.match(registration, /does not infer or prove/);
    assert.match(registration, /CONTEXT\.md is captured automatically when present/);
    assert.match(registration, /absence is recorded/);
    assert.doesNotMatch(registration, /context_path|context_required/i);
    assert.doesNotMatch(registration, /fingerprint|artifact_id|candidate_id|canonical_content/i);
    assert.doesNotMatch(source, /author_specification|Author Specification/);

    assert.equal(typeof context.specificationSourceSubmission, 'function');
    const submission = context.specificationSourceSubmission(
        [action('register_specification_source', 'specifications/source')],
        ' specification.md ',
        ' grill-with-docs ',
        ' docs/adr/0002.md\n\n docs/adr/0001.md ',
    );
    assert.equal(submission.action.endpoint, 'specifications/source');
    assert.deepEqual(JSON.parse(JSON.stringify(submission.fields)), {
        source_path: 'specification.md',
        preparation_capability: 'grill-with-docs',
        adr_paths: ['docs/adr/0002.md', 'docs/adr/0001.md'],
    });
});

test('accepted Specification remains visible while amendment source is prepared', () => {
    const { context } = loadFrontend();
    const rendered = context.specificationPanelMarkup(
        {
            current: {
                spec_version_id: 8,
                spec_hash: 'sha256:accepted-base',
                candidate: {
                    rendered_markdown: '# Accepted base Specification',
                },
            },
            candidate: null,
            review: null,
        },
        [
            action('register_specification_source', 'specifications/source'),
            action('structure_specification', 'specifications/structure'),
        ],
    );

    assert.match(rendered, /Accepted Specification/);
    assert.match(rendered, /# Accepted base Specification/);
    assert.match(rendered, /data-specification-source-form="true"/);
    assert.match(rendered, /data-direct-action="structure_specification"/);
});

test('Specification structuring is host-owned and preserves exact candidate review', () => {
    const { context } = loadFrontend();

    const structure = context.specificationPanelMarkup(
        { candidate: null, review: null },
        [action('structure_specification', 'specifications/structure')],
    );
    assert.match(structure, /data-direct-action="structure_specification"/);
    assert.match(structure, />Structure Specification</);
    assert.doesNotMatch(structure, /textarea|type="file"|fingerprint|canonical_content/i);

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

test('terminal Specification review keeps exact candidate and source re-entry', () => {
    const { context } = loadFrontend();
    const actions = [action('register_specification_source', 'specifications/source')];
    const candidate = {
        candidate_fingerprint: 'sha256:terminal-candidate',
        rendered_markdown: '# Exact terminal candidate',
    };

    for (const state of ['rejected', 'feedback', 'accepted']) {
        const rendered = context.specificationPanelMarkup(
            { candidate, review: { state } },
            actions,
        );

        assert.match(rendered, /# Exact terminal candidate/);
        assert.match(rendered, /data-specification-source-form="true"/);
        assert.match(rendered, /Register Specification source/);
    }
});

test('pending Specification review never exposes source replacement', () => {
    const { context } = loadFrontend();
    const rendered = context.specificationPanelMarkup(
        {
            candidate: {
                candidate_fingerprint: 'sha256:pending-candidate',
                rendered_markdown: '# Exact pending candidate',
            },
            review: { state: 'pending' },
        },
        [
            action('decide_specification', 'specifications/decision'),
            action('register_specification_source', 'specifications/source'),
        ],
    );

    assert.match(rendered, /# Exact pending candidate/);
    assert.match(rendered, /data-review-scope="specification"/);
    assert.doesNotMatch(rendered, /data-specification-source-form="true"/);
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

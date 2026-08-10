import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const sourcePath = path.resolve(import.meta.dirname, '../frontend/project.js');
const source = fs.readFileSync(sourcePath, 'utf8');
const html = fs.readFileSync(
    path.resolve(import.meta.dirname, '../frontend/project.html'),
    'utf8',
);

function loadFrontend() {
    const context = vm.createContext({
        console,
        crypto: { randomUUID: () => 'uuid-1' },
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
    return {
        node_id: `internal.${requestKind}`,
        instance_key: null,
        request_kind: requestKind,
        endpoint,
        transport: 'semantic',
    };
}

test('human lifecycle labels cover every operator stage', () => {
    const context = loadFrontend();
    assert.equal(typeof context.lifecycleStageLabels, 'function');
    assert.deepEqual(Array.from(context.lifecycleStageLabels()), [
        'Vision',
        'Product Goal',
        'Discovery',
        'Specification',
        'Authority',
        'Backlog',
        'Roadmap',
        'Stories',
        'Sprint',
        'Execution',
        'Review',
    ]);
});

test('workflow position renders plain-language routing without internal guards', () => {
    const context = loadFrontend();
    assert.equal(typeof context.workflowPositionMarkup, 'function');
    const position = {
        graph_version: 'agileforge.workflow.v2',
        fact_fingerprint: 'sha256:secret-facts',
        decisions: [
            {
                node_id: 'authority.compile',
                child_graph_id: 'authority',
                request_kind: 'compile_authority',
                category: 'available',
                reason_code: 'AUTHORITY_COMPILE_REQUIRED',
                decision_fingerprint: 'sha256:secret-decision',
            },
            {
                node_id: 'backlog.generate',
                child_graph_id: 'backlog',
                request_kind: 'record_backlog_draft',
                category: 'blocked',
                reason_code: 'AUTHORITY_REQUIRED',
                blockers: [{ message: 'Accept the Specification Authority first.' }],
                decision_fingerprint: 'sha256:blocked-decision',
            },
        ],
    };

    const markup = context.workflowPositionMarkup(
        position,
        [action('compile_authority', 'authority/compile')],
    );

    assert.match(markup, />Authority</);
    assert.match(markup, />Backlog</);
    assert.match(markup, /Ready/);
    assert.match(markup, /Accept the Specification Authority first\./);
    assert.doesNotMatch(markup, /authority\.compile|backlog\.generate/);
    assert.doesNotMatch(markup, /AUTHORITY_COMPILE_REQUIRED|AUTHORITY_REQUIRED/);
    assert.doesNotMatch(markup, /agileforge\.workflow|sha256:/);
});

test('semantic mutations contain transport metadata and human input only', () => {
    const context = loadFrontend();
    assert.equal(typeof context.semanticMutationPayload, 'function');
    assert.deepEqual(
        JSON.parse(JSON.stringify(
            context.semanticMutationPayload({ rationale: 'Exact human decision.' }),
        )),
        {
            idempotency_key: 'dashboard-uuid-1',
            actor: 'dashboard-ui',
            rationale: 'Exact human decision.',
        },
    );
    assert.doesNotMatch(source, /expected_fact_fingerprint|expected_decision_fingerprint/);
    assert.doesNotMatch(source, /workflowInputControl|input_payload|model_id/);
});

test('discovery and specification are readable and reviewable without JSON editing', () => {
    const context = loadFrontend();
    assert.equal(typeof context.discoveryPanelMarkup, 'function');
    assert.equal(typeof context.specificationPanelMarkup, 'function');
    const discovery = context.discoveryPanelMarkup({
        current: {
            canonical_content: {
                summary: 'Operators need reliable reconciliation.',
                constraints: ['Local-first', 'Auditable'],
            },
        },
    });
    const specification = context.specificationPanelMarkup(
        {
            candidate: {
                canonical_content: {
                    title: 'Movement reconciliation',
                    acceptance: ['Every imported row has durable provenance.'],
                },
            },
            review: { state: 'pending' },
        },
        [action('decide_specification', 'specifications/review')],
    );

    assert.match(discovery, /Operators need reliable reconciliation\./);
    assert.match(discovery, /Local-first/);
    assert.doesNotMatch(discovery, /textarea|\{&quot;|canonical_content/);
    assert.match(specification, /Movement reconciliation/);
    assert.match(specification, /Every imported row has durable provenance\./);
    assert.match(specification, /data-review-decision="accepted"/);
    assert.match(specification, /data-review-decision="feedback"/);
    assert.match(specification, /data-review-decision="rejected"/);
    assert.doesNotMatch(specification, /textarea|fingerprint/);
});

test('Authority and repository panels expose human controls and provenance only', () => {
    const context = loadFrontend();
    assert.equal(typeof context.authorityPanelMarkup, 'function');
    assert.equal(typeof context.repositoryPanelMarkup, 'function');
    const compile = context.authorityPanelMarkup(
        { pending_authority: null, findings: [] },
        [action('compile_authority', 'authority/compile')],
    );
    const review = context.authorityPanelMarkup(
        {
            pending_authority: {
                invariants: [{ id: 'INV-01', type: 'REQUIRED_FIELD', parameters: { field_name: 'project_id' } }],
                findings: [{ severity: 'review', message: 'Confirm project ownership.' }],
            },
            findings: [{ severity: 'review', message: 'Confirm project ownership.' }],
        },
        [action('decide_authority', 'authority/decision')],
    );
    const repository = context.repositoryPanelMarkup(
        {
            repository: {
                worktree_path: '/tmp/human-project',
                branch_name: null,
                detached_head: true,
                head_sha: '0123456789abcdef',
                dirty: true,
                inspected_at: '2026-08-10T12:00:00Z',
                warnings: [{ message: 'Working tree has uncommitted changes.' }],
            },
        },
        [
            action('record_repository_binding', 'repository'),
            action('refresh_repository_binding', 'repository/refresh'),
        ],
    );

    assert.match(compile, />Compile</);
    assert.doesNotMatch(compile, /textarea|payload|model/i);
    assert.match(review, /REQUIRED FIELD/);
    assert.match(review, /project id/);
    assert.match(review, /Confirm project ownership\./);
    assert.match(review, /data-review-decision="accepted"/);
    assert.match(review, /data-review-decision="feedback"/);
    assert.match(review, /data-review-decision="rejected"/);
    assert.doesNotMatch(review, /INV-01|fingerprint|compiler_version/);
    assert.match(repository, /\/tmp\/human-project/);
    assert.match(repository, /Detached at 01234567/);
    assert.match(repository, /Dirty/);
    assert.match(repository, /Working tree has uncommitted changes\./);
    assert.match(repository, />Replace</);
    assert.match(repository, />Refresh</);
    assert.doesNotMatch(repository, /status_fingerprint|common_git_dir|remotes/);
});

test('project page owns only named human panels and dialogs', () => {
    for (const id of [
        'lifecycle-stage-strip',
        'vision-panel',
        'goal-panel',
        'discovery-panel',
        'specification-panel',
        'authority-panel',
        'repository-panel',
        'human-action-dialog',
    ]) {
        assert.match(html, new RegExp(`id="${id}"`));
    }
    assert.doesNotMatch(html, /id="workflow-action-fields"|id="project-origin"/);
    assert.doesNotMatch(html, /\bonclick=/);
});

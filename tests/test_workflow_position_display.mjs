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

function stageCard(markup, stage) {
    const card = markup
        .split('</li>')
        .find((item) => item.includes(`>${stage}</p>`));
    assert.ok(card, `Missing ${stage} lifecycle card.`);
    return `${card}</li>`;
}

test('human lifecycle labels cover every operator stage', () => {
    const context = loadFrontend();
    assert.equal(typeof context.lifecycleStageLabels, 'function');
    assert.deepEqual(Array.from(context.lifecycleStageLabels()), [
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

test('workflow position distinguishes prerequisite waiting from human review', () => {
    const context = loadFrontend();
    const prerequisiteMarkup = context.workflowPositionMarkup(
        {
            decisions: [
                {
                    child_graph_id: 'authority',
                    request_kind: 'compile_authority',
                    category: 'waiting',
                    reason_code: 'WAITING_FOR_REGISTERED_SPEC',
                },
            ],
        },
        [],
    );
    const reviewMarkup = context.workflowPositionMarkup(
        {
            decisions: [
                {
                    child_graph_id: 'authority',
                    request_kind: 'decide_authority',
                    category: 'waiting',
                    reason_code: 'AUTHORITY_REVIEW_REQUIRED',
                },
            ],
        },
        [action('decide_authority', 'authority/decision')],
    );

    assert.match(prerequisiteMarkup, />Authority</);
    assert.match(prerequisiteMarkup, />Waiting</);
    assert.match(prerequisiteMarkup, /Waiting for registered Specification\./);
    assert.doesNotMatch(prerequisiteMarkup, /In progress|A human decision is pending/);
    assert.match(reviewMarkup, /In progress/);
    assert.match(reviewMarkup, /A human decision is pending\./);
});

test('accepted definition and failed Authority retry override graph-only cards', () => {
    const context = loadFrontend();
    const position = {
        decisions: [
            {
                node_id: 'goal.fulfill',
                child_graph_id: 'product_goal',
                request_kind: 'fulfill_product_goal',
                category: 'available',
                reason_code: 'PRODUCT_GOAL_FULFILLED_AVAILABLE',
            },
            {
                node_id: 'goal.abandon',
                child_graph_id: 'product_goal',
                request_kind: 'abandon_product_goal',
                category: 'available',
                reason_code: 'PRODUCT_GOAL_ABANDONED_AVAILABLE',
            },
            {
                node_id: 'authority.compile',
                child_graph_id: 'authority',
                request_kind: 'compile_authority',
                category: 'available',
                reason_code: 'AUTHORITY_COMPILE_FAILED',
            },
            {
                node_id: 'backlog.generate',
                child_graph_id: 'backlog',
                request_kind: 'record_backlog_draft',
                category: 'blocked',
                reason_code: 'ACCEPTED_AUTHORITY_REQUIRED',
                blockers: [{
                    message: 'Backlog generation requires accepted current authority.',
                }],
            },
        ],
    };
    const actions = [action('compile_authority', 'authority/compile')];
    const projections = {
        vision: { current: { statement: 'Accepted Vision' } },
        goal: {
            accepted_vision: { statement: 'Accepted Vision' },
            active: { statement: 'Accepted Product Goal' },
        },
        specification: {
            candidate: { rendered_markdown: '# Accepted Specification' },
            review: { state: 'accepted' },
        },
        authority: {
            pending_authority: null,
            accepted_authority: null,
        },
    };

    const markup = context.workflowPositionMarkup(position, actions, projections);

    assert.match(stageCard(markup, 'Vision'), />Complete</);
    assert.match(stageCard(markup, 'Product Goal'), />Active</);
    assert.doesNotMatch(stageCard(markup, 'Product Goal'), /Ready/);
    assert.match(stageCard(markup, 'Specification'), />Complete</);
    assert.match(stageCard(markup, 'Authority'), />Failed</);
    assert.match(stageCard(markup, 'Authority'), /Retry available\./);
    assert.doesNotMatch(stageCard(markup, 'Authority'), /Ready for your input/);
    assert.match(
        stageCard(markup, 'Backlog'),
        /Backlog generation requires accepted current authority\./,
    );

    assert.match(
        context.productGoalPanelMarkup(projections.goal, actions),
        /Active Product Goal/,
    );
    assert.match(
        context.specificationPanelMarkup(projections.specification, actions, position),
        /Review: Accepted/,
    );
    assert.match(context.authorityPanelMarkup(projections.authority, actions), />Compile</);
});

test('Ready for your input requires a control rendered by this dashboard', () => {
    const context = loadFrontend();
    const compilePosition = {
        decisions: [{
            child_graph_id: 'authority',
            request_kind: 'compile_authority',
            category: 'available',
            reason_code: 'AUTHORITY_COMPILE_REQUIRED',
        }],
    };
    const deliveryPosition = {
        decisions: [{
            child_graph_id: 'backlog',
            request_kind: 'record_backlog_draft',
            category: 'available',
            reason_code: 'BACKLOG_GENERATION_AVAILABLE',
        }],
    };

    const ready = context.workflowPositionMarkup(
        compilePosition,
        [action('compile_authority', 'authority/compile')],
        {},
    );
    const noControl = context.workflowPositionMarkup(
        deliveryPosition,
        [action('record_backlog_draft', 'backlog/generate')],
        {},
    );

    assert.match(stageCard(ready, 'Authority'), />Ready</);
    assert.match(stageCard(ready, 'Authority'), /Ready for your input\./);
    assert.match(stageCard(noControl, 'Backlog'), />Waiting</);
    assert.doesNotMatch(stageCard(noControl, 'Backlog'), /Ready for your input/);
});

test('Specification card waits when lineage guards suppress structuring control', () => {
    const context = loadFrontend();
    const position = {
        decisions: [{
            node_id: 'specification.structure',
            child_graph_id: 'specification',
            request_kind: 'structure_specification',
            category: 'available',
            reason_code: 'SPECIFICATION_FEEDBACK_RETRY_AVAILABLE',
            decision_fingerprint: 'sha256:structure-position',
            fact_references: [
                {
                    fact_type: 'specification_source',
                    fact_id: '5',
                    fingerprint: 'sha256:source-a',
                },
                {
                    fact_type: 'specification_candidate',
                    fact_id: '9',
                    fingerprint: 'sha256:stale-candidate',
                },
            ],
        }],
    };
    const actions = [action('structure_specification', 'specifications/structure')];
    const projections = {
        specification: {
            source: {
                specification_source_id: 5,
                source_fingerprint: 'sha256:source-a',
            },
            candidate: null,
            review: null,
        },
    };

    const markup = context.workflowPositionMarkup(position, actions, projections);
    const panel = context.specificationPanelMarkup(
        projections.specification,
        actions,
        position,
    );

    assert.doesNotMatch(panel, /data-direct-action="structure_specification"/);
    assert.match(stageCard(markup, 'Specification'), />Waiting</);
    assert.doesNotMatch(
        stageCard(markup, 'Specification'),
        /Ready for your input/,
    );
});

test('pending and accepted Authority artifacts remain distinct lifecycle states', () => {
    const context = loadFrontend();
    const pending = context.workflowPositionMarkup(
        {
            decisions: [{
                child_graph_id: 'authority',
                request_kind: 'decide_authority',
                category: 'waiting',
                reason_code: 'AUTHORITY_REVIEW_REQUIRED',
            }],
        },
        [action('decide_authority', 'authority/decision')],
        { authority: { pending_authority: { authority_id: 17 } } },
    );
    const accepted = context.workflowPositionMarkup(
        {
            decisions: [{
                child_graph_id: 'product_goal',
                request_kind: 'fulfill_product_goal',
                category: 'available',
                reason_code: 'PRODUCT_GOAL_FULFILLED_AVAILABLE',
            }],
        },
        [],
        { authority: { accepted_authority: { authority_id: 17 } } },
    );

    assert.match(stageCard(pending, 'Authority'), />In progress</);
    assert.match(stageCard(pending, 'Authority'), /A human decision is pending\./);
    assert.match(stageCard(accepted, 'Authority'), />Complete</);
    assert.doesNotMatch(stageCard(accepted, 'Authority'), /Failed|Retry available/);
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

test('Specification is structured and reviewed without JSON editing', () => {
    const context = loadFrontend();
    assert.equal(typeof context.specificationPanelMarkup, 'function');
    const specification = context.specificationPanelMarkup(
        {
            candidate: {
                candidate_fingerprint: 'sha256:hidden',
                rendered_markdown: (
                    '# Movement reconciliation\n\n'
                    + '- Every imported row has durable provenance.'
                ),
            },
            review: { state: 'pending' },
        },
        [action('decide_specification', 'specifications/review')],
    );

    assert.match(specification, /Movement reconciliation/);
    assert.match(specification, /Every imported row has durable provenance\./);
    assert.match(specification, /data-review-decision="accepted"/);
    assert.match(specification, /data-review-decision="feedback"/);
    assert.match(specification, /data-review-decision="rejected"/);
    assert.doesNotMatch(specification, /textarea|fingerprint/);
});

test('Authority and repository panels expose complete review data and human controls', () => {
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
    assert.match(review, /Complete compiled Authority artifact/);
    assert.match(review, /REQUIRED_FIELD/);
    assert.match(review, /project_id/);
    assert.match(review, /INV-01/);
    assert.match(review, /compiler_version/);
    assert.match(review, /prompt_hash/);
    assert.match(review, /Confirm project ownership\./);
    assert.match(review, /data-review-decision="accepted"/);
    assert.match(review, /data-review-decision="feedback"/);
    assert.match(review, /data-review-decision="rejected"/);
    assert.doesNotMatch(review, /authority_fingerprint/);
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

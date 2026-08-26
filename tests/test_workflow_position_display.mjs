import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const sourcePath = path.resolve(import.meta.dirname, '../frontend/project.js');
const source = fs.readFileSync(sourcePath, 'utf8');

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
        crypto: { randomUUID: () => 'uuid-1' },
        document: {
            createElement,
            getElementById() { return null; },
            querySelector() { return null; },
            addEventListener() {},
        },
        fetch: fetchImpl,
        URLSearchParams,
        window: { addEventListener() {}, location: { href: '' } },
    });
    vm.runInContext(source, context, { filename: sourcePath });
    return context;
}

function validInvestAssessment() {
    return {
        independent: {
            result: 'pass',
            rationale: 'Self-contained logic.',
            evidence: 'No dependencies on unbuilt stories.',
        },
        negotiable: {
            result: 'pass',
            rationale: 'Implementation open to refinement.',
            evidence: 'Focuses on user outcome.',
        },
        valuable: {
            result: 'pass',
            rationale: 'Direct user capability.',
            evidence: 'Addresses requirement.',
        },
        estimable: {
            result: 'pass',
            rationale: 'Clear scope for sizing.',
            evidence: 'Discrete criteria.',
        },
        small: {
            result: 'pass',
            rationale: 'Sized for single iteration.',
            evidence: 'Effort is S.',
        },
        testable: {
            result: 'pass',
            rationale: 'Verifiable pass/fail criteria.',
            evidence: 'Verification steps included.',
        },
    };
}

function storyReview(instanceKey) {
    return {
        binding: {
            decision_fingerprint: `decision-${instanceKey}`,
            instance_key: instanceKey,
        },
        review: {
            phase: 'story',
            lineage: {
                backlog_item: {
                    requirement: 'Keep each Story bound to its source item.',
                    priority: 'high',
                    value_driver: 'correctness',
                    estimated_effort: 'medium',
                    justification: 'Selectors must not cross backlog items.',
                },
            },
            candidate: {
                story_items: [{
                    story_title: 'Exact Story draft',
                    statement: 'As an operator, I review the intended Story.',
                    persona: 'Operator',
                    acceptance_criteria: ['The exact selector is preserved.'],
                    specification_evidence: [],
                    invest_assessment: validInvestAssessment(),
                    estimated_effort: 'S',
                    story_points: 2,
                    effort_rationale: 'Single focused operation.',
                    order_rationale: 'First priority slice.',
                }],
                is_complete: true,
                clarifying_questions: [],
            },
        },
    };
}

function directActionButton(action) {
    const label = { textContent: 'Generate Stories', dataset: {} };
    const status = { textContent: '', hidden: true };
    let button;
    const wrapper = {
        dataset: {},
        querySelector(selector) {
            return selector === '[data-delivery-action-status="true"]' ? status : null;
        },
        querySelectorAll() { return [button]; },
    };
    button = {
        _label: label,
        _status: status,
        _wrapper: wrapper,
        ariaBusy: null,
        disabled: false,
        dataset: {
            directAction: action.request_kind,
            deliveryActionNode: action.node_id,
            deliveryActionInstance: action.instance_key,
            deliveryActionHasInstance: action.instance_key === null || action.instance_key === undefined
                ? 'false'
                : 'true',
            deliveryActionEndpoint: action.endpoint,
            deliveryActionTransport: action.transport ?? '',
        },
        closest() { return wrapper; },
        querySelector(selector) {
            return selector === '[data-delivery-action-label="true"]' ? label : null;
        },
        setAttribute(name, value) {
            if (name === 'aria-busy') this.ariaBusy = value;
        },
        removeAttribute(name) {
            if (name === 'aria-busy') this.ariaBusy = null;
        },
    };
    return button;
}

function storyAction(overrides = {}) {
    return {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000001',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
        transport: 'semantic',
        ...overrides,
    };
}

function selectedScopeStory(overrides = {}) {
    return {
        story_id: 101,
        source_story_item_id: 'US-001',
        structurally_eligible: true,
        structural_eligibility_status: 'eligible',
        sprint_selection_state: 'selected',
        sprint_selection_state_fingerprint: 'sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
        selected_scope_fingerprint: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        dependency_safe: false,
        sprint_candidate: false,
        validation_status: 'validated',
        validation_failures: [],
        ...overrides,
    };
}

function sprintCandidateStory(overrides = {}) {
    return selectedScopeStory({
        dependency_safe: true,
        sprint_candidate: true,
        ...overrides,
    });
}

const structuralEvidenceScope = {
    proves: [
        'exact Story identity',
        'immutable accepted Story artifact/item binding',
        'accepted Backlog and Specification lineage',
        'parent-bounded Specification references',
        'required Story shape',
        'non-empty acceptance criteria',
        'current evidence and input fingerprints',
    ],
    does_not_prove: [
        'semantic/model quality',
        'product value',
        'human Sprint selection',
        'dependency safety',
        'Sprint candidacy',
        'Sprint-generation readiness',
    ],
};

function dependencyProjection(stories, edges = [], selectedStoryIds = null) {
    const selected = selectedStoryIds ?? stories
        .filter((story) => story.structurally_eligible
            && story.sprint_selection_state === 'selected')
        .map((story) => story.story_id);
    const selectedStory = stories.find((story) => story.story_id === selected[0]);
    return {
        stories,
        edges,
        selected_story_ids: selected,
        selected_scope_fingerprint: selectedStory?.selected_scope_fingerprint ?? null,
        structural_evidence_scope: structuralEvidenceScope,
    };
}

test('human lifecycle labels are the exact direct-Spec sequence', () => {
    const context = loadFrontend();
    assert.deepEqual(Array.from(context.lifecycleStageLabels()), [
        'Vision',
        'Product Goal',
        'Specification',
        'Backlog',
        'Roadmap',
        'Stories',
        'Sprint',
        'Execution',
        'Review',
    ]);
});

test('workflow position hides graph guards and machine fingerprints', () => {
    const context = loadFrontend();
    const markup = context.workflowPositionMarkup({
        graph_version: 'agileforge.workflow.v2',
        fact_fingerprint: 'sha256:secret-facts',
        decisions: [{
            node_id: 'backlog.generate',
            child_graph_id: 'backlog',
            request_kind: 'record_backlog_draft',
            category: 'available',
            reason_code: 'BACKLOG_REQUIRED',
            decision_fingerprint: 'sha256:secret-decision',
        }],
    });
    assert.ok(markup.includes('Backlog'));
    assert.ok(!markup.includes('secret-facts'));
    assert.ok(!markup.includes('secret-decision'));
    assert.ok(!markup.includes('backlog.generate'));
});

test('delivery generation submits the exact rendered Story selector', async () => {
    const requests = [];
    const context = loadFrontend(async (url, options = {}) => {
        requests.push({ url, options });
        return { ok: true, text: async () => '{}' };
    });
    const actions = [
        {
            node_id: 'planning.story.generate',
            instance_key: 'backlog_item:PBI-000001',
            request_kind: 'record_story_draft',
            endpoint: 'story/generate',
        },
        {
            node_id: 'planning.story.generate',
            instance_key: 'backlog_item:PBI-000002',
            request_kind: 'record_story_draft',
            endpoint: 'story/generate',
        },
    ];
    vm.runInContext(`selectedProjectId = 7; lifecycleState = ${JSON.stringify({
        actions,
        position: {},
        planningReviews: {},
    })};`, context);

    await context.runDirectAction(
        'record_story_draft',
        directActionButton(actions[1]),
    );

    const post = requests.find(({ options }) => options.method === 'POST');
    assert.ok(post);
    assert.equal(
        JSON.parse(post.options.body).instance_key,
        'backlog_item:PBI-000002',
    );
});

test('successful generation keeps its old control disabled when dashboard reload fails', async () => {
    let postCount = 0;
    const context = loadFrontend(async (_url, options = {}) => {
        if (options.method === 'POST') {
            postCount += 1;
            return { ok: true, text: async () => '{}' };
        }
        throw new Error('dashboard reload unavailable');
    });
    const action = storyAction();
    const button = directActionButton(action);
    vm.runInContext(`selectedProjectId = 7; lifecycleState = ${JSON.stringify({
        actions: [action],
        position: {},
        planningReviews: {},
    })};`, context);

    await context.runDirectAction('record_story_draft', button);
    await context.runDirectAction('record_story_draft', button);

    assert.equal(button.disabled, true);
    assert.equal(button.ariaBusy, null);
    assert.equal(button._wrapper.dataset.submitting, undefined);
    assert.equal(button._label.textContent, 'Generate Stories');
    assert.match(button._status.textContent, /could not reload/i);
    assert.equal(postCount, 1);
});

test('superseded dashboard reload does not re-enable the old generation control', async () => {
    let postCount = 0;
    const context = loadFrontend(async (_url, options = {}) => {
        if (options.method === 'POST') postCount += 1;
        return { ok: true, text: async () => '{}' };
    });
    const action = storyAction();
    const button = directActionButton(action);
    vm.runInContext(`selectedProjectId = 7; lifecycleState = ${JSON.stringify({
        actions: [action],
        position: {},
        planningReviews: {},
    })}; loadDashboard = async () => false;`, context);

    await context.runDirectAction('record_story_draft', button);
    await context.runDirectAction('record_story_draft', button);

    assert.equal(button.disabled, true);
    assert.equal(button.ariaBusy, null);
    assert.equal(button._wrapper.dataset.submitting, undefined);
    assert.equal(button._label.textContent, 'Generate Stories');
    assert.match(button._status.textContent, /could not reload/i);
    assert.equal(postCount, 1);
});

test('delivery generation rejects a rendered action whose binding changed', async () => {
    const rendered = storyAction();
    const changedActions = [
        storyAction({ node_id: 'planning.story.generate.changed' }),
        storyAction({ endpoint: 'story/generate/changed' }),
        storyAction({ transport: 'changed' }),
    ];

    for (const current of changedActions) {
        const requests = [];
        const context = loadFrontend(async (url, options = {}) => {
            requests.push({ url, options });
            return { ok: true, text: async () => '{}' };
        });
        vm.runInContext(`selectedProjectId = 7; lifecycleState = ${JSON.stringify({
            actions: [current],
            position: {},
            planningReviews: {},
        })};`, context);

        await context.runDirectAction(
            'record_story_draft',
            directActionButton(rendered),
        );

        assert.equal(
            requests.filter(({ options }) => options.method === 'POST').length,
            0,
        );
    }
});

test('delivery panel keeps a pending Story review beside another generation action', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000002',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
    };

    const markup = context.deliveryPanelMarkup(
        { decisions: [] },
        { stories: { items: [storyReview('backlog_item:PBI-000001')] } },
        [action],
    );

    assert.ok(markup.includes('data-planning-review-card="story"'));
    assert.ok(markup.includes('data-delivery-generation-action="record_story_draft"'));
});

test('Sprint generation asks the operator for a team', () => {
    const context = loadFrontend();
    const candidate = sprintCandidateStory();
    const markup = context.deliveryPanelMarkup(
        { decisions: [] },
        {},
        [{
            node_id: 'planning.sprint.plan',
            instance_key: null,
            request_kind: 'record_sprint_plan',
            endpoint: 'sprint/generate',
        }],
        {
            storyDependencies: dependencyProjection([candidate]),
            sprintCandidates: { items: [candidate] },
        },
    );

    assert.ok(markup.includes('data-delivery-generation-form="record_sprint_plan"'));
    assert.ok(markup.includes('name="team_name"'));
    assert.ok(markup.includes('required'));
    assert.ok(!markup.includes('value="Platform"'));
});

test('story generation controls render exact PBI IDs and requirement summaries, not array ordinals', () => {
    const context = loadFrontend();
    const actions = [
        {
            node_id: 'planning.story.generate',
            instance_key: 'backlog_item:PBI-000002',
            request_kind: 'record_story_draft',
            endpoint: 'story/generate',
            transport: 'semantic',
        },
        {
            node_id: 'planning.story.generate',
            instance_key: 'backlog_item:PBI-000004',
            request_kind: 'record_story_draft',
            endpoint: 'story/generate',
            transport: 'semantic',
        },
        {
            node_id: 'planning.story.generate',
            instance_key: 'backlog_item:PBI-000005',
            request_kind: 'record_story_draft',
            endpoint: 'story/generate',
            transport: 'semantic',
        },
        {
            node_id: 'planning.story.generate',
            instance_key: 'backlog_item:PBI-000006',
            request_kind: 'record_story_draft',
            endpoint: 'story/generate',
            transport: 'semantic',
        },
    ];
    const position = {
        decisions: actions.map((a) => ({
            node_id: a.node_id,
            instance_key: a.instance_key,
            request_kind: a.request_kind,
            category: 'available',
            reason_code: 'STORY_GENERATION_REQUIRED',
            recommendation_kind: 'required',
        })),
    };
    const reviews = {
        stories: {
            items: [storyReview('backlog_item:PBI-000003')],
        },
    };
    const appState = {
        storyPending: {
            items: [
                { backlog_item_id: 'PBI-000002', requirement: 'Support accepted Number List language.' },
                { backlog_item_id: 'PBI-000003', requirement: 'Reject negative numeric values.' },
                { backlog_item_id: 'PBI-000004', requirement: 'Provide the installed CLI.' },
                { backlog_item_id: 'PBI-000005', requirement: 'Verify through public behavior.' },
                { backlog_item_id: 'PBI-000006', requirement: 'Provide human-reviewable release evidence.' },
            ],
        },
    };

    const markup = context.deliveryPanelMarkup(position, reviews, actions, appState);

    // Exact PBI IDs and summaries are present
    assert.ok(markup.includes('Generate Stories for PBI-000002: Support accepted Number List language.'));
    assert.ok(markup.includes('Generate Stories for PBI-000004: Provide the installed CLI.'));
    assert.ok(markup.includes('Generate Stories for PBI-000005: Verify through public behavior.'));
    assert.ok(markup.includes('Generate Stories for PBI-000006: Provide human-reviewable release evidence.'));

    // Array ordinals are NEVER used as domain identity
    assert.ok(!markup.includes('backlog item 1'));
    assert.ok(!markup.includes('backlog item 2'));
    assert.ok(!markup.includes('backlog item 3'));
    assert.ok(!markup.includes('backlog item 4'));

    // Pending story review card is identified by exact PBI ID, not array index
    assert.ok(markup.includes('Story review for PBI-000003'));
    assert.ok(!markup.includes('Story review 1'));
});

test('single story generation control renders exact PBI ID and requirement', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000002',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
        transport: 'semantic',
    };
    const position = {
        decisions: [{
            node_id: action.node_id,
            instance_key: action.instance_key,
            request_kind: action.request_kind,
            category: 'available',
            reason_code: 'STORY_GENERATION_REQUIRED',
            recommendation_kind: 'required',
        }],
    };
    const appState = {
        storyPending: {
            items: [
                { backlog_item_id: 'PBI-000002', requirement: 'Support accepted Number List language.' },
            ],
        },
    };

    const markup = context.deliveryPanelMarkup(position, {}, [action], appState);

    assert.ok(markup.includes('Generate Stories for PBI-000002: Support accepted Number List language.'));
    assert.ok(!markup.includes('Generate Stories</span>'));
});

test('initial, revision, and correction story actions render distinct intent', () => {
    const context = loadFrontend();
    const appState = {
        storyPending: {
            items: [
                { backlog_item_id: 'PBI-000001', requirement: 'Provide arithmetic sum.' },
                { backlog_item_id: 'PBI-000002', requirement: 'Support number language.' },
                { backlog_item_id: 'PBI-000003', requirement: 'Reject negatives.' },
            ],
        },
    };

    // Initial generation
    const initialAction = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000002',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
    };
    const initialMarkup = context.deliveryPanelMarkup(
        {
            decisions: [{
                node_id: initialAction.node_id,
                instance_key: initialAction.instance_key,
                reason_code: 'STORY_GENERATION_REQUIRED',
                recommendation_kind: 'required',
            }],
        },
        {},
        [initialAction],
        appState,
    );
    assert.ok(initialMarkup.includes('Generate Stories for PBI-000002: Support number language.'));

    // Revision after feedback/rejection
    const revisionAction = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000003',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
    };
    const revisionMarkup = context.deliveryPanelMarkup(
        {
            decisions: [{
                node_id: revisionAction.node_id,
                instance_key: revisionAction.instance_key,
                reason_code: 'STORY_REVISION_REQUIRED',
                recommendation_kind: 'recovery',
            }],
        },
        {},
        [revisionAction],
        appState,
    );
    assert.ok(revisionMarkup.includes('Revise Stories for PBI-000003: Reject negatives.'));

    // Correction of accepted story
    const correctionAction = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000001',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
    };
    const correctionMarkup = context.deliveryPanelMarkup(
        {
            decisions: [{
                node_id: correctionAction.node_id,
                instance_key: correctionAction.instance_key,
                reason_code: 'STORY_CORRECTION_AVAILABLE',
                recommendation_kind: 'optional_reentry',
            }],
        },
        {},
        [correctionAction],
        appState,
    );
    assert.ok(correctionMarkup.includes('Correct Stories for PBI-000001: Provide arithmetic sum.'));
});

test('busy state and status preserve exact PBI identity and intent', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000002',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
    };
    const button = directActionButton(action);
    button._label.textContent = 'Generate Stories for PBI-000002: Support number language.';

    context.setDeliveryActionBusy(button, true, 'record_story_draft');

    assert.equal(button.ariaBusy, 'true');
    assert.equal(button.disabled, true);
    assert.equal(button._label.textContent, 'Generating Stories for PBI-000002...');
    assert.equal(button._status.textContent, 'Generating Stories for PBI-000002...');

    // Reset busy restores original label
    context.setDeliveryActionBusy(button, false, 'record_story_draft');
    assert.equal(button.ariaBusy, null);
    assert.equal(button.disabled, false);
    assert.equal(button._label.textContent, 'Generate Stories for PBI-000002: Support number language.');
});

test('story review confirmation modal identifies exact PBI ID and requirement', () => {
    const context = loadFrontend();
    const item = storyReview('backlog_item:PBI-000003');
    item.review.lineage.backlog_item.requirement = 'Reject any Number List containing a negative.';

    vm.runInContext(`lifecycleState = {
        planningReviews: {
            stories: {
                items: [${JSON.stringify(item)}],
            },
        },
    };`, context);

    const acceptBinding = context.capturePlanningReview('story', 0, 'accepted');
    assert.equal(acceptBinding.title, 'Accept Story review for PBI-000003: Reject any Number List containing a negative.');

    const changesBinding = context.capturePlanningReview('story', 0, 'feedback');
    assert.equal(changesBinding.title, 'Request changes for Story review for PBI-000003: Reject any Number List containing a negative.');

    const rejectBinding = context.capturePlanningReview('story', 0, 'rejected');
    assert.equal(rejectBinding.title, 'Reject Story review for PBI-000003: Reject any Number List containing a negative.');
});

test('story generation control is disabled when requirement summary is missing', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000002',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
        transport: 'semantic',
    };
    const position = {
        decisions: [{
            node_id: action.node_id,
            instance_key: action.instance_key,
            request_kind: action.request_kind,
            category: 'available',
            reason_code: 'STORY_GENERATION_REQUIRED',
            recommendation_kind: 'required',
        }],
    };
    // No storyPending items provided
    const markup = context.deliveryPanelMarkup(position, {}, [action], { storyPending: { items: [] } });

    assert.ok(markup.includes('disabled'));
    assert.ok(markup.includes('title="Requirement summary unavailable"'));
});

test('delivery generation details provide exact title, description, and intent for confirmation dialog', () => {
    const context = loadFrontend();
    const appState = {
        storyPending: {
            items: [
                { backlog_item_id: 'PBI-000001', requirement: 'Provide arithmetic sum.' },
                { backlog_item_id: 'PBI-000002', requirement: 'Support number language.' },
                { backlog_item_id: 'PBI-000003', requirement: 'Reject negatives.' },
            ],
        },
    };

    // Initial generation
    const genAction = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000002',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
    };
    const genDetails = context.deliveryGenerationActionDetails(
        genAction,
        { decisions: [{ node_id: genAction.node_id, instance_key: genAction.instance_key, reason_code: 'STORY_GENERATION_REQUIRED' }] },
        {},
        appState,
    );
    assert.equal(genDetails.intentVerb, 'Generate');
    assert.equal(genDetails.intentLabel, 'generation');
    assert.equal(genDetails.pbiId, 'PBI-000002');
    assert.equal(genDetails.requirement, 'Support number language.');

    // Revision
    const revAction = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000003',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
    };
    const revDetails = context.deliveryGenerationActionDetails(
        revAction,
        { decisions: [{ node_id: revAction.node_id, instance_key: revAction.instance_key, reason_code: 'STORY_REVISION_REQUIRED' }] },
        {},
        appState,
    );
    assert.equal(revDetails.intentVerb, 'Revise');
    assert.equal(revDetails.intentLabel, 'revision');
    assert.equal(revDetails.pbiId, 'PBI-000003');
    assert.equal(revDetails.requirement, 'Reject negatives.');

    // Correction
    const corrAction = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000001',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
    };
    const corrDetails = context.deliveryGenerationActionDetails(
        corrAction,
        { decisions: [{ node_id: corrAction.node_id, instance_key: corrAction.instance_key, reason_code: 'STORY_CORRECTION_AVAILABLE' }] },
        {},
        appState,
    );
    assert.equal(corrDetails.intentVerb, 'Correct');
    assert.equal(corrDetails.intentLabel, 'correction');
    assert.equal(corrDetails.pbiId, 'PBI-000001');
    assert.equal(corrDetails.requirement, 'Provide arithmetic sum.');
});

test('story generation control ignores candidate backlog reviews and stays disabled when storyPending is empty', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story.generate',
        instance_key: 'backlog_item:PBI-000002',
        request_kind: 'record_story_draft',
        endpoint: 'story/generate',
        transport: 'semantic',
    };
    const position = {
        decisions: [{
            node_id: action.node_id,
            instance_key: action.instance_key,
            request_kind: action.request_kind,
            category: 'available',
            reason_code: 'STORY_GENERATION_REQUIRED',
            recommendation_kind: 'required',
        }],
    };
    const reviews = {
        backlog: {
            candidate: {
                backlog_items: [
                    { backlog_item_id: 'PBI-000002', requirement: 'UNTRUSTED OR STALE CANDIDATE REQUIREMENT' },
                ],
            },
        },
    };
    // storyPending is empty
    const appState = { storyPending: { items: [] } };
    const details = context.deliveryGenerationActionDetails(action, position, reviews, appState);
    assert.equal(details.requirement, '');
    assert.equal(details.pbiId, 'PBI-000002');
    assert.equal(details.label, 'Generate Stories for PBI-000002');

    const markup = context.deliveryPanelMarkup(position, reviews, [action], appState);
    assert.ok(markup.includes('disabled'));
    assert.ok(markup.includes('title="Requirement summary unavailable"'));
    assert.ok(!markup.includes('UNTRUSTED OR STALE CANDIDATE REQUIREMENT'));
});

test('story readiness keeps structural proof separate from three-state Sprint selection', () => {
    const context = loadFrontend();
    const stories = [
        {
            story_id: 101,
            source_story_item_id: 'US-001',
            backlog_item_id: 'PBI-000001',
            story_points: 5,
            rank: '1',
            structurally_eligible: true,
            structural_eligibility_status: 'eligible',
            sprint_selection_state: 'unselected',
            sprint_selection_state_fingerprint: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            selected_scope_fingerprint: null,
            dependency_safe: false,
            sprint_candidate: false,
            content_accepted: true,
            validation_status: 'validated',
            validation_failures: [],
            readiness_blockers: [],
        },
    ];
    const appState = {
        storyPending: {
            items: [
                { backlog_item_id: 'PBI-000001', requirement: 'Implement parser core' },
            ],
        },
        storyDependencies: dependencyProjection(stories, [], []),
    };

    const markup = context.storyReadinessMarkup(stories, appState);
    assert.ok(markup.includes('Story readiness'));
    assert.ok(markup.includes('US-001'));
    assert.ok(markup.includes('(PBI-000001)'));
    assert.ok(markup.includes('Implement parser core'));
    assert.ok(markup.includes('Rank: 1'));
    assert.ok(markup.includes('Points: 5'));
    assert.ok(markup.includes('Structurally eligible'));
    assert.ok(markup.includes('Unselected'));
    assert.ok(markup.includes('Select for Sprint'));
    assert.ok(markup.includes('Defer'));
    assert.ok(markup.includes('data-story-selection-id="101"'));
    assert.ok(markup.includes('data-story-selection-fingerprint="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"'));
    assert.ok(markup.includes('Provider-free structural evidence proves:'));
    assert.ok(markup.includes('exact Story identity'));
    assert.ok(markup.includes('Sprint-generation readiness'));
    assert.ok(!markup.includes('Validate Story'));
    assert.ok(!markup.includes('Validated'));
});

test('story readiness renders selected and deferred intent separately and preserves selected intent through stale evidence', () => {
    const context = loadFrontend();
    const stories = [
        {
            story_id: 101,
            source_story_item_id: 'US-001',
            backlog_item_id: 'PBI-000001',
            story_points: 5,
            rank: '1',
            structurally_eligible: false,
            structural_eligibility_status: 'stale',
            sprint_selection_state: 'selected',
            sprint_selection_state_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
            selected_scope_fingerprint: null,
            dependency_safe: false,
            sprint_candidate: false,
            content_accepted: true,
            validation_status: 'validated',
            validation_failures: [],
            readiness_blockers: [],
        },
        {
            story_id: 102,
            source_story_item_id: 'US-002',
            backlog_item_id: 'PBI-000002',
            story_points: 3,
            rank: '2',
            structurally_eligible: true,
            structural_eligibility_status: 'eligible',
            sprint_selection_state: 'deferred',
            sprint_selection_state_fingerprint: 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
            selected_scope_fingerprint: null,
            dependency_safe: false,
            sprint_candidate: false,
            content_accepted: true,
            validation_status: 'validated',
            validation_failures: [],
            readiness_blockers: [],
        },
    ];
    const appState = {
        storyPending: { items: [] },
        storyDependencies: dependencyProjection(stories, [], []),
        sprintCandidates: { items: stories },
    };

    const markup = context.storyReadinessMarkup(stories, appState);
    assert.ok(markup.includes('Structural evidence stale'));
    assert.ok(markup.includes('Selected for Sprint'));
    assert.ok(markup.includes('Re-run structural checks'));
    assert.ok(markup.includes('Remove from Sprint selection'));
    assert.ok(markup.includes('Deferred'));
    assert.ok(markup.includes('data-story-selection-intent="select"'));
});

test('dependency review section renders when apply_story_dependencies action is available', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story_dependencies',
        request_kind: 'apply_story_dependencies',
        endpoint: 'story/dependencies/apply',
        transport: 'semantic',
    };
    const stories = [selectedScopeStory({ backlog_item_id: 'PBI-000001' })];
    const dependencies = dependencyProjection(stories);

    const markup = context.storyDependencyReviewMarkup(action, stories, dependencies);
    assert.ok(markup.includes('Dependency review required'));
    assert.ok(markup.includes('US-001'));
    assert.ok(markup.includes('(PBI-000001)'));
    assert.ok(markup.includes('data-apply-dependencies="true"'));
    assert.ok(markup.includes('Confirm dependencies'));
});

test('dependency review displays only candidate stories and candidate-contained edges', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story_dependencies',
        request_kind: 'apply_story_dependencies',
        endpoint: 'story/dependencies/apply',
        transport: 'semantic',
    };
    const candidates = [selectedScopeStory()];
    const dependencies = dependencyProjection([
        selectedScopeStory(),
        selectedScopeStory({
                story_id: 102,
                source_story_item_id: 'US-002',
                sprint_selection_state: 'unselected',
                sprint_selection_state_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        }),
    ], [
        { dependent_story_id: 102, prerequisite_story_id: 101, status: 'proposed', reason: 'US-002 needs US-001' },
    ]);

    const markup = context.storyDependencyReviewMarkup(action, dependencies.stories, dependencies);
    assert.ok(markup.includes('US-001'));
    assert.ok(!markup.includes('US-002'));
    assert.ok(markup.includes('None (independent stories)'));
    assert.ok(!markup.includes('102 -> 101'));
});

test('dependency review displays human-readable story identifiers, PBIs, and dependency reasons', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story_dependencies',
        request_kind: 'apply_story_dependencies',
        endpoint: 'story/dependencies/apply',
        transport: 'semantic',
    };
    const candidates = [
        selectedScopeStory({ backlog_item_id: 'PBI-000001' }),
        selectedScopeStory({
            story_id: 102,
            source_story_item_id: 'US-002',
            backlog_item_id: 'PBI-000002',
            sprint_selection_state_fingerprint: 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
        }),
    ];
    const dependencies = dependencyProjection(candidates, [
        { dependent_story_id: 102, prerequisite_story_id: 101, status: 'proposed', reason: 'US-002 requires data model from US-001' },
    ]);

    const markup = context.storyDependencyReviewMarkup(action, candidates, dependencies);
    assert.ok(markup.includes('US-001 (PBI-000001)'));
    assert.ok(markup.includes('US-002 (PBI-000002)'));
    assert.ok(markup.includes('US-002 (PBI-000002) -&gt; US-001 (PBI-000001) - US-002 requires data model from US-001'));
    assert.ok(markup.includes('Confirm dependencies'));
});

test('story readiness shows current rule diagnostics without suggesting another approval-like check', () => {
    const context = loadFrontend();
    const stories = [
        {
            story_id: 101,
            source_story_item_id: 'US-001',
            backlog_item_id: 'PBI-000001',
            status: 'accepted',
            story_points: 3,
            rank: '0|hzzzzz:',
            content_accepted: true,
            structurally_eligible: false,
            structural_eligibility_status: 'ineligible',
            sprint_selection_state: 'unselected',
            sprint_selection_state_fingerprint: 'sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
            selected_scope_fingerprint: null,
            dependency_safe: false,
            sprint_candidate: false,
            readiness_blockers: [],
            validation_status: 'failed',
            validation_failures: [
                {
                    code: 'STORY_SPEC_REFERENCE_INVALID',
                    message: 'Story references invalid specification items: REQ.099',
                },
            ],
        },
    ];
    const appState = {
        storyPending: { items: [] },
        storyDependencies: { stories },
        sprintCandidates: { items: [] },
    };

    const readinessMarkup = context.storyReadinessMarkup(stories, appState);
    assert.ok(readinessMarkup.includes('Structural eligibility failed'));
    assert.ok(readinessMarkup.includes('data-story-validation-diagnostics="true"'));
    assert.ok(readinessMarkup.includes('STORY_SPEC_REFERENCE_INVALID'));
    assert.ok(readinessMarkup.includes('Story references invalid specification items: REQ.099'));
    assert.ok(!readinessMarkup.includes('Re-run structural checks'));
    assert.ok(!readinessMarkup.includes('Validate Story'));
});

test('malformed readiness projections fail closed and hide selection controls', () => {
    const context = loadFrontend();
    const stories = [
        {
            story_id: 102,
            source_story_item_id: 'US-002',
            backlog_item_id: 'PBI-000002',
            status: 'accepted',
            story_points: 5,
            rank: '0|hzzzzz:1',
            content_accepted: true,
            structurally_eligible: true,
            structural_eligibility_status: 'eligible',
            sprint_selection_state: 'selected',
            // Missing exact selection state fingerprint.
            selected_scope_fingerprint: null,
            dependency_safe: true,
            sprint_candidate: true,
            readiness_blockers: ['PREREQUISITE_STORY_101_INCOMPLETE'],
            validation_status: 'validated',
            validation_failures: [],
        },
    ];
    const appState = {
        storyPending: { items: [] },
        storyDependencies: { stories },
        sprintCandidates: { items: [] },
    };

    const readinessMarkup = context.storyReadinessMarkup(stories, appState);
    assert.ok(readinessMarkup.includes('Story state unavailable'));
    assert.ok(readinessMarkup.includes('aria-disabled="true"'));
    assert.ok(!readinessMarkup.includes('data-story-selection-id="102"'));
});

test('structural and selection mutation payloads bind exact Story state and reuse an idempotency key for retry', async () => {
    const requests = [];
    let attempts = 0;
    const context = loadFrontend(async (path, options) => {
        requests.push({ path, body: JSON.parse(options.body) });
        attempts += 1;
        if (attempts === 1) throw new Error('temporary network failure');
        return { ok: true, status: 200, text: async () => '{"ok":true,"data":{},"errors":[]}' };
    });
    const fingerprint = 'sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee';
    await context.postStorySelectionMutation(1, 101, 'select', fingerprint);
    assert.equal(requests.length, 2);
    assert.equal(requests[0].path, '/api/projects/1/story/sprint-selection');
    assert.deepEqual(requests[0].body, {
        story_id: 101,
        intent: 'select',
        expected_state_fingerprint: fingerprint,
        rationale: 'Selected for Sprint from dashboard.',
        actor: 'dashboard-ui',
        idempotency_key: 'dashboard-uuid-1',
    });
    assert.equal(requests[0].body.idempotency_key, requests[1].body.idempotency_key);
    assert.deepEqual(JSON.parse(JSON.stringify(context.structuralEligibilityMutationPayload(101))), {
        story_ids: [101],
        actor: 'dashboard-ui',
        idempotency_key: 'dashboard-uuid-1',
    });
});

test('Story mutation token lock survives a 409 authority reload and releases only on recovery', async () => {
    let conflict = true;
    const context = loadFrontend(async (path) => {
        if (conflict && path.endsWith('/story/dependencies')) {
            return {
                ok: false,
                status: 409,
                text: async () => JSON.stringify({
                    detail: { error: { code: 'STALE_POSITION', message: 'Projection changed.' } },
                }),
            };
        }
        return { ok: true, status: 200, text: async () => '{"data":{}}' };
    });
    const story = selectedScopeStory({ sprint_selection_state: 'unselected', selected_scope_fingerprint: null });
    vm.runInContext(`
        selectedProjectId = 7;
        activeStoryMutation = {
            token: 'story-token-409',
            phase: 'awaiting_authority',
            storyId: 101,
            intent: 'select',
        };
    `, context);

    await assert.rejects(context.loadDashboard());
    const locked = context.storyReadinessMarkup([story], {
        storyDependencies: { stories: [story], edges: [] },
    });
    assert.ok(locked.includes('data-story-selection-intent="select" disabled aria-disabled="true"'));
    assert.ok(locked.includes('Current project projection is reloading; controls remain locked.'));
    assert.notEqual(vm.runInContext('activeStoryMutation', context), null);

    conflict = false;
    assert.strictEqual(await context.loadDashboard(), true);
    assert.strictEqual(vm.runInContext('activeStoryMutation', context), null);
});

test('dependency review submits the backend-projected next-Sprint IDs without rederiving history', () => {
    const context = loadFrontend();
    const completed = selectedScopeStory({ story_id: 101, source_story_item_id: 'US-completed' });
    const future = selectedScopeStory({
        story_id: 102,
        source_story_item_id: 'US-future',
        sprint_selection_state_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    });
    const dependencies = {
        stories: [completed, future],
        selected_story_ids: [102],
        selected_scope_fingerprint: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        edges: [],
    };

    const projection = context.selectedScopeDependencies([completed, future], dependencies);
    assert.strictEqual(projection.isWellFormed, true);
    assert.deepEqual(JSON.parse(JSON.stringify(projection.scopeIds)), [102]);
    assert.strictEqual(projection.scopeFingerprint, dependencies.selected_scope_fingerprint);
});

test('browser renders the exact structural proof and non-proof disclosure from the projection', () => {
    const context = loadFrontend();
    const story = selectedScopeStory({ sprint_selection_state: 'unselected', selected_scope_fingerprint: null });
    const scope = {
        proves: [
            'exact Story identity',
            'immutable accepted Story artifact/item binding',
            'accepted Backlog and Specification lineage',
            'parent-bounded Specification references',
            'required Story shape',
            'non-empty acceptance criteria',
            'current evidence and input fingerprints',
        ],
        does_not_prove: [
            'semantic/model quality',
            'product value',
            'human Sprint selection',
            'dependency safety',
            'Sprint candidacy',
            'Sprint-generation readiness',
        ],
    };

    const markup = context.storyReadinessMarkup([story], {
        storyDependencies: { stories: [story], edges: [], structural_evidence_scope: scope },
    });
    for (const item of [...scope.proves, ...scope.does_not_prove]) {
        assert.ok(markup.includes(item));
    }
});

test('selected scope retains external prerequisites and excludes unselected dependents', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story_dependencies',
        request_kind: 'apply_story_dependencies',
        endpoint: 'story/dependencies/apply',
        transport: 'semantic',
    };
    const selected = selectedScopeStory({ backlog_item_id: 'PBI-000001' });
    const external = selectedScopeStory({
        story_id: 102,
        source_story_item_id: 'US-002',
        backlog_item_id: 'PBI-000002',
        sprint_selection_state: 'unselected',
        sprint_selection_state_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    });
    const dependencies = dependencyProjection([selected, external], [
        { dependent_story_id: 101, prerequisite_story_id: 102, status: 'proposed', reason: 'US-001 requires external US-002.' },
        { dependent_story_id: 102, prerequisite_story_id: 101, status: 'proposed', reason: 'Unselected dependent stays out of scope.' },
    ]);

    const result = context.selectedScopeDependencies([selected, external], dependencies);
    assert.strictEqual(result.isWellFormed, true);
    assert.deepEqual(JSON.parse(JSON.stringify(result.scopeEdges)), [
        { dependent_story_id: 101, prerequisite_story_id: 102, reason: 'US-001 requires external US-002.', isExternal: true },
    ]);
    const markup = context.storyDependencyReviewMarkup(action, [selected, external], dependencies);
    assert.ok(markup.includes('External/excluded prerequisite'));
    assert.ok(markup.includes('US-002 (PBI-000002)'));
    assert.ok(!markup.includes('Unselected dependent stays out of scope.'));
});

test('dependency and readiness contradictions fail closed while missing evidence is labelled precisely', () => {
    const context = loadFrontend();
    const selected = selectedScopeStory();
    const missing = selectedScopeStory({
        story_id: 102,
        source_story_item_id: 'US-002',
        structurally_eligible: false,
        structural_eligibility_status: 'stale',
        sprint_selection_state: 'unselected',
        sprint_selection_state_fingerprint: 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
        selected_scope_fingerprint: null,
        validation_status: 'unvalidated',
        validation_failures: [],
    });
    const missingMarkup = context.storyReadinessMarkup([missing]);
    assert.ok(missingMarkup.includes('Structural evidence missing'));
    assert.ok(missingMarkup.includes('Re-run structural checks'));

    const contradictoryEligible = selectedScopeStory({ validation_failures: [{ code: 'BAD', message: 'Should not accompany eligibility.' }] });
    assert.strictEqual(context.parseStoryReadinessProjection(contradictoryEligible), null);
    assert.strictEqual(context.selectedScopeDependencies([selected], { stories: [selected] }).isWellFormed, false);
    assert.strictEqual(context.selectedScopeDependencies([selected, selected], { stories: [selected, selected], edges: [] }).isWellFormed, false);
    assert.strictEqual(context.selectedScopeDependencies([selected], { stories: [selected], edges: [
        { dependent_story_id: 101, prerequisite_story_id: 102, reason: 'Once.' },
        { dependent_story_id: 101, prerequisite_story_id: 102, reason: 'Duplicate.' },
    ] }).isWellFormed, false);
});

test('malformed Sprint candidate projection blocks the generated Sprint form and transport', () => {
    const context = loadFrontend();
    const sprintAction = {
        node_id: 'planning.sprint.plan',
        request_kind: 'record_sprint_plan',
        endpoint: 'sprint/generate',
        transport: 'semantic',
        instance_key: null,
    };
    const malformed = [{ story_id: 101, sprint_candidate: true }];
    const markup = context.deliveryPanelMarkup({}, {}, [sprintAction], {
        storyDependencies: { stories: [] },
        sprintCandidates: { items: malformed },
    });
    assert.strictEqual(context.canGenerateSprintPlan({ sprintCandidates: { items: malformed } }), false);
    assert.ok(markup.includes('Sprint candidate projection unavailable'));
    assert.ok(!markup.includes('data-delivery-generation-form="record_sprint_plan"'));
});

test('Sprint generation binds canonical candidates to the current dependency scope', () => {
    const requests = [];
    const context = loadFrontend(async (path) => {
        requests.push(path);
        return { ok: true, status: 200, text: async () => '{"status":"success","data":{}}' };
    });
    const sprintAction = {
        node_id: 'planning.sprint.plan',
        request_kind: 'record_sprint_plan',
        endpoint: 'sprint/generate',
        transport: 'semantic',
        instance_key: null,
    };
    const candidate = sprintCandidateStory();
    const currentStory = selectedScopeStory({
        selected_scope_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    });
    const appState = {
        storyDependencies: dependencyProjection([currentStory]),
        sprintCandidates: { items: [candidate] },
    };

    const markup = context.deliveryPanelMarkup({}, {}, [sprintAction], appState);
    assert.strictEqual(context.canGenerateSprintPlan(appState), false);
    assert.ok(markup.includes('Sprint candidate projection unavailable'));
    assert.ok(!markup.includes('data-delivery-generation-form="record_sprint_plan"'));
    assert.deepEqual(requests, []);
});

test('scope parser rejects a conflicting fingerprint on an unselected Story', () => {
    const context = loadFrontend();
    const selected = selectedScopeStory();
    const unselected = selectedScopeStory({
        story_id: 102,
        source_story_item_id: 'US-002',
        sprint_selection_state: 'unselected',
        sprint_selection_state_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        selected_scope_fingerprint: 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
    });

    assert.strictEqual(
        context.selectedScopeDependencies([selected, unselected], {
            stories: [selected, unselected],
            edges: [],
        }).isWellFormed,
        false,
    );
});

test('dependency scope excludes rejected edges and rejects self edges before transport', () => {
    const context = loadFrontend();
    const selected = selectedScopeStory();
    const external = selectedScopeStory({
        story_id: 102,
        source_story_item_id: 'US-002',
        sprint_selection_state: 'unselected',
        sprint_selection_state_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    });
    const rejected = context.selectedScopeDependencies([selected, external], dependencyProjection(
        [selected, external], [{
            dependent_story_id: 101,
            prerequisite_story_id: 102,
            status: 'rejected',
            reason: 'Do not reactivate this edge.',
        }],
    ));
    assert.strictEqual(rejected.isWellFormed, true);
    assert.deepEqual(JSON.parse(JSON.stringify(rejected.scopeEdges)), []);
    assert.strictEqual(
        context.selectedScopeDependencies([selected], dependencyProjection(
            [selected], [{
                dependent_story_id: 101,
                prerequisite_story_id: 101,
                status: 'proposed',
                reason: 'Malformed self edge.',
            }],
        )).isWellFormed,
        false,
    );
});

test('dependency controls remain locked after a successful mutation cannot reload', () => {
    const context = loadFrontend();
    assert.strictEqual(context.shouldUnlockDependencyMutation(true, false), false);
    assert.strictEqual(context.shouldUnlockDependencyMutation(true, true), false);
    assert.strictEqual(context.shouldUnlockDependencyMutation(false, false), true);
});

test('a dashboard load started during dependency submission cannot release its token', async () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story_dependencies',
        request_kind: 'apply_story_dependencies',
        endpoint: 'story/dependencies/apply',
        transport: 'semantic',
    };
    const story = selectedScopeStory();
    vm.runInContext(`
        selectedProjectId = 7;
        activeDependencyMutation = {
            token: 'dependency-token-1',
            phase: 'submitting',
            payload: {
                selected_story_ids: [101],
                reviewed_edges: [],
                actor: 'dashboard-ui',
                idempotency_key: 'dashboard-uuid-1',
            },
            button: null,
        };
    `, context);

    assert.strictEqual(await context.loadDashboard(), true);

    const active = JSON.parse(vm.runInContext(
        'JSON.stringify(activeDependencyMutation)',
        context,
    ));
    assert.notEqual(active, null);
    assert.equal(active.token, 'dependency-token-1');
    assert.equal(active.phase, 'submitting');
    const markup = context.storyDependencyReviewMarkup(
        action,
        [story],
        { stories: [story], edges: [] },
    );
    assert.ok(markup.includes('disabled aria-disabled="true"'));
    assert.ok(markup.includes('aria-busy="true"'));
    assert.ok(markup.includes('Dependency review is being submitted'));
    assert.ok(!markup.includes('Dependency review was accepted'));
});

test('rejected Story mutations restore every control to its exact prior state', () => {
    const context = loadFrontend();
    const disabledControl = {
        disabled: true,
        attributes: new Map([['aria-disabled', 'true']]),
        getAttribute(name) { return this.attributes.get(name) ?? null; },
        setAttribute(name, value) { this.attributes.set(name, value); },
        removeAttribute(name) { this.attributes.delete(name); },
    };
    const enabledControl = {
        disabled: false,
        attributes: new Map(),
        getAttribute(name) { return this.attributes.get(name) ?? null; },
        setAttribute(name, value) { this.attributes.set(name, value); },
        removeAttribute(name) { this.attributes.delete(name); },
    };
    const states = context.captureStoryControlStates([disabledControl, enabledControl]);
    disabledControl.disabled = false;
    disabledControl.removeAttribute('aria-disabled');
    enabledControl.disabled = true;
    enabledControl.setAttribute('aria-disabled', 'true');
    context.restoreStoryControlStates(states);
    assert.equal(disabledControl.disabled, true);
    assert.equal(disabledControl.getAttribute('aria-disabled'), 'true');
    assert.equal(enabledControl.disabled, false);
    assert.equal(enabledControl.getAttribute('aria-disabled'), null);
});

test('dependency review disables confirm button when canonical candidate projection is missing', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story_dependencies',
        request_kind: 'apply_story_dependencies',
        endpoint: 'story/dependencies/apply',
        transport: 'semantic',
    };
    const dependencies = {
        stories: [selectedScopeStory()],
        edges: [],
    };

    // When candidates is null (missing sprintCandidates.items)
    const markup = context.storyDependencyReviewMarkup(action, null, dependencies);
    assert.ok(markup.includes('Unavailable (current selected scope missing or malformed)'));
    assert.ok(markup.includes('disabled'));
    assert.ok(markup.includes('aria-disabled="true"'));
});

test('storyItemMarkup renders explainable INVEST assessment across all 6 dimensions', () => {
    const context = loadFrontend();
    const story = {
        story_title: 'Calculate values',
        statement: 'As a user, I want to calculate values, so that I learn.',
        persona: 'user',
        estimated_effort: 'S',
        acceptance_criteria: ['Verify calculation passes.'],
        specification_evidence: [],
        invest_assessment: {
            independent: {
                result: 'pass',
                rationale: 'Self-contained calculation logic.',
                evidence: 'No dependencies on unbuilt stories.',
            },
            negotiable: {
                result: 'pass',
                rationale: 'Calculation approach can be refined.',
                evidence: 'Focuses on outcome.',
            },
            valuable: {
                result: 'pass',
                rationale: 'Provides core calculation capability.',
                evidence: 'Directly addresses user need.',
            },
            estimable: {
                result: 'pass',
                rationale: 'Clear scope for estimation.',
                evidence: 'Two discrete criteria.',
            },
            small: {
                result: 'concern',
                rationale: 'Multiple operations included.',
                evidence: 'Effort is S but covers add and subtract.',
            },
            testable: {
                result: 'pass',
                rationale: 'Deterministic criteria.',
                evidence: 'Verify calculation passes.',
            },
        },
        order: 1,
        rank: '101',
        order_rationale: 'Foundational parser operations.',
        story_points: 2,
        estimated_effort: 'S',
        effort_rationale: 'Single straightforward parser operation.',
        research_caveats: ['Requires standard floating point behavior.'],
        dependency_candidates: [
            {
                prerequisite_ref: 'US-0001',
                reason: 'Parser needed first',
                confidence: 'explicit',
            },
        ],
    };

    const markup = context.storyItemMarkup(story);
    assert.ok(markup.includes('INVEST assessment'));
    assert.ok(markup.includes('Independent'));
    assert.ok(markup.includes('Negotiable'));
    assert.ok(markup.includes('Valuable'));
    assert.ok(markup.includes('Estimable'));
    assert.ok(markup.includes('Small'));
    assert.ok(markup.includes('Testable'));
    assert.ok(markup.includes('Pass'));
    assert.ok(markup.includes('Concern'));
    assert.ok(markup.includes('Self-contained calculation logic.'));
    assert.ok(markup.includes('No dependencies on unbuilt stories.'));
    assert.ok(markup.includes('Story order within PBI:</strong> 1 <span class="text-slate-500">(Derived rank: 101)</span>'));
    assert.ok(markup.includes('Estimated effort:</strong> S (derived: 2 pts)'));
    assert.ok(markup.includes('Order rationale:</strong> Foundational parser operations.'));
    assert.ok(markup.includes('Effort rationale:</strong> Single straightforward parser operation.'));
    assert.ok(markup.includes('Requires standard floating point behavior.'));
    assert.ok(markup.includes('Prerequisite:</strong> US-0001'));
    assert.ok(markup.includes('Parser needed first'));
});

test('dependencyCandidatesMarkup renders explicit none proposed message when empty', () => {
    const context = loadFrontend();
    const emptyMarkup = context.dependencyCandidatesMarkup([]);
    assert.ok(emptyMarkup.includes('Proposed dependencies'));
    assert.ok(emptyMarkup.includes('None proposed'));
});

test('investAssessmentMarkup renders explicit error on missing or malformed assessment', () => {
    const context = loadFrontend();
    const missingMarkup = context.investAssessmentMarkup(null);
    assert.ok(missingMarkup.includes('data-invest-assessment="invalid"'));
    assert.ok(missingMarkup.includes('Quality Assessment Incomplete'));
    assert.ok(missingMarkup.includes('Acceptance is disabled.'));

    // Incomplete dimensions
    const incomplete = validInvestAssessment();
    delete incomplete.small;
    const incompleteMarkup = context.investAssessmentMarkup(incomplete);
    assert.ok(incompleteMarkup.includes('data-invest-assessment="invalid"'));
    assert.ok(incompleteMarkup.includes('Missing / Invalid'));

    // Blank rationale
    const blankRationale = validInvestAssessment();
    blankRationale.valuable.rationale = '   ';
    const blankRationaleMarkup = context.investAssessmentMarkup(blankRationale);
    assert.ok(blankRationaleMarkup.includes('data-invest-assessment="invalid"'));

    // Invalid result string
    const badResult = validInvestAssessment();
    badResult.testable.result = 'maybe';
    const badResultMarkup = context.investAssessmentMarkup(badResult);
    assert.ok(badResultMarkup.includes('data-invest-assessment="invalid"'));

    // Coercion test: non-string rationale, object evidence, whitespace-padded result
    const coercedMalformed = validInvestAssessment();
    coercedMalformed.independent = {
        result: ' PASS ',
        rationale: 123,
        evidence: { source: 'REQ.1' },
    };
    assert.strictEqual(context.isWellFormedInvestDimension(coercedMalformed.independent), false);
    assert.strictEqual(context.isWellFormedInvestAssessment(coercedMalformed), false);
    const coercedMarkup = context.investAssessmentMarkup(coercedMalformed);
    assert.ok(coercedMarkup.includes('data-invest-assessment="invalid"'));
    assert.ok(coercedMarkup.includes('Missing / Invalid rationale'));
    assert.ok(coercedMarkup.includes('Missing / Invalid evidence'));

    // Uppercase result is rejected (strict lowercase enum required)
    const upperCaseResult = validInvestAssessment();
    upperCaseResult.independent.result = 'PASS';
    assert.strictEqual(context.isWellFormedInvestDimension(upperCaseResult.independent), false);

    // Extra keys on dimension are rejected
    const extraDimKeys = validInvestAssessment();
    extraDimKeys.independent.extra_key = 'unexpected';
    assert.strictEqual(context.isWellFormedInvestDimension(extraDimKeys.independent), false);

    // Extra keys on assessment object are rejected
    const extraAssessmentKeys = validInvestAssessment();
    extraAssessmentKeys.unknown_dimension = { result: 'pass', rationale: 'R', evidence: 'E' };
    assert.strictEqual(context.isWellFormedInvestAssessment(extraAssessmentKeys), false);
});

test('planningReviewCardMarkup disables Accept and renders error banner for invalid story review', () => {
    const context = loadFrontend();
    const item = storyReview('backlog_item:PBI-000003');
    // Remove invest_assessment to simulate missing quality evidence
    delete item.review.candidate.story_items[0].invest_assessment;

    const markup = context.planningReviewCardMarkup('Story review for PBI-000003', item, 'story', 0);
    assert.ok(markup.includes('data-review-error="invalid-story-evidence"'));
    assert.ok(markup.includes('data-review-decision="accepted" disabled class='));
    assert.ok(markup.includes('Acceptance disabled: required INVEST, sizing, or ordering evidence is missing or malformed'));
    assert.ok(markup.includes('Request changes'));
    assert.ok(markup.includes('Reject'));

    vm.runInContext(`lifecycleState = {
        planningReviews: {
            stories: {
                items: [${JSON.stringify(item)}],
            },
        },
    };`, context);

    // planningReviewBinding must fail closed and return null for accepted decision
    const acceptBinding = context.capturePlanningReview('story', 0, 'accepted');
    assert.strictEqual(acceptBinding, null);

    // But Request changes and Reject remain possible
    const changesBinding = context.capturePlanningReview('story', 0, 'feedback');
    assert.notStrictEqual(changesBinding, null);
    const rejectBinding = context.capturePlanningReview('story', 0, 'rejected');
    assert.notStrictEqual(rejectBinding, null);
});

test('planningReviewCardMarkup names missing rationale evidence when disabling Accept', () => {
    const context = loadFrontend();
    const item = storyReview('backlog_item:PBI-000003');
    delete item.review.candidate.story_items[0].effort_rationale;

    const markup = context.planningReviewCardMarkup('Story review for PBI-000003', item, 'story', 0);
    assert.ok(markup.includes('data-review-error="invalid-story-evidence"'));
    assert.ok(markup.includes('required INVEST, sizing, or ordering evidence is missing or malformed'));
    assert.ok(markup.includes('data-review-decision="accepted" disabled'));
});

test('planningReviewCardMarkup enables Accept when story review has valid INVEST assessment', () => {
    const context = loadFrontend();
    const item = storyReview('backlog_item:PBI-000003');

    const markup = context.planningReviewCardMarkup('Story review for PBI-000003', item, 'story', 0);
    assert.ok(!markup.includes('data-review-error="invalid-story-evidence"'));
    assert.ok(markup.includes('data-review-decision="accepted" class='));
    assert.ok(!markup.includes('data-review-decision="accepted" disabled'));
    assert.ok(markup.includes('>Accept</button>'));

    vm.runInContext(`lifecycleState = {
        planningReviews: {
            stories: {
                items: [${JSON.stringify(item)}],
            },
        },
    };`, context);

    const acceptBinding = context.capturePlanningReview('story', 0, 'accepted');
    assert.notStrictEqual(acceptBinding, null);
    assert.equal(acceptBinding.decision, 'accepted');
});

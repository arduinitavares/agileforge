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
    const markup = context.deliveryPanelMarkup(
        { decisions: [] },
        {},
        [{
            node_id: 'planning.sprint.plan',
            instance_key: null,
            request_kind: 'record_sprint_plan',
            endpoint: 'sprint/generate',
        }],
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

test('story readiness section renders unvalidated story with exact ID, parent PBI, rank, points, and validate button', () => {
    const context = loadFrontend();
    const stories = [
        {
            story_id: 101,
            source_story_item_id: 'US-001',
            backlog_item_id: 'PBI-000001',
            story_points: 5,
            rank: '1',
            sprint_candidate: false,
            content_accepted: true,
            readiness_blockers: ['STORY_VALIDATION_REQUIRED'],
        },
    ];
    const appState = {
        storyPending: {
            items: [
                { backlog_item_id: 'PBI-000001', requirement: 'Implement parser core' },
            ],
        },
    };

    const markup = context.storyReadinessMarkup(stories, appState);
    assert.ok(markup.includes('Story readiness'));
    assert.ok(markup.includes('US-001'));
    assert.ok(markup.includes('(PBI-000001)'));
    assert.ok(markup.includes('Implement parser core'));
    assert.ok(markup.includes('Rank: 1'));
    assert.ok(markup.includes('Points: 5'));
    assert.ok(markup.includes('Unvalidated'));
    assert.ok(markup.includes('data-story-validate-id="101"'));
    assert.ok(markup.includes('Validate Story'));
});

test('story readiness section renders validated badge and candidate pool renders candidate stories', () => {
    const context = loadFrontend();
    const stories = [
        {
            story_id: 101,
            source_story_item_id: 'US-001',
            backlog_item_id: 'PBI-000001',
            story_points: 5,
            rank: '1',
            sprint_candidate: true,
            content_accepted: true,
            readiness_blockers: [],
        },
    ];
    const appState = {
        storyPending: { items: [] },
        storyDependencies: { stories },
        sprintCandidates: { items: stories },
    };

    const readinessMarkup = context.storyReadinessMarkup(stories, appState);
    assert.ok(readinessMarkup.includes('Validated'));
    assert.ok(!readinessMarkup.includes('data-story-validate-id'));

    const candidateMarkup = context.sprintCandidatePoolMarkup(stories);
    assert.ok(candidateMarkup.includes('Sprint candidate pool'));
    assert.ok(candidateMarkup.includes('1 candidate ready'));
    assert.ok(candidateMarkup.includes('US-001'));
    assert.ok(candidateMarkup.includes('(PBI-000001)'));
});

test('dependency review section renders when apply_story_dependencies action is available', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'planning.story_dependencies',
        request_kind: 'apply_story_dependencies',
        endpoint: 'story/dependencies/apply',
        transport: 'semantic',
    };
    const stories = [
        { story_id: 101, source_story_item_id: 'US-001', backlog_item_id: 'PBI-000001' },
    ];
    const dependencies = { edges: [] };

    const markup = context.storyDependencyReviewMarkup(action, stories, dependencies);
    assert.ok(markup.includes('Dependency review required'));
    assert.ok(markup.includes('US-001'));
    assert.ok(markup.includes('data-apply-dependencies="true"'));
    assert.ok(markup.includes('Confirm dependencies'));
});

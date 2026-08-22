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

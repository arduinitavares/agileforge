import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const fixture = JSON.parse(fs.readFileSync(
    path.resolve(import.meta.dirname, 'fixtures/workflow_position.json'),
    'utf8',
));
const sourcePath = path.resolve(import.meta.dirname, '../frontend/project.js');
const source = fs.readFileSync(sourcePath, 'utf8');
const html = fs.readFileSync(
    path.resolve(import.meta.dirname, '../frontend/project.html'),
    'utf8',
);

function attributeDataset(tag) {
    const dataset = {};
    for (const match of tag.matchAll(/data-([a-z-]+)="([^"]*)"/g)) {
        const name = match[1].replace(/-([a-z])/g, (_value, letter) => letter.toUpperCase());
        dataset[name] = match[2];
    }
    return dataset;
}

class FakeButton {
    constructor(dataset) {
        this.dataset = dataset;
        this.disabled = false;
        this.listeners = new Map();
    }

    addEventListener(name, listener) {
        this.listeners.set(name, listener);
    }

    click() {
        return this.listeners.get('click')?.();
    }
}

class FakePanel {
    constructor() {
        this._innerHTML = '';
        this.buttons = [];
        this.textContent = '';
    }

    set innerHTML(value) {
        this._innerHTML = value;
        this.buttons = [...value.matchAll(/<button\b[^>]*class="[^"]*workflow-action[^"]*"[^>]*>/g)]
            .map((match) => new FakeButton(attributeDataset(match[0])));
    }

    get innerHTML() {
        return this._innerHTML;
    }

    querySelectorAll(selector) {
        return selector === '.workflow-action' ? this.buttons : [];
    }
}

function createFrontendHarness(fetchImpl) {
    const panel = new FakePanel();
    const domListeners = new Map();
    const alerts = [];
    let uuidSequence = 0;
    const document = {
        createElement() {
            return {
                _textContent: '',
                innerHTML: '',
                set textContent(value) {
                    this._textContent = String(value);
                    this.innerHTML = this._textContent
                        .replaceAll('&', '&amp;')
                        .replaceAll('<', '&lt;')
                        .replaceAll('>', '&gt;')
                        .replaceAll('"', '&quot;');
                },
                get textContent() {
                    return this._textContent;
                },
            };
        },
        getElementById(id) {
            return id === 'workflow-position-panel' ? panel : null;
        },
        querySelector() {
            return null;
        },
    };
    const window = {
        addEventListener(name, listener) {
            domListeners.set(name, listener);
        },
        alert(message) {
            alerts.push(message);
        },
        prompt() {
            return '{}';
        },
        location: { href: '' },
    };
    const context = vm.createContext({
        console,
        crypto: { randomUUID: () => `uuid-${uuidSequence += 1}` },
        document,
        fetch: fetchImpl,
        setTimeout,
        URLSearchParams,
        window,
    });
    vm.runInContext(source, context, { filename: sourcePath });
    return { alerts, context, domListeners, panel };
}

function availableDecision({
    nodeId,
    instanceKey = null,
    decisionFingerprint,
    requestKind,
}) {
    return {
        node_id: nodeId,
        instance_key: instanceKey,
        child_graph_id: 'test',
        request_kind: requestKind,
        category: 'available',
        recommendation_kind: 'required',
        reason_code: 'TEST_AVAILABLE',
        required_inputs: [],
        fact_references: [],
        blockers: [],
        valid_until: null,
        decision_fingerprint: decisionFingerprint,
    };
}

function positionWith(decisions) {
    return {
        project_id: 41,
        graph_version: 'agileforge.workflow.v1',
        fact_fingerprint: 'facts-41',
        evaluated_at: '2026-08-03T12:00:00Z',
        available_nodes: decisions.map((decision) => decision.node_id),
        waiting_nodes: [],
        blocked_nodes: [],
        invalid_nodes: [],
        terminal: false,
        decisions,
    };
}

function actionFor(decision, endpoint, transport = 'positioned') {
    return {
        node_id: decision.node_id,
        instance_key: decision.instance_key,
        decision_fingerprint: decision.decision_fingerprint,
        request_kind: decision.request_kind,
        endpoint,
        transport,
    };
}

async function flushPromises() {
    await new Promise((resolve) => setTimeout(resolve, 0));
}

function loadFunction(name) {
    const start = source.indexOf(`function ${name}(`);
    assert.notEqual(start, -1, `${name} should exist`);
    const bodyStart = source.indexOf('{', start);
    let depth = 0;
    for (let index = bodyStart; index < source.length; index += 1) {
        if (source[index] === '{') depth += 1;
        if (source[index] === '}') depth -= 1;
        if (depth === 0) {
            const body = source.slice(start, index + 1);
            return new Function(`${body}; return ${name};`)();
        }
    }
    assert.fail(`${name} should have a complete body`);
}

test('position view groups decisions by child graph and category', () => {
    const workflowPositionViewModel = loadFunction('workflowPositionViewModel');
    const view = workflowPositionViewModel(fixture);

    assert.deepEqual(view.childGraphIds, [
        'authority',
        'scope_extension',
        'vision',
        'backlog',
        'planning',
    ]);
    assert.deepEqual(view.available, [
        'authority.compile',
        'authority.repair',
        'scope_extension.start',
    ]);
    assert.deepEqual(view.waiting, ['vision.generate']);
    assert.deepEqual(view.blocked, ['backlog.generate']);
    assert.deepEqual(view.invalid, ['planning.roadmap.generate']);
});

test('action payload preserves exact decision and position guards', () => {
    const workflowActionPayload = loadFunction('workflowActionPayload');
    const decision = fixture.decisions[0];

    assert.deepEqual(
        workflowActionPayload(fixture, decision, 'run-41', 'dashboard-user'),
        {
            graph_version: 'agileforge.workflow.v1',
            expected_fact_fingerprint: 'facts-41',
            expected_decision_fingerprint: 'decision-compile',
            idempotency_key: 'run-41',
            changed_by: 'dashboard-user',
        },
    );
});

test('legacy phase arrays are absent from the frontend', () => {
    assert.doesNotMatch(source, /PHASE_STATES|PHASE_TERMINAL_STATES/);
    assert.doesNotMatch(source, /SETUP_REQUIRED|VISION_|BACKLOG_|SPRINT_COMPLETE/);
});

test('project HTML exposes only controls wired by the production script', () => {
    assert.match(html, /id="workflow-position-panel"/);
    assert.match(html, /id="workflow-action-dialog"/);
    assert.match(html, /src="\/dashboard\/project\.js"/);
    assert.doesNotMatch(html, /\bonclick=/);
    assert.doesNotMatch(html, /id="setup-panel"|id="authority-review-panel"/);
});

test('every available fixed-route decision renders one executable control', () => {
    const decisions = [
        availableDecision({
            nodeId: 'vision.review',
            decisionFingerprint: 'decision-vision',
            requestKind: 'decide_vision',
        }),
        availableDecision({
            nodeId: 'execution.task.complete',
            instanceKey: 'task:17',
            decisionFingerprint: 'decision-task-17',
            requestKind: 'complete_task',
        }),
        availableDecision({
            nodeId: 'planning.story.generate',
            instanceKey: 'requirement:req-a',
            decisionFingerprint: 'decision-story-a',
            requestKind: 'record_story_draft',
        }),
    ];
    const position = positionWith(decisions);
    const actions = [
        actionFor(decisions[0], 'vision/decide'),
        actionFor(decisions[1], 'sprint/task/complete'),
        actionFor(decisions[2], 'story/generate', 'agentic'),
    ];
    const { context, panel } = createFrontendHarness(async () => ({ ok: true }));

    context.renderWorkflowPosition(position, actions);

    assert.equal(panel.buttons.length, decisions.length);
    assert.equal(new Set(panel.buttons.map((button) => button.dataset.decisionKey)).size, decisions.length);
    assert.ok(panel.buttons.every((button) => button.listeners.has('click')));
});

test('clicking a repeated node instance submits that exact row guards', async () => {
    const decisions = [
        availableDecision({
            nodeId: 'planning.story.generate',
            instanceKey: 'requirement:req-a',
            decisionFingerprint: 'decision-story-a',
            requestKind: 'record_story_draft',
        }),
        availableDecision({
            nodeId: 'planning.story.generate',
            instanceKey: 'requirement:req-b',
            decisionFingerprint: 'decision-story-b',
            requestKind: 'record_story_draft',
        }),
    ];
    const position = positionWith(decisions);
    const actions = decisions.map((decision) => actionFor(
        decision,
        'story/generate',
        'agentic',
    ));
    const requests = [];
    const { context, panel } = createFrontendHarness(async (url, options = {}) => {
        requests.push({ url, options });
        if (options.method === 'POST') {
            return { ok: true, status: 200, json: async () => ({ status: 'success' }) };
        }
        return {
            ok: true,
            status: 200,
            json: async () => ({ status: 'success', data: position, actions }),
        };
    });
    context.renderWorkflowPosition(position, actions);

    panel.buttons[1].click();
    await flushPromises();

    const post = requests.find((request) => request.options.method === 'POST');
    assert.ok(post);
    const body = JSON.parse(post.options.body);
    assert.equal(body.instance_key, 'requirement:req-b');
    assert.equal(body.expected_decision_fingerprint, 'decision-story-b');
    assert.equal(body.expected_fact_fingerprint, 'facts-41');
});

test('structured conflict replaces stale controls with returned position', async () => {
    const oldDecision = availableDecision({
        nodeId: 'vision.generate',
        decisionFingerprint: 'decision-old',
        requestKind: 'record_vision_draft',
    });
    const newDecision = availableDecision({
        nodeId: 'backlog.generate',
        decisionFingerprint: 'decision-new',
        requestKind: 'record_backlog_draft',
    });
    const oldPosition = positionWith([oldDecision]);
    const newPosition = {
        ...positionWith([newDecision]),
        fact_fingerprint: 'facts-new',
    };
    const oldActions = [actionFor(oldDecision, 'vision/generate', 'agentic')];
    const newActions = [actionFor(newDecision, 'backlog/generate', 'agentic')];
    const { context, panel } = createFrontendHarness(async () => ({
        ok: false,
        status: 409,
        json: async () => ({
            detail: {
                ok: false,
                position: newPosition,
                actions: newActions,
            },
        }),
    }));
    context.renderWorkflowPosition(oldPosition, oldActions);

    panel.buttons[0].click();
    await flushPromises();

    assert.match(panel.innerHTML, /backlog\.generate/);
    assert.doesNotMatch(panel.innerHTML, /vision\.generate/);
    assert.equal(panel.buttons.length, 1);
});

test('retrying one rendered decision reuses its transport idempotency key', async () => {
    const decision = availableDecision({
        nodeId: 'vision.generate',
        decisionFingerprint: 'decision-vision',
        requestKind: 'record_vision_draft',
    });
    const position = positionWith([decision]);
    const actions = [actionFor(decision, 'vision/generate', 'agentic')];
    const requests = [];
    const { context, panel } = createFrontendHarness(async (url, options = {}) => {
        requests.push({ url, options });
        return {
            ok: false,
            status: 500,
            json: async () => ({ detail: { message: 'provider unavailable' } }),
        };
    });
    context.renderWorkflowPosition(position, actions);

    panel.buttons[0].click();
    await flushPromises();
    panel.buttons[0].click();
    await flushPromises();

    const keys = requests.map((request) => JSON.parse(request.options.body).idempotency_key);
    assert.equal(keys.length, 2);
    assert.equal(keys[0], keys[1]);
});

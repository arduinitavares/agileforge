import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { randomUUID, webcrypto } from 'node:crypto';
import test from 'node:test';

const sourcePath = path.resolve('frontend/project.js');
const source = fs.readFileSync(sourcePath, 'utf8');
const html = fs.readFileSync(path.resolve('frontend/project.html'), 'utf8');

function element(textContent = '') {
    const attributes = new Map();
    const listeners = {};
    return {
        textContent, disabled: false, dataset: {}, style: {},
        classList: { toggle() {}, add() {}, remove() {} },
        setAttribute(name, value) { attributes.set(name, value); },
        removeAttribute(name) { attributes.delete(name); },
        getAttribute(name) { return attributes.get(name) ?? null; },
        addEventListener(type, listener) { (listeners[type] ??= []).push(listener); },
        dispatch(type) { for (const listener of listeners[type] ?? []) listener({ currentTarget: this }); },
        querySelector() { return null; },
        closest(selector) { return selector === 'button' ? this : null; },
        focus() {},
        scrollIntoView() {},
    };
}

function harness(actions = []) {
    const requests = [];
    const documentListeners = {};
    const elements = {
        'cockpit-primary-action-btn': element(),
        'cockpit-primary-action-label': element('Execute Stage Action'),
        'cockpit-action-stage-chip': element('Available'),
        'cockpit-action-description': element(),
        'project-error': element(),
        'human-action-submit': element(),
        'human-action-error': element(),
        'human-action-dialog': element(),
    };
    const dialog = elements['human-action-dialog'];
    dialog.open = false;
    dialog.showModal = () => { dialog.open = true; };
    dialog.close = () => { dialog.open = false; dialog.dispatch('close'); };
    let directButton = null;
    const context = vm.createContext({
        console, crypto: { randomUUID }, URLSearchParams, AbortController,
        window: { addEventListener() {}, location: { href: '' }, setTimeout() {} },
        document: {
            addEventListener(type, listener) { (documentListeners[type] ??= []).push(listener); },
            getElementById(id) { return elements[id] ?? null; },
            querySelector(selector) {
                return directButton && selector === `button[data-direct-action="${directButton.dataset.directAction}"]`
                    ? directButton : null;
            },
            querySelectorAll() { return []; },
            createElement() {
                let text = '';
                return {
                    set textContent(value) { text = String(value ?? ''); },
                    get textContent() { return text; },
                    get innerHTML() { return text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'); },
                };
            },
        },
        fetch: (url, options) => new Promise((resolve, reject) => {
            requests.push({ url, options, resolve, reject });
        }),
    });
    vm.runInContext(source, context, { filename: sourcePath });
    vm.runInContext(`selectedProjectId = 7; lifecycleState.actions = ${JSON.stringify(actions)};`, context);
    context.installInteractions();
    context.renderTopCockpit();
    return {
        context, elements, requests, dialog,
        cockpit: elements['cockpit-primary-action-btn'],
        state(expression) { return vm.runInContext(expression, context); },
        submit(form) { return Promise.all(documentListeners.submit.map((listener) => listener({ target: form, preventDefault() {} }))); },
        direct(action) {
            const button = element();
            button.dataset = {
                directAction: action.request_kind,
                deliveryActionNode: action.node_id,
                deliveryActionEndpoint: action.endpoint,
                ...(action.instance_key ? { deliveryActionInstance: action.instance_key } : {}),
            };
            button.click = () => {
                if (!button.disabled) button.completion = Promise.all(documentListeners.click.map((listener) => listener({ target: button })));
            };
            directButton = button;
            return button;
        },
    };
}

const bootstrap = { request_kind: 'generate_vision_bootstrap', endpoint: 'vision/bootstrap', availability: 'available' };
const failed = { ok: false, status: 500, text: async () => JSON.stringify({ message: 'Controlled test stop.' }) };
const nextTurn = () => new Promise((resolve) => setImmediate(resolve));

function completeDashboardGets(requests, actions, fail = false) {
    assert.equal(requests.length, 14);
    for (const request of requests) {
        if (fail && request.url === '/api/projects/7') {
            request.reject(new Error('Controlled recovery GET failure.'));
        } else if (request.url.endsWith('/sprint/status')) {
            request.resolve({ ok: false, status: 404, text: async () => JSON.stringify({ code: 'SPRINT_NOT_FOUND' }) });
        } else {
            const data = request.url === '/api/projects/7' ? { id: 7, name: 'Initialized dashboard' } : {};
            request.resolve({ ok: true, text: async () => JSON.stringify({ data, ...(request.url.endsWith('/position') ? { actions } : {}) }) });
        }
    }
}

async function successfulDashboardLoad(h, actions) {
    const start = h.requests.length;
    const pending = h.context.loadDashboard();
    completeDashboardGets(h.requests.slice(start), actions);
    assert.equal(await pending, true);
}

test('cancelling the Story confirmation leaves cockpit unlocked', async () => {
    const action = { request_kind: 'record_story_draft', node_id: 'story.generate', instance_key: 'backlog_item:PBI-1', endpoint: 'story/draft' };
    const h = harness([action]);
    h.state("lifecycleState.storyPending = { items: [{ backlog_item_id: 'PBI-1', requirement: 'Review fixture requirement' }] };");
    const button = h.direct(action);
    h.context.handlePrimaryCockpitAction({ request_kind: action.request_kind });
    await button.completion;
    assert.equal(h.dialog.open, true);
    assert.equal(h.requests.length, 0);

    h.context.closeHumanDialog();
    h.context.renderTopCockpit();

    assert.equal(h.dialog.open, false);
    assert.equal(h.cockpit.disabled, false);
    assert.equal(h.cockpit.getAttribute('aria-busy'), null);
    assert.equal(h.state('activeCockpitAction'), null);
});

test('rejected pre-dispatch binding leaves cockpit unlocked', async () => {
    const action = { request_kind: 'record_story_draft', node_id: 'story.generate', endpoint: 'story/draft' };
    const h = harness([action]);
    const button = h.direct({ ...action, endpoint: 'stale/story/draft' });
    h.context.handlePrimaryCockpitAction({ request_kind: action.request_kind });
    await button.completion;

    assert.equal(h.requests.length, 0);
    assert.match(h.elements['project-error'].textContent, /changed/);
    assert.equal(h.cockpit.disabled, false);
    assert.equal(h.cockpit.getAttribute('aria-busy'), null);
    assert.equal(h.state('activeCockpitAction'), null);
});

test('replacement workbench control cannot submit duplicate request while action is in flight', async () => {
    const h = harness([bootstrap]);
    const firstButton = h.direct(bootstrap);
    const first = h.context.runDirectAction(bootstrap.request_kind, firstButton);
    const firstToken = h.state('activeCockpitAction.token');

    // A replacement button appears after a view re-render
    const replacementButton = h.direct(bootstrap);
    const second = h.context.runDirectAction(bootstrap.request_kind, replacementButton);

    assert.equal(h.requests.length, 1);
    assert.equal(h.state('activeCockpitAction.token'), firstToken);

    h.requests[0].resolve(failed);
    await first;
    await second;

    assert.equal(h.state('activeCockpitAction'), null);
    assert.equal(h.cockpit.disabled, false);
    assert.equal(h.cockpit.getAttribute('aria-busy'), null);
});

test('Vision interview submits and marks cockpit busy until completion', async () => {
    const action = { request_kind: 'record_vision_interview_turn', endpoint: 'vision/respond' };
    const h = harness([action]);
    h.elements['vision-response'] = { value: 'A concrete interview response.', disabled: false };
    const submit = element();
    const form = { dataset: { interviewScope: 'vision' }, querySelector() { return submit; } };

    const pending = h.submit(form);

    assert.equal(h.requests.length, 1);
    assert.equal(submit.disabled, true);
    assert.equal(h.cockpit.disabled, true);
    assert.equal(h.cockpit.getAttribute('aria-busy'), 'true');
    assert.equal(h.elements['cockpit-action-stage-chip'].textContent, 'Executing');

    h.requests[0].resolve(failed);
    await pending;

    assert.equal(h.cockpit.disabled, false);
    assert.equal(h.cockpit.getAttribute('aria-busy'), null);
    assert.equal(h.elements['cockpit-action-stage-chip'].textContent, 'Available');
});

test('real HTML field IDs retain correct data after renderTopCockpit', () => {
    const h = harness([bootstrap]);
    const expectedIds = {
        'cockpit-goal-statement': 'Loading Product Goal...',
        'cockpit-goal-status': 'Active',
        'cockpit-vision-anchor': 'Vision: Loading...',
        'cockpit-cycle-progress': 'Cycle 1',
        'cockpit-active-stage-label': 'Sprint Planning',
        'cockpit-progress-bar': '',
    };
    for (const [id, initial] of Object.entries(expectedIds)) {
        assert.ok(html.includes(`id="${id}"`));
        h.elements[id] = element(initial);
    }
    h.elements['cockpit-progress-bar'].style.width = '70%';
    h.state("lifecycleState.goal = { candidate: { statement: 'A real current goal.' } }; lifecycleState.vision = { accepted: { statement: 'An accepted vision.' } }; lifecycleState.sprintHistory = { items: [{}, {}] }; lifecycleState.position = { decisions: [{ node_id: 'vision.bootstrap', request_kind: 'generate_vision_bootstrap' }] };");

    h.context.renderTopCockpit();

    assert.equal(h.elements['cockpit-goal-statement'].textContent, 'A real current goal.');
    assert.equal(h.elements['cockpit-goal-status'].textContent, 'Review');
    assert.equal(h.elements['cockpit-vision-anchor'].textContent, 'Vision: An accepted vision.');
    assert.equal(h.elements['cockpit-cycle-progress'].textContent, 'Cycle 3');
});

test('failed post-mutation refresh keeps cockpit locked rather than re-enabling an ineffective control', async () => {
    const action = { request_kind: 'record_roadmap_draft', node_id: 'roadmap.generate', endpoint: 'roadmap/draft' };
    const h = harness([action]);
    h.context.loadDashboard = async () => { throw new Error('Controlled refresh failure.'); };
    const button = h.direct(action);

    const pending = h.context.runDirectAction(action.request_kind, button);
    h.requests[0].resolve({ ok: true, text: async () => '{}' });
    await pending;

    // Both workbench and cockpit controls remain locked
    assert.equal(button.disabled, true);
    assert.equal(h.cockpit.disabled, true);
    assert.equal(h.cockpit.getAttribute('aria-busy'), null);
    assert.equal(h.elements['cockpit-action-stage-chip'].textContent, 'Locked');
    assert.equal(h.elements['cockpit-primary-action-label'].textContent, 'Action Unavailable');

    // Clicks on the cockpit primary action are ignored
    h.context.handlePrimaryCockpitAction({ request_kind: action.request_kind });
    assert.equal(h.requests.length, 1);
});

test('human confirmation is rejected while direct action is in flight', async () => {
    const h = harness([bootstrap]);
    const visionButton = h.direct(bootstrap);
    const visionPending = h.context.runDirectAction(bootstrap.request_kind, visionButton);
    const visionToken = h.state('activeCockpitAction.token');
    h.elements['human-action-path'] = { value: 'C:/review-fixture/repository' };
    h.context.openHumanDialog({ kind: 'repository', field: 'path', required: false });
    await h.submit({ id: 'human-action-form' });

    // Human submission is rejected by the guard; no second POST is sent
    assert.equal(h.requests.length, 1);
    assert.equal(h.requests[0].url, '/api/projects/7/vision/bootstrap');
    assert.equal(h.state('activeCockpitAction.token'), visionToken);

    h.requests[0].resolve(failed);
    await visionPending;

    assert.equal(h.state('activeCockpitAction'), null);
    assert.equal(h.cockpit.disabled, false);
});

test('successful Vision generation followed by failed refresh enters reconciliation lock', async () => {
    const h = harness([bootstrap]);
    h.context.loadDashboard = async () => { throw new Error('Controlled refresh failure.'); };
    const button = h.direct(bootstrap);
    const pending = h.context.runDirectAction(bootstrap.request_kind, button);
    h.requests[0].resolve({ ok: true, text: async () => '{}' });
    await pending;

    assert.equal(h.state('activeDeliveryUnreconciled'), true);
    assert.equal(h.cockpit.disabled, true);
    assert.equal(button.disabled, true);
    assert.equal(h.elements['cockpit-action-stage-chip'].textContent, 'Locked');
    assert.equal(h.elements['cockpit-primary-action-label'].textContent, 'Action Unavailable');

    const retry = h.context.runDirectAction(bootstrap.request_kind, button);
    assert.equal(await retry, false);
    assert.equal(h.requests.length, 1);
});

test('successful Sprint start with failed refresh keeps both workbench and cockpit Locked', async () => {
    const action = { request_kind: 'start_sprint', endpoint: 'sprint/start', node_id: 'sprint.start' };
    const h = harness([action]);
    h.context.loadDashboard = async () => { throw new Error('Controlled refresh failure.'); };
    const button = h.direct(action);
    const pending = h.context.runSprintStart({ action, decisionFingerprint: 'fixture-decision' }, button);
    const handled = pending.catch((error) => error);
    h.requests[0].resolve({ ok: true, text: async () => '{}' });
    assert.match((await handled).message, /Controlled refresh failure/);

    assert.equal(h.state('activeSprintMutation.phase'), 'awaiting_authority');
    assert.equal(h.state('activeDeliveryUnreconciled'), true);
    assert.equal(h.cockpit.disabled, true);
    assert.equal(button.disabled, true);
    assert.equal(h.elements['cockpit-action-stage-chip'].textContent, 'Locked');
    assert.equal(h.elements['cockpit-primary-action-label'].textContent, 'Action Unavailable');

    h.context.handlePrimaryCockpitAction({ request_kind: action.request_kind });
    assert.equal(h.requests.length, 1);
});

test('unowned or mismatched token release cannot clear an active lock', () => {
    const h = harness([bootstrap]);
    const token1 = h.context.setCockpitActionBusy(true, 'first_action', { token: 'token-1' });
    assert.equal(token1, 'token-1');
    assert.equal(h.state('activeCockpitAction.token'), 'token-1');

    // Unowned release without token does nothing
    h.context.setCockpitActionBusy(false);
    assert.equal(h.state('activeCockpitAction.token'), 'token-1');

    // Release with wrong token does nothing
    h.context.setCockpitActionBusy(false, 'other_action', { token: 'token-wrong' });
    assert.equal(h.state('activeCockpitAction.token'), 'token-1');

    // Matching token release succeeds
    h.context.setCockpitActionBusy(false, 'first_action', { token: 'token-1' });
    assert.equal(h.state('activeCockpitAction'), null);
});

test('Story selection is blocked while failed Sprint-plan refresh keeps cockpit Locked', async () => {
    const action = { request_kind: 'record_sprint_plan', node_id: 'planning.sprint.plan', endpoint: 'sprint/generate' };
    const h = harness([action]);
    const selectedScopeFingerprint = `sha256:${'a'.repeat(64)}`;
    const candidate = {
        story_id: 101, source_story_item_id: 'US-001', is_superseded: false,
        structurally_eligible: true, structural_eligibility_status: 'eligible',
        sprint_selection_state: 'selected', sprint_selection_state_fingerprint: `sha256:${'f'.repeat(64)}`,
        selected_scope_fingerprint: selectedScopeFingerprint,
        dependency_safe: true, sprint_candidate: true,
        validation_status: 'validated', validation_failures: [],
    };
    const stateFields = {
        storyDependencies: { stories: [candidate], edges: [], selected_story_ids: [101], selected_scope_fingerprint: selectedScopeFingerprint },
        sprintCandidates: {
            items: [candidate],
            capacity: { status: 'recommended', recommended_max_story_points: 8, source: 'project_metrics', rationale: 'Review fixture capacity.' },
        },
    };
    h.state(`Object.assign(lifecycleState, ${JSON.stringify(stateFields)});`);
    h.context.loadDashboard = async () => { throw new Error('Controlled refresh failure.'); };
    const pendingPlan = h.context.runDirectAction(action.request_kind, h.direct(action), null, { max_story_points: 8 });
    assert.equal(h.requests.length, 1);
    h.requests[0].resolve({ ok: true, text: async () => '{}' });
    await pendingPlan;
    assert.equal(h.state('activeDeliveryUnreconciled'), true);
    assert.equal(h.cockpit.disabled, true);
    assert.equal(h.elements['cockpit-action-stage-chip'].textContent, 'Locked');

    const selectionButton = h.direct({});
    selectionButton.dataset = { storySelectionId: '101', storySelectionIntent: 'remove', storySelectionFingerprint: candidate.sprint_selection_state_fingerprint };
    h.context.document.querySelectorAll = (selector) => selector.includes('[data-story-selection-intent]') ? [selectionButton] : [];
    selectionButton.click();

    // Guard prevents sending the second request
    assert.equal(h.requests.length, 1);
    assert.equal(h.state('activeDeliveryUnreconciled'), true);
});

test('accepted interview and failed refresh leave local controls disabled', async () => {
    const action = { request_kind: 'record_vision_interview_turn', endpoint: 'vision/respond' };
    const h = harness([action]);
    await successfulDashboardLoad(h, [action]);
    const textarea = { value: 'Confirmed interview response.', disabled: false };
    h.elements['vision-response'] = textarea;
    h.elements['vision-response-status'] = element();
    const submit = element();
    const form = { dataset: { interviewScope: 'vision' }, querySelector() { return submit; } };
    const postIndex = h.requests.length;
    const pending = h.submit(form);
    h.requests[postIndex].resolve({ ok: true, text: async () => '{}' });
    await nextTurn();
    completeDashboardGets(h.requests.slice(postIndex + 1), [action], true);
    await pending;
    assert.equal(h.state('activeDeliveryUnreconciled'), true);
    assert.equal(h.cockpit.disabled, true);
    assert.equal(h.elements['cockpit-action-stage-chip'].textContent, 'Locked');
    assert.equal(submit.disabled, true);
    assert.equal(textarea.disabled, true);
    await h.submit(form);
    assert.equal(h.requests.length, postIndex + 15);
});

test('initialized Vision action cannot be dispatched again after failed reload', async () => {
    const h = harness([bootstrap]);
    await successfulDashboardLoad(h, [bootstrap]);
    const button = h.direct(bootstrap);
    const postIndex = h.requests.length;
    const pending = h.context.runDirectAction(bootstrap.request_kind, button);
    h.requests[postIndex].resolve({ ok: true, text: async () => '{}' });
    await nextTurn();
    completeDashboardGets(h.requests.slice(postIndex + 1), [bootstrap], true);
    await pending;
    assert.equal(h.state('activeDeliveryUnreconciled'), true);
    assert.equal(button.disabled, true);
    assert.equal(h.cockpit.disabled, true);
    assert.equal(h.elements['cockpit-action-stage-chip'].textContent, 'Locked');

    const repeatIndex = h.requests.length;
    h.context.handlePrimaryCockpitAction({ request_kind: bootstrap.request_kind });
    assert.equal(h.requests.length, repeatIndex);
});

test('failed delivery POST and failed recovery GET leave cockpit Locked', async () => {
    const action = { request_kind: 'record_roadmap_draft', node_id: 'roadmap.generate', endpoint: 'roadmap/draft' };
    const h = harness([action]);
    await successfulDashboardLoad(h, [action]);
    const button = h.direct(action);
    const postIndex = h.requests.length;
    const pending = h.context.runDirectAction(action.request_kind, button);
    h.requests[postIndex].resolve({ ...failed, status: 409 });
    await nextTurn();
    completeDashboardGets(h.requests.slice(postIndex + 1), [action], true);
    await pending;
    assert.equal(button.disabled, true);
    assert.equal(h.state('activeDeliveryUnreconciled'), true);
    assert.equal(h.cockpit.disabled, true);
    assert.equal(h.elements['cockpit-action-stage-chip'].textContent, 'Locked');
    assert.equal(h.elements['cockpit-primary-action-label'].textContent, 'Action Unavailable');
    h.context.handlePrimaryCockpitAction({ request_kind: action.request_kind });
    assert.equal(h.requests.length, postIndex + 15);
});

test('pre-POST refresh cannot reconcile the mutation', async () => {
    const action = { request_kind: 'record_vision_interview_turn', endpoint: 'vision/respond' };
    const h = harness([action]);
    await successfulDashboardLoad(h, [action]);
    const textarea = { value: 'Confirmed interview response.', disabled: false };
    h.elements['vision-response'] = textarea;
    h.elements['vision-response-status'] = element();
    const submit = element();
    const form = { dataset: { interviewScope: 'vision' }, querySelector() { return submit; } };
    const postIndex = h.requests.length;
    const pending = h.submit(form);
    assert.equal(h.requests[postIndex].options.method, 'POST');
    await successfulDashboardLoad(h, [action]);
    assert.equal(h.state('lastSuccessfulDashboardLoadSequence'), 2);
    assert.ok(h.state('activeCockpitAction'));
    const recoveryStart = h.requests.length;
    h.requests[postIndex].resolve({ ok: true, text: async () => '{}' });
    await nextTurn();
    completeDashboardGets(h.requests.slice(recoveryStart), [action], true);
    await pending;
    assert.equal(h.state('activeDeliveryUnreconciled'), true);
    assert.equal(h.cockpit.disabled, true);
    assert.equal(h.elements['cockpit-action-stage-chip'].textContent, 'Locked');
    assert.equal(submit.disabled, true);
    assert.equal(textarea.disabled, true);
});

test('superseded refresh does not relock cockpit after a newer refresh has already succeeded', async () => {
    const action = { request_kind: 'refresh_repository_binding', endpoint: 'repository/refresh' };
    const h = harness([action]);
    const teamLabel = 'Review Team';
    const encodedTeam = new TextEncoder().encode(teamLabel);
    const actualDigest = await webcrypto.subtle.digest('SHA-256', encodedTeam);
    const hex = Buffer.from(actualDigest).toString('hex');
    const owner = { kind: 'named_team', key: `agileforge:sprint-owner:named-team:v1:sha256:${hex}`, label: teamLabel, display_label: teamLabel };
    let announceFirstDigest;
    const firstDigestStarted = new Promise((resolve) => { announceFirstDigest = resolve; });
    let releaseFirstDigest;
    const delayedDigest = new Promise((resolve) => { releaseFirstDigest = resolve; });
    let digestCalls = 0;
    h.context.TextEncoder = TextEncoder;
    h.context.crypto.subtle = {
        digest(algorithm, input) {
            digestCalls += 1;
            if (digestCalls === 1) {
                announceFirstDigest();
                return delayedDigest;
            }
            return webcrypto.subtle.digest(algorithm, input);
        },
    };
    function finishGets(requests, marker) {
        assert.equal(requests.length, 14);
        for (const request of requests) {
            if (request.url.endsWith('/sprint/status')) {
                request.resolve({ ok: false, status: 404, text: async () => JSON.stringify({ code: 'SPRINT_NOT_FOUND' }) });
                continue;
            }
            let data = {};
            if (request.url === '/api/projects/7') data = { id: 7, name: marker };
            if (request.url.endsWith('/sprint/candidates')) data = { project_id: 7, items: [], sprint_owner: owner };
            request.resolve({ ok: true, text: async () => JSON.stringify({ data, ...(request.url.endsWith('/position') ? { actions: [action] } : {}) }) });
        }
    }

    const pendingMutation = h.context.runDirectAction(action.request_kind, h.direct(action));
    h.requests[0].resolve({ ok: true, text: async () => '{}' });
    await new Promise((resolve) => setImmediate(resolve));
    finishGets(h.requests.slice(1), 'first projection');
    await firstDigestStarted;
    assert.equal(h.state('dashboardLoadSequence'), 1);

    const newerRefresh = h.context.loadDashboard();
    finishGets(h.requests.slice(15), 'newer successful projection');
    assert.equal(await newerRefresh, true);
    assert.equal(h.state('dashboardLoadSequence'), 2);
    assert.equal(h.state('lifecycleState.project.name'), 'newer successful projection');
    assert.equal(h.state('activeDeliveryUnreconciled'), false);
    assert.ok(h.state('activeCockpitAction'));

    // WebCrypto validation is asynchronous and is not cancelled by fetch's AbortController.
    releaseFirstDigest(actualDigest);
    await pendingMutation;
    assert.equal(h.state('lifecycleState.project.name'), 'newer successful projection');
    assert.equal(h.state('activeDeliveryUnreconciled'), false);
    assert.equal(h.state('activeCockpitAction'), null);
    assert.equal(h.cockpit.disabled, false);
    assert.equal(h.elements['cockpit-action-stage-chip'].textContent, 'Available');
});

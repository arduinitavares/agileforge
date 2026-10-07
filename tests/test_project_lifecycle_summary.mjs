// tests/test_project_lifecycle_summary.mjs
import assert from 'node:assert/strict';
import { randomUUID, webcrypto } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import { acceptedDashboardBundle, acceptedLifecycleState, activeSprintLifecycleState, sourceOnlyDashboardBundle, sourceOnlyLifecycleState } from './dashboard_bundle_fixture.mjs';

const projectSourcePath = path.resolve('frontend/project.js');
const projectSource = fs.readFileSync(projectSourcePath, 'utf8');
const projectMarkup = fs.readFileSync(path.resolve('frontend/project.html'), 'utf8');
const workspaceSourcePath = path.resolve('frontend/lifecycle-workspace.js');
const workspaceSource = fs.readFileSync(workspaceSourcePath, 'utf8');
const escapeText = (value) => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const decodeText = (value) => value.replaceAll('&quot;', '"').replaceAll('&#39;', "'").replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&');

// Parse the markup assigned by the real mount function into a small DOM tree.
// Network and browser surfaces are doubles; workspace rendering and events are real.
function lifecycleElement(tagName = 'div') {
    const attributes = new Map();
    const listeners = new Map();
    let ownText = '';
    let markup = '';
    const node = {
        tagName: tagName.toUpperCase(), children: [], parentElement: null, dataset: {}, style: {}, hidden: false, disabled: false, scrollTop: 0,
        setAttribute(name, value) {
            attributes.set(name, String(value));
            if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = String(value);
            if (name === 'hidden') this.hidden = true;
        },
        getAttribute(name) { return attributes.get(name) ?? null; },
        removeAttribute(name) { attributes.delete(name); },
        addEventListener(type, listener) { const group = listeners.get(type) ?? []; group.push(listener); listeners.set(type, group); },
        dispatch(type, event = {}) { return Promise.all((listeners.get(type) ?? []).map((listener) => listener({ currentTarget: this, target: this, preventDefault() {}, ...event }))); },
        click() { return this.dispatch('click'); },
        focus() {}, scrollIntoView() {},
        contains(other) { return this === other || this.children.some((child) => child.contains(other)); },
        matches(selector) {
            if (selector.startsWith('#')) return this.getAttribute('id') === selector.slice(1);
            if (selector.startsWith('.')) return (this.getAttribute('class') ?? '').split(/\s+/).includes(selector.slice(1));
            const attribute = /^\[([^=\]]+)(?:="([^"]*)")?\]$/.exec(selector);
            if (attribute) return attribute[2] === undefined ? attributes.has(attribute[1]) : this.getAttribute(attribute[1]) === attribute[2];
            return this.tagName.toLowerCase() === selector;
        },
        querySelectorAll(selector) {
            const selectors = selector.split(',').map((item) => item.trim());
            const found = [];
            const visit = (parent) => parent.children.forEach((child) => { if (selectors.some((item) => child.matches(item))) found.push(child); visit(child); });
            visit(this);
            return found;
        },
        querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; },
        closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) ?? null; },
        get textContent() { return ownText + this.children.map((child) => child.textContent).join(''); },
        set textContent(value) { ownText = String(value ?? ''); this.children = []; markup = escapeText(ownText); },
        get innerHTML() { return markup; },
        set innerHTML(value) {
            markup = String(value); ownText = ''; this.children = [];
            const stack = [this];
            for (const token of markup.match(/<[^>]*>|[^<]+/g) ?? []) {
                if (token.startsWith('</')) { if (stack.length > 1) stack.pop(); continue; }
                if (token.startsWith('<!')) continue;
                if (!token.startsWith('<')) { const text = lifecycleElement('text'); text.textContent = decodeText(token); text.parentElement = stack.at(-1); stack.at(-1).children.push(text); continue; }
                const start = /^<([\w-]+)([^>]*)>/.exec(token);
                if (!start) continue;
                const child = lifecycleElement(start[1]); child.parentElement = stack.at(-1);
                for (const match of start[2].matchAll(/([^\s=/>]+)(?:="([^"]*)"|='([^']*)'|=([^\s>]+))?/g)) child.setAttribute(match[1], decodeText(match[2] ?? match[3] ?? match[4] ?? ''));
                stack.at(-1).children.push(child);
                if (!['input', 'br', 'hr', 'img', 'meta', 'link'].includes(start[1]) && !token.endsWith('/>')) stack.push(child);
            }
        },
    };
    node.classList = {
        add(...names) { node.setAttribute('class', [...new Set([...(node.getAttribute('class') ?? '').split(/\s+/).filter(Boolean), ...names])].join(' ')); },
        remove(...names) { node.setAttribute('class', (node.getAttribute('class') ?? '').split(/\s+/).filter((name) => !names.includes(name)).join(' ')); },
        toggle(name, force) { const has = (node.getAttribute('class') ?? '').split(/\s+/).includes(name); const next = force ?? !has; if (next) this.add(name); else this.remove(name); return next; },
    };
    return node;
}

function projectLifecycleHarness({ workspace = true, bundle = sourceOnlyDashboardBundle(), fetchImpl = null, template = false, seededView = null, dateImpl = Date } = {}) {
    const ids = ['workspace-map-host', 'stage-workbench', 'workbench-stage-title', 'workbench-stage-kicker', 'cockpit-active-stage-label', 'cockpit-vision-anchor', 'cockpit-goal-status', 'dashboard-refresh-time', 'project-error', 'vision-panel', 'goal-panel', 'specification-panel', 'repository-panel', 'delivery-panel'];
    const requests = [];
    const root = lifecycleElement('body');
    if (template) root.innerHTML = projectMarkup;
    const elements = template
        ? Object.fromEntries(root.querySelectorAll('[id]').map((node) => [node.getAttribute('id'), node]))
        : Object.fromEntries(ids.map((id) => { const node = lifecycleElement(); node.setAttribute('id', id); return [id, node]; }));
    if (!template) root.children = Object.values(elements);
    const document = {
        body: root, activeElement: null, title: '',
        createElement: lifecycleElement,
        getElementById(id) { return elements[id] ?? root.querySelector(`#${id}`); },
        querySelector(selector) { return root.querySelector(selector); },
        querySelectorAll(selector) { return root.querySelectorAll(selector); },
        addEventListener() {},
    };
    const windowListeners = new Map();
    const historyEntries = [seededView ? { agileForgeWorkspace: { ...seededView } } : null];
    let historyIndex = 0;
    const dispatchWindow = async (type, event = {}) => {
        await Promise.all((windowListeners.get(type) ?? []).map((listener) => listener(event)));
    };
    const history = {
        get state() { return historyEntries[historyIndex]; },
        pushState(state) { historyEntries.splice(historyIndex + 1); historyEntries.push(structuredClone(state)); historyIndex += 1; },
        replaceState(state) { historyEntries[historyIndex] = structuredClone(state); },
        async back() { if (historyIndex > 0) { historyIndex -= 1; await dispatchWindow('popstate', { state: this.state }); } },
        async forward() { if (historyIndex + 1 < historyEntries.length) { historyIndex += 1; await dispatchWindow('popstate', { state: this.state }); } },
    };
    const context = vm.createContext({
        AbortController, URLSearchParams, TextEncoder, console, Date: dateImpl,
        crypto: { randomUUID, subtle: webcrypto.subtle }, document,
        window: {
            addEventListener(type, listener) { const group = windowListeners.get(type) ?? []; group.push(listener); windowListeners.set(type, group); },
            location: { href: '/project.html?id=7', search: '?id=7' }, history, setTimeout() {},
        },
        fetch: async (url, options = {}) => {
            requests.push({ url, options });
            if (fetchImpl) return fetchImpl(url, options);
            assert.equal(url, '/api/projects/7/dashboard', 'Unexpected network operation in lifecycle fixture');
            return { ok: true, status: 200, text: async () => JSON.stringify(bundle) };
        },
    });
    if (workspace) vm.runInContext(workspaceSource, context, { filename: workspaceSourcePath });
    vm.runInContext(projectSource, context, { filename: projectSourcePath });
    vm.runInContext('selectedProjectId = 7;', context);
    return {
        context, elements, requests, history,
        start() { return dispatchWindow('DOMContentLoaded'); },
        state(expression) { return vm.runInContext(expression, context); },
        setState(state, readKind = 'ready') {
            context.fixtureState = state;
            vm.runInContext(`lifecycleState = fixtureState; lifecycleDisplayRead = { kind: ${JSON.stringify(readKind)} };`, context);
        },
        workspace() { return vm.runInContext('AgileForgeWorkspace', context); },
        display(state, kind = 'ready') { return context.projectLifecycleDisplayProjection(state, { kind }); },
    };
}

function dashboardResponse(bundle, status = 200) {
    return { ok: status < 300, status, text: async () => JSON.stringify(bundle) };
}

function completedSprintPendingPlanBundle() {
    const bundle = acceptedDashboardBundle();
    const sprint = activeSprintLifecycleState().sprintStatus.data;
    sprint.sprint.status = 'completed';
    sprint.effective_status = 'completed';
    sprint.accepted_plan.status = 'completed';
    sprint.tasks[0].status = 'Done';
    sprint.story_completions = [{ story_id: 81, sprint_id: 71 }];
    sprint.original_triage = [{ triage_id: 101 }];
    bundle.data.sprintStatusResponse = { status: 200, body: { data: sprint } };
    bundle.data.sprintPlanReview.body.data = {
        review: { project_id: 7, review: { state: 'pending' } },
    };
    bundle.data.position.body.data.decisions = [{
        node_id: 'sprint.plan.review', request_kind: 'decide_sprint_plan',
        category: 'waiting', recommendation_kind: 'required',
    }];
    return bundle;
}

const lifecycleBadgeIds = ['nav-vision-badge', 'nav-goal-badge', 'nav-specification-badge', 'nav-backlog-badge', 'nav-roadmap-badge', 'nav-sprint-badge'];

function renderedLifecycleLabels(h) {
    const ids = ['cockpit-active-stage-label', 'cockpit-vision-anchor', 'cockpit-goal-status', ...lifecycleBadgeIds];
    return Object.fromEntries(ids.map((id) => [id, { label: h.elements[id].textContent, title: h.elements[id].getAttribute('title') }]));
}

test('source-only state uses Specification and accepted current Vision', () => {
    const h = projectLifecycleHarness();
    const display = h.display(sourceOnlyLifecycleState());
    assert.equal(display.phaseLabel, 'Specification');
    assert.equal(display.primaryStageId, 4);
    assert.deepEqual(Array.from(display.currentStageIds), [4]);
    assert.equal(display.visionLabel, 'Vision: Accepted · Accepted direction');
    assert.equal(display.badges.Vision.label, 'Complete');
    assert.equal(display.badges['Product Goal'].label, 'Active');
    assert.equal(display.badges.Specification.label, 'Source registered');
    assert.equal(display.badges.Specification.detail, 'Awaiting structuring');
    assert.equal(display.badges.Backlog.label, 'Blocked');
    assert.equal(display.badges.Backlog.detail, 'SPECIFICATION_NOT_APPROVED');
    assert.equal(display.badges.Roadmap.label, 'Blocked');
    assert.equal(display.badges.Roadmap.detail, 'BACKLOG_NOT_ACCEPTED');
    assert.equal(display.badges.Sprint.label, 'Not started');
});

test('framing badges use source and accepted artifact projections', () => {
    const h = projectLifecycleHarness({ template: true });
    h.setState(sourceOnlyLifecycleState()); h.context.renderMasterStageNav();
    const elements = h.elements;
    assert.equal(elements['nav-vision-badge']?.textContent, 'Complete');
    assert.equal(elements['nav-vision-badge'].getAttribute('title'), '');
    assert.equal(elements['nav-goal-badge']?.textContent, 'Active');
    assert.equal(elements['nav-goal-badge'].getAttribute('title'), '');
    assert.equal(elements['nav-specification-badge']?.textContent, 'Source registered');
    assert.equal(elements['nav-specification-badge'].getAttribute('title'), 'Awaiting structuring');
    assert.equal(elements['nav-backlog-badge']?.textContent, 'Blocked');
    assert.equal(elements['nav-backlog-badge'].getAttribute('title'), 'SPECIFICATION_NOT_APPROVED');
    assert.equal(elements['nav-roadmap-badge']?.textContent, 'Blocked');
    assert.equal(elements['nav-roadmap-badge'].getAttribute('title'), 'BACKLOG_NOT_ACCEPTED');
    assert.equal(elements['nav-sprint-badge'].textContent, 'Not started');
    assert.equal(elements['nav-sprint-badge'].getAttribute('title'), '');

    h.setState(acceptedLifecycleState()); h.context.renderMasterStageNav();
    assert.equal(elements['nav-specification-badge'].textContent, 'Accepted');
    assert.equal(elements['nav-specification-badge'].getAttribute('title'), 'Review: Accepted · Exact candidate accepted.');
    assert.equal(elements['nav-backlog-badge'].textContent, 'Accepted');
    assert.equal(elements['nav-backlog-badge'].getAttribute('title'), '');
    assert.equal(elements['nav-roadmap-badge'].textContent, 'Accepted');
    assert.equal(elements['nav-roadmap-badge'].getAttribute('title'), '');
});

test('framing badge titles preserve accepted bases and pending successor context', () => {
    const h = projectLifecycleHarness({ template: true });
    const state = acceptedLifecycleState();
    state.vision.candidate = { statement: 'Revised direction', review_fingerprint: 'sha256:vision-revision' };
    state.vision.review = { state: 'pending', rationale: 'Review the Vision revision.' };
    state.goal.candidate = { statement: 'Revised outcome', fingerprint: 'sha256:goal-revision' };
    state.goal.review = { state: 'feedback', rationale: 'Clarify the Goal revision.' };
    state.planningReviews.backlog = { review: {
        phase: 'backlog', project_id: 7,
        candidate: { backlog_artifact_id: 52, artifact_fingerprint: 'sha256:backlog-52', supersedes_backlog_artifact_id: 51, backlog_items: [], is_complete: true, clarifying_questions: [] },
        review: { state: 'pending', rationale: 'Review the Backlog successor.' },
    } };
    state.planningReviews.roadmap = { continuation: { review: {
        phase: 'roadmap', project_id: 7,
        candidate: { roadmap_artifact_id: 62, artifact_fingerprint: 'sha256:roadmap-62', supersedes_roadmap_artifact_id: 61, roadmap_releases: [], is_complete: true, clarifying_questions: [] },
        review: { state: 'feedback', rationale: 'Clarify the Roadmap successor.' },
    } } };
    h.setState(state); h.context.renderMasterStageNav();
    assert.equal(h.elements['nav-vision-badge']?.textContent, 'Complete');
    assert.equal(h.elements['nav-vision-badge'].getAttribute('title'), 'Revision: Pending · Review the Vision revision.');
    assert.equal(h.elements['nav-goal-badge'].textContent, 'Active');
    assert.equal(h.elements['nav-goal-badge'].getAttribute('title'), 'Revision: Feedback · Clarify the Goal revision.');
    assert.equal(h.elements['nav-specification-badge'].textContent, 'Accepted');
    assert.equal(h.elements['nav-specification-badge'].getAttribute('title'), 'Review: Accepted · Exact candidate accepted.');
    assert.equal(h.elements['nav-backlog-badge'].textContent, 'Accepted');
    assert.equal(h.elements['nav-backlog-badge'].getAttribute('title'), 'Successor review: Pending · Review the Backlog successor.');
    assert.equal(h.elements['nav-roadmap-badge'].textContent, 'Accepted');
    assert.equal(h.elements['nav-roadmap-badge'].getAttribute('title'), 'Successor review: Feedback · Clarify the Roadmap successor.');

    state.specification.review = { state: 'pending', rationale: 'Review the current Specification.' };
    h.setState(state); h.context.renderMasterStageNav();
    assert.equal(h.elements['nav-specification-badge'].textContent, 'Review');
    assert.equal(h.elements['nav-specification-badge'].getAttribute('title'), 'Review: Pending · Review the current Specification.');
});

test('Sprint badge clears when a later projection is absent', async () => {
    const h = projectLifecycleHarness({ template: true });
    const state = activeSprintLifecycleState();
    const validated = await h.context.validateSprintStatusProjection(state.sprintStatus.data, 7);
    assert.ok(validated, 'The rendered Sprint fixture must pass the shipped validator');
    state.sprintStatus = { kind: 'ready', data: validated };
    h.setState(state); h.context.renderMasterStageNav();
    assert.equal(h.elements['nav-sprint-badge'].textContent, 'Active');
    state.sprintStatus = { kind: 'absent' };
    h.setState(state); h.context.renderMasterStageNav();
    assert.equal(h.elements['nav-sprint-badge'].textContent, 'Not started');
    assert.equal(h.elements['nav-sprint-badge'].getAttribute('title'), '');
    state.sprintStatus = { kind: 'error', message: 'Sprint read failed.' };
    h.setState(state); h.context.renderMasterStageNav();
    assert.equal(h.elements['nav-sprint-badge'].textContent, 'Unavailable');
    assert.equal(h.elements['nav-sprint-badge'].getAttribute('title'), 'Sprint read failed.');
    state.sprintStatus = { kind: 'absent' };
    h.setState(state); h.context.renderMasterStageNav();
    assert.equal(h.elements['nav-sprint-badge'].textContent, 'Not started');
    assert.equal(h.elements['nav-sprint-badge'].getAttribute('title'), '');
});

test('rendered Roadmap read failures and absence clear accepted labels and details', () => {
    const h = projectLifecycleHarness({ template: true });
    const state = acceptedLifecycleState();
    state.planningReviews.roadmap = { review: { review: { state: 'pending', rationale: 'Review the Roadmap successor.' } } };
    h.setState(state); h.context.renderMasterStageNav();
    assert.equal(h.elements['nav-roadmap-badge']?.textContent, 'Accepted');
    assert.equal(h.elements['nav-roadmap-badge'].getAttribute('title'), 'Successor review: Pending · Review the Roadmap successor.');
    state.acceptedRoadmap = { kind: 'error', message: 'Roadmap read failed.' };
    h.setState(state); h.context.renderMasterStageNav();
    assert.equal(h.elements['nav-roadmap-badge'].textContent, 'Unavailable');
    assert.equal(h.elements['nav-roadmap-badge'].getAttribute('title'), 'Roadmap read failed.');
    state.acceptedRoadmap = { kind: 'ready', data: { state: 'absent', project_id: 7 } };
    state.planningReviews.roadmap = {};
    h.setState(state); h.context.renderMasterStageNav();
    assert.equal(h.elements['nav-roadmap-badge'].textContent, 'Not started');
    assert.equal(h.elements['nav-roadmap-badge'].getAttribute('title'), '');
});

test('every badge render replaces earlier evidence for loading or unavailable reads', () => {
    const h = projectLifecycleHarness({ template: true });
    for (const [kind, label] of [['loading', 'Loading…'], ['unavailable', 'Unavailable']]) {
        h.setState(acceptedLifecycleState()); h.context.renderMasterStageNav();
        h.setState(null, kind); h.context.renderMasterStageNav();
        for (const id of ['nav-vision-badge', 'nav-goal-badge', 'nav-specification-badge', 'nav-backlog-badge', 'nav-roadmap-badge', 'nav-sprint-badge']) {
            assert.equal(h.elements[id]?.textContent, label, `${id} must reflect ${kind} readiness`);
            assert.equal(h.elements[id].getAttribute('title'), '', `${id} must clear earlier detail`);
        }
    }
});

test('renderDashboard mounts Specification as the only current stage and default workbench', async () => {
    const h = projectLifecycleHarness({ template: true });
    assert.equal(h.state('lifecycleDisplayRead.kind'), 'loading');
    assert.equal(await h.context.loadDashboard(), true);
    // loadDashboard calls the actual renderDashboard and the actual workspace mount.
    const map = h.elements['workspace-map-host'];
    const stages = map.querySelectorAll('[data-workspace-stage]');
    assert.deepEqual(stages.filter((stage) => stage.getAttribute('aria-current') === 'step').map((stage) => Number(stage.dataset.workspaceStage)), [4]);
    assert.equal(map.querySelector('#workspace-stage-4').getAttribute('aria-current'), 'step');
    assert.equal(map.querySelector('[data-workspace-return-current]'), null);
    assert.equal(h.elements['workbench-stage-title'].textContent, 'Specification');
    assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Project Framing');
    assert.equal(h.elements['cockpit-active-stage-label'].textContent, 'Specification');
    assert.equal(h.elements['cockpit-vision-anchor'].textContent, 'Vision: Accepted · Accepted direction');
    for (const [id, label, detail] of [
        ['nav-vision-badge', 'Complete', ''],
        ['nav-goal-badge', 'Active', ''],
        ['nav-specification-badge', 'Source registered', 'Awaiting structuring'],
        ['nav-backlog-badge', 'Blocked', 'SPECIFICATION_NOT_APPROVED'],
        ['nav-roadmap-badge', 'Blocked', 'BACKLOG_NOT_ACCEPTED'],
        ['nav-sprint-badge', 'Not started', ''],
    ]) {
        assert.equal(h.elements[id].textContent, label, id);
        assert.equal(h.elements[id].getAttribute('title'), detail, `${id} detail`);
    }
    assert.equal(h.state('lifecycleDisplayRead.kind'), 'ready');
    assert.deepEqual(h.requests.map((request) => [request.url, request.options.method ?? 'GET']), [['/api/projects/7/dashboard', 'GET']]);
});

test('first page load stays neutral while pending and becomes unavailable after failure', async () => {
    let resolveRead;
    const h = projectLifecycleHarness({ template: true, fetchImpl: () => new Promise((resolve) => { resolveRead = resolve; }) });
    const loading = h.start();
    assert.equal(h.elements['cockpit-active-stage-label'].textContent, 'Loading…');
    assert.equal(h.elements['cockpit-vision-anchor'].textContent, 'Vision: Loading…');
    assert.equal(h.elements['cockpit-goal-status'].textContent, 'Loading…');
    for (const id of lifecycleBadgeIds) assert.equal(h.elements[id].textContent, 'Loading…', id);
    assert.equal(h.elements['workbench-stage-title'].textContent, 'Loading…');
    assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Loading…');
    const map = h.elements['workspace-map-host'];
    assert.equal(map.querySelector('[aria-current="step"]'), null);
    assert.equal(map.querySelector('.workspace-map-freshness').textContent, 'Loading lifecycle · Manual refresh required');
    assert.equal(map.querySelector('.workspace-route-note').textContent, 'Loading workflow position…');
    assert.equal(map.querySelector('[data-workspace-return-current]'), null);
    assert.equal(map.querySelector('time'), null);
    resolveRead(dashboardResponse({ message: 'Initial lifecycle read failed.' }, 503));
    await loading;
    assert.equal(h.state('lifecycleDisplayRead.kind'), 'unavailable');
    assert.equal(h.elements['cockpit-active-stage-label'].textContent, 'Unavailable');
    assert.equal(h.elements['cockpit-vision-anchor'].textContent, 'Vision: Unavailable');
    assert.equal(h.elements['cockpit-goal-status'].textContent, 'Unavailable');
    for (const id of lifecycleBadgeIds) {
        assert.equal(h.elements[id].textContent, 'Unavailable', id);
        assert.equal(h.elements[id].getAttribute('title'), '', `${id} detail`);
    }
    assert.equal(map.querySelector('[aria-current="step"]'), null);
    assert.equal(map.querySelector('.workspace-map-freshness').textContent, 'Unavailable lifecycle · Manual refresh required');
    assert.equal(map.querySelector('.workspace-route-note').textContent, 'Workflow position unavailable.');
    assert.equal(map.querySelector('[data-workspace-return-current]'), null);
    assert.equal(map.querySelector('time'), null);
    assert.equal(h.elements['workbench-stage-title'].textContent, 'Unavailable');
    assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Project lifecycle');
    assert.equal(h.elements['project-error'].textContent, 'Initial lifecycle read failed.');
    assert.match(h.elements['dashboard-refresh-time'].textContent, /Manual refresh required/);
    assert.equal(h.elements['specification-panel'].hidden, true, 'An unavailable read does not select Specification');
    await map.querySelector('[data-workspace-stage="4"]').click();
    assert.equal(h.elements['specification-panel'].hidden, false);
    assert.equal(h.elements['workbench-stage-title'].textContent, 'Specification');
    assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Viewing · Project Framing');
    assert.equal(map.querySelector('[aria-current="step"]'), null);
    assert.equal(map.querySelector('[data-workspace-return-current]'), null);
    assert.equal(map.querySelector('.workspace-map-freshness').textContent, 'Unavailable lifecycle · Manual refresh required');
    assert.equal(map.querySelector('time'), null);
});

test('initial failed load retains explicit historical delivery Viewing context', async () => {
    const h = projectLifecycleHarness({
        template: true, seededView: { stageId: 9, sprintId: null, taskId: null, tab: 'checks', filter: 'all', scopeKey: null },
        fetchImpl: async (url) => {
            assert.equal(url, '/api/projects/7/dashboard');
            return dashboardResponse({ message: 'Initial lifecycle read failed.' }, 503);
        },
    });
    await h.start();
    assert.equal(h.state('workspaceView.stageId'), 9);
    assert.equal(h.elements['workbench-stage-title'].textContent, 'Develop & verify');
    assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Viewing · Delivery Loop');
    assert.equal(h.elements['workspace-map-host'].querySelector('[aria-current="step"]'), null);
    assert.match(h.elements['workspace-map-host'].querySelector('#workspace-stage-9').textContent, /Viewing/);
});

test('initial failed load preserves a null view until successful recovery defaults to Specification', async () => {
    const responses = [
        dashboardResponse({ message: 'Initial lifecycle read failed.' }, 503),
        dashboardResponse(sourceOnlyDashboardBundle()),
    ];
    const h = projectLifecycleHarness({ template: true, fetchImpl: async (url) => {
        assert.equal(url, '/api/projects/7/dashboard');
        return responses.shift();
    } });
    await h.start();
    assert.equal(h.state('lifecycleDisplayRead.kind'), 'unavailable');
    assert.equal(h.state('workspaceView'), null, 'A failed initial read must not create a stage selection');
    assert.equal(h.state('lastDashboardConfirmedAt'), null);
    assert.equal(await h.context.loadDashboard(), true);
    assert.equal(h.state('lifecycleDisplayRead.kind'), 'ready');
    assert.equal(h.state('workspaceView.stageId'), 4);
    assert.equal(h.elements['workbench-stage-title'].textContent, 'Specification');
    assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Project Framing');
    const map = h.elements['workspace-map-host'];
    assert.deepEqual(map.querySelectorAll('[aria-current="step"]').map((stage) => Number(stage.dataset.workspaceStage)), [4]);
    assert.equal(map.querySelector('[data-workspace-return-current]'), null);
    assert.equal(h.elements['cockpit-active-stage-label'].textContent, 'Specification');
    assert.equal(h.elements['cockpit-vision-anchor'].textContent, 'Vision: Accepted · Accepted direction');
    assert.equal(h.elements['cockpit-goal-status'].textContent, 'Active');
    for (const [id, label, detail] of [
        ['nav-vision-badge', 'Complete', ''],
        ['nav-goal-badge', 'Active', ''],
        ['nav-specification-badge', 'Source registered', 'Awaiting structuring'],
        ['nav-backlog-badge', 'Blocked', 'SPECIFICATION_NOT_APPROVED'],
        ['nav-roadmap-badge', 'Blocked', 'BACKLOG_NOT_ACCEPTED'],
        ['nav-sprint-badge', 'Not started', ''],
    ]) {
        assert.equal(h.elements[id].textContent, label, id);
        assert.equal(h.elements[id].getAttribute('title'), detail, `${id} detail`);
    }
    const confirmedAt = h.state('lastDashboardConfirmedAt');
    assert.ok(confirmedAt);
    assert.ok(h.elements['dashboard-refresh-time'].getAttribute('title').includes(confirmedAt));
    assert.match(h.elements['dashboard-refresh-time'].textContent, /Confirmed/);
    assert.match(map.textContent, /Manual refresh required/);
    assert.equal(h.elements['project-error'].textContent, '');
});

test('successful same-project refresh replaces every badge and clears accepted Roadmap and active Sprint', async () => {
    const initial = acceptedDashboardBundle();
    initial.data.sprintStatusResponse = { status: 200, body: { status: 'success', data: activeSprintLifecycleState().sprintStatus.data } };
    const later = sourceOnlyDashboardBundle();
    later.data.vision.body.data.current = null;
    later.data.vision.body.data.candidate = { statement: 'Draft direction', review_fingerprint: 'sha256:draft' };
    later.data.vision.body.data.review = { state: 'pending' };
    later.data.goal.body.data.active = null;
    later.data.goal.body.data.outcome = { outcome: 'fulfilled', statement: 'Outcome delivered.' };
    later.data.acceptedRoadmap = { status: 503, body: { message: 'Roadmap read failed.' } };
    const absent = structuredClone(later);
    absent.data.acceptedRoadmap = sourceOnlyDashboardBundle().data.acceptedRoadmap;
    const responses = [initial, later, absent];
    const h = projectLifecycleHarness({ template: true, fetchImpl: async (url) => {
        assert.equal(url, '/api/projects/7/dashboard');
        return dashboardResponse(responses.shift());
    } });
    // Keep a deliberate framing view so Sprint inventory reads are irrelevant to these labels.
    h.state('workspaceView = { ...AgileForgeWorkspace.createView(), stageId: 4 };');
    assert.equal(await h.context.loadDashboard(), true);
    assert.equal(h.elements['cockpit-active-stage-label'].textContent, 'Develop & verify');
    assert.deepEqual(lifecycleBadgeIds.map((id) => h.elements[id].textContent), ['Complete', 'Active', 'Accepted', 'Accepted', 'Accepted', 'Active']);
    assert.equal(await h.context.loadDashboard(), true);
    assert.equal(h.elements['cockpit-active-stage-label'].textContent, 'Specification');
    assert.equal(h.elements['cockpit-vision-anchor'].textContent, 'Vision: Draft · Draft direction');
    assert.equal(h.elements['cockpit-goal-status'].textContent, 'Fulfilled');
    const laterBadges = [
        ['Draft', 'Review: Pending'], ['Fulfilled', ''], ['Source registered', 'Awaiting structuring'],
        ['Blocked', 'SPECIFICATION_NOT_APPROVED'], ['Unavailable', 'Roadmap read failed.'], ['Not started', ''],
    ];
    lifecycleBadgeIds.forEach((id, index) => {
        assert.equal(h.elements[id].textContent, laterBadges[index][0], id);
        assert.equal(h.elements[id].getAttribute('title'), laterBadges[index][1], `${id} detail`);
    });
    assert.equal(await h.context.loadDashboard(), true);
    assert.equal(h.elements['nav-roadmap-badge'].textContent, 'Blocked');
    assert.equal(h.elements['nav-roadmap-badge'].getAttribute('title'), 'BACKLOG_NOT_ACCEPTED');
    assert.equal(h.elements['nav-sprint-badge'].textContent, 'Not started');
    assert.equal(h.state('workspaceView.stageId'), 4);
});

for (const status of [503, 409]) {
    test(`failed same-project refresh (${status}) retains last confirmed labels and details`, async () => {
        const confirmed = acceptedDashboardBundle();
        confirmed.data.backlogReview.body.data = { review: { review: { state: 'pending', rationale: 'Review the Backlog successor.' } } };
        confirmed.data.roadmapReview.body.data = { review: { review: { state: 'feedback', rationale: 'Clarify the Roadmap successor.' } } };
        let resolveRefresh;
        const h = projectLifecycleHarness({ template: true, fetchImpl: async (url) => {
            assert.equal(url, '/api/projects/7/dashboard');
            if (h.requests.length === 1) return dashboardResponse(confirmed);
            return new Promise((resolve) => { resolveRefresh = resolve; });
        } });
        assert.equal(await h.context.loadDashboard(), true);
        assert.equal(h.elements['nav-backlog-badge'].getAttribute('title'), 'Successor review: Pending · Review the Backlog successor.');
        assert.equal(h.elements['nav-roadmap-badge'].getAttribute('title'), 'Successor review: Feedback · Clarify the Roadmap successor.');
        const lastLabels = renderedLifecycleLabels(h);
        const lastConfirmed = h.state('lastDashboardConfirmedAt');
        const map = h.elements['workspace-map-host'];
        const lastFreshness = map.querySelector('.workspace-map-freshness').textContent;
        const lastRouteNote = map.querySelector('.workspace-route-note').textContent;
        const refreshing = h.context.loadDashboard();
        assert.deepEqual(renderedLifecycleLabels(h), lastLabels, 'Pending refresh keeps confirmed labels');
        assert.equal(map.querySelector('.workspace-map-freshness').textContent, lastFreshness);
        assert.equal(map.querySelector('time').getAttribute('datetime'), lastConfirmed);
        resolveRefresh(dashboardResponse({ message: 'Lifecycle refresh failed.' }, status));
        await assert.rejects(refreshing, /Lifecycle refresh failed\./);
        assert.deepEqual(renderedLifecycleLabels(h), lastLabels, 'Failed refresh keeps confirmed labels and detail');
        assert.equal(h.state('lastDashboardConfirmedAt'), lastConfirmed);
        assert.equal(h.state('lifecycleDisplayRead.kind'), 'ready');
        assert.equal(map.querySelector('.workspace-map-freshness').textContent, lastFreshness);
        assert.equal(map.querySelector('.workspace-route-note').textContent, lastRouteNote);
        assert.equal(map.querySelector('time').getAttribute('datetime'), lastConfirmed);
        assert.ok(h.elements['dashboard-refresh-time'].getAttribute('title').includes(lastConfirmed));
        if (status === 409) assert.deepEqual(Object.keys(h.state('lifecycleState.planningReviews.backlog')), [], 'Conflict still clears action review state');
    });
}

for (const reconcileStart of [false, true]) {
    test(`accepted core read stays coherent when its inventory refresh is superseded by a failed read${reconcileStart ? ' during Sprint-start reconciliation' : ''}`, async () => {
        const initial = acceptedDashboardBundle();
        initial.data.sprintStatusResponse = { status: 200, body: { data: activeSprintLifecycleState().sprintStatus.data } };
        const later = reconcileStart ? structuredClone(initial) : completedSprintPendingPlanBundle();
        if (reconcileStart) {
            const sprint = initial.data.sprintStatusResponse.body.data;
            sprint.sprint.status = 'planned';
            sprint.effective_status = 'planned';
            sprint.accepted_plan.status = 'planned';
            sprint.start = null;
        }
        const expectedStage = reconcileStart ? 9 : 8;
        const expectedPhase = reconcileStart ? 'Develop & verify' : 'Sprint plan & start';
        later.data.vision.body.data.current.statement = 'Second accepted direction';
        later.data.goal.body.data.active = null;
        later.data.goal.body.data.outcome = { outcome: 'fulfilled', statement: 'Second outcome delivered.' };
        const initialTime = '2026-10-07T10:00:00.000Z';
        const laterTime = '2026-10-07T11:00:00.000Z';
        const failedTime = '2026-10-07T12:00:00.000Z';
        const laterTimeLabel = new Date(laterTime).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        let now = initialTime;
        class LifecycleClock extends Date {
            constructor(...args) { super(...(args.length ? args : [now])); }
            static now() { return Date.parse(now); }
        }
        let dashboard = dashboardResponse(initial);
        let stallInventory = false;
        let markInventoryStarted;
        let releaseInventory;
        const inventoryStarted = new Promise((resolve) => { markInventoryStarted = resolve; });
        const inventoryResponse = new Promise((resolve) => { releaseInventory = resolve; });
        const inventory = { data: { project_id: 7, sprint_id: 71, items: initial.data.sprintStatusResponse.body.data.tasks } };
        const h = projectLifecycleHarness({ template: true, dateImpl: LifecycleClock, fetchImpl: async (url, options) => {
            assert.equal(options.method ?? 'GET', 'GET', 'The race must contain only provider-free reads');
            if (url === '/api/projects/7/dashboard') return dashboard;
            assert.equal(url, '/api/projects/7/sprints/71/tasks', 'The real selected-Sprint inventory must cause the stall');
            if (!stallInventory) return dashboardResponse(inventory);
            markInventoryStarted();
            return inventoryResponse;
        } });
        h.state('workspaceView = { ...AgileForgeWorkspace.createView(), stageId: 9, sprintId: 71 };');
        assert.equal(await h.context.loadDashboard(), true);
        const map = h.elements['workspace-map-host'];
        const observe = () => ({
            labels: renderedLifecycleLabels(h),
            current: map.querySelectorAll('[aria-current="step"]').map((node) => Number(node.dataset.workspaceStage)),
            freshness: h.elements['dashboard-refresh-time'].getAttribute('title'),
            freshnessLabel: h.elements['dashboard-refresh-time'].textContent,
            mapTime: map.querySelector('time')?.getAttribute('datetime'),
            mapTimeLabel: map.querySelector('time')?.textContent,
            confirmedAt: h.state('lastDashboardConfirmedAt'),
        });
        const initialDisplay = observe();
        assert.equal(initialDisplay.labels['cockpit-active-stage-label'].label, reconcileStart ? 'Sprint plan & start' : 'Develop & verify');
        assert.equal(initialDisplay.confirmedAt, initialTime);
        if (reconcileStart) {
            const plan = later.data.sprintStatusResponse.body.data.accepted_plan;
            h.context.startBinding = {
                kind: 'original', sprintId: 71, sprintPlanArtifactId: plan.sprint_plan_artifact_id,
                sprintPlanArtifactDecisionId: plan.sprint_plan_artifact_decision_id,
                planFingerprint: plan.plan_fingerprint, candidateSetFingerprint: plan.candidate_set_fingerprint,
                taskContentFingerprint: plan.task_content_fingerprint,
            };
            h.state("activeSprintMutation = { token: 'fixture-start', phase: 'awaiting_authority', binding: startBinding };");
            // A browser dialog exposes close(); the real reconciliation callback still runs.
            h.elements['human-action-dialog'].close = () => {};
        }

        now = laterTime;
        dashboard = dashboardResponse(later);
        stallInventory = true;
        const readA = h.context.loadDashboard();
        await inventoryStarted;
        const pendingDisplay = observe();
        // A real stage selection renders while A is still awaiting the real inventory fetch.
        await map.querySelector('#workspace-stage-13').click();
        const pendingSelectionDisplay = observe();
        const acceptedState = h.state('lifecycleState');
        const pendingStartMutation = h.state('activeSprintMutation');

        now = failedTime;
        dashboard = dashboardResponse({ message: 'Newer lifecycle read failed.' }, 503);
        await assert.rejects(h.context.loadDashboard(), /Newer lifecycle read failed\./);
        releaseInventory(dashboardResponse(inventory));
        assert.equal(await readA, false, 'The superseded loader must keep its existing false result');
        await map.querySelector('#workspace-stage-4').click();
        const finalDisplay = observe();
        await map.querySelector('[data-workspace-return-current]').click();

        for (const [point, display] of [['pending selection', pendingSelectionDisplay], ['after supersession', finalDisplay]]) {
            assert.equal(display.labels['cockpit-active-stage-label'].label, expectedPhase, point);
            assert.equal(display.labels['cockpit-vision-anchor'].label, 'Vision: Accepted · Second accepted direction', point);
            assert.equal(display.labels['cockpit-goal-status'].label, 'Fulfilled', point);
            assert.deepEqual(lifecycleBadgeIds.map((id) => display.labels[id].label), ['Complete', 'Fulfilled', 'Accepted', 'Accepted', 'Accepted', reconcileStart ? 'Active' : 'Completed'], point);
            assert.deepEqual(display.current, [expectedStage], point);
            assert.equal(display.confirmedAt, laterTime, point);
            assert.equal(display.mapTime, laterTime, point);
            assert.equal(display.mapTimeLabel, laterTimeLabel, point);
            assert.equal(display.freshness, `Last confirmed ${laterTime}; manual refresh required`, point);
            assert.equal(display.freshnessLabel, `Confirmed ${laterTimeLabel} · Manual refresh`, point);
            assert.ok(!display.freshness.includes(failedTime), point);
        }
        assert.equal(acceptedState.vision.current.statement, 'Second accepted direction');
        assert.equal(acceptedState.sprintStatus.data.effective_status, reconcileStart ? 'active' : 'completed');
        const pendingRenderedRead = reconcileStart ? pendingSelectionDisplay : initialDisplay;
        assert.deepEqual(pendingDisplay.labels, pendingRenderedRead.labels, 'A synchronous reconciliation render publishes the whole new read; otherwise the previous visible read stays together');
        assert.equal(pendingDisplay.freshness, pendingRenderedRead.freshness, 'Freshness must not advance ahead of the rendered labels');
        assert.equal(pendingDisplay.freshnessLabel, pendingRenderedRead.freshnessLabel, 'The visible confirmation note must match the visible labels');
        assert.equal(pendingDisplay.mapTimeLabel, pendingRenderedRead.mapTimeLabel);
        assert.equal(pendingDisplay.mapTime, reconcileStart ? laterTime : initialTime);
        assert.equal(pendingDisplay.confirmedAt, laterTime, 'The successful core read is already accepted internally');
        if (reconcileStart) assert.equal(pendingStartMutation, null, 'The real synchronous Sprint-start reconciliation must have rendered before the stall');
        assert.equal(h.state('workspaceView.stageId'), expectedStage, 'Return targets A rather than old S1 or failed B');
        assert.equal(h.elements['workbench-stage-title'].textContent, expectedPhase);
        assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Delivery Loop');
        assert.equal(map.querySelector('[data-workspace-return-current]'), null);
        assert.deepEqual(observe().current, [expectedStage]);
        assert.ok(h.requests.some((request) => request.url === '/api/projects/7/sprints/71/tasks'));
        assert.ok(h.requests.some((request) => request.url === '/api/projects/7/dashboard' && request.options.signal.aborted), 'B must actually supersede and abort A');
    });
}

test('409 clears review authority without moving confirmed Sprint progress and recovery replaces it', async () => {
    const confirmed = completedSprintPendingPlanBundle();
    const recovery = structuredClone(confirmed);
    recovery.data.sprintPlanReview.body.data = {};
    recovery.data.position.body.data.decisions = [];
    const responses = [
        dashboardResponse(confirmed),
        dashboardResponse({ message: 'Lifecycle refresh failed.' }, 409),
        dashboardResponse(recovery),
    ];
    const h = projectLifecycleHarness({ template: true, fetchImpl: async (url) => {
        assert.equal(url, '/api/projects/7/dashboard');
        return responses.shift();
    } });
    await h.start();
    const map = h.elements['workspace-map-host'];
    const currentMarkers = () => map.querySelectorAll('[aria-current="step"]').map((node) => Number(node.dataset.workspaceStage));
    const confirmedLabels = renderedLifecycleLabels(h);
    assert.deepEqual(currentMarkers(), [8]);
    assert.equal(h.state('workspaceView.stageId'), 8);
    assert.equal(h.elements['workbench-stage-title'].textContent, 'Sprint plan & start');
    assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Delivery Loop');
    assert.equal(map.querySelector('[data-workspace-return-current]'), null);

    await assert.rejects(h.context.loadDashboard(), /Lifecycle refresh failed\./);
    for (const phase of ['backlog', 'roadmap', 'sprintPlan']) {
        assert.deepEqual(Object.keys(h.state(`lifecycleState.planningReviews.${phase}`)), [], `${phase} authority remains cleared`);
    }
    assert.deepEqual(Array.from(h.state('lifecycleState.planningReviews.stories.items')), []);
    assert.deepEqual(renderedLifecycleLabels(h), confirmedLabels);
    assert.deepEqual(currentMarkers(), [8]);
    assert.equal(h.state('workspaceView.stageId'), 8);
    assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Delivery Loop');
    assert.equal(map.querySelector('[data-workspace-return-current]'), null, '409 must not invent a new Return destination');

    await map.querySelector('#workspace-stage-13').click();
    assert.deepEqual(currentMarkers(), [8]);
    assert.equal(h.elements['workbench-stage-title'].textContent, 'Assess next Sprint');
    assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Viewing · Delivery Loop');
    assert.match(map.querySelector('#workspace-stage-13').textContent, /Viewing/);
    await map.querySelector('[data-workspace-return-current]').click();
    assert.equal(h.state('workspaceView.stageId'), 8, 'Return uses the same confirmed primary stage as the map');
    assert.equal(h.elements['workbench-stage-title'].textContent, 'Sprint plan & start');
    assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Delivery Loop');
    assert.equal(map.querySelector('[data-workspace-return-current]'), null);

    await map.querySelector('#workspace-stage-9').click();
    assert.equal(await h.context.loadDashboard(), true);
    assert.equal(h.state('workspaceView.stageId'), 9, 'A successful snapshot must retain deliberate viewing');
    assert.deepEqual(currentMarkers(), [13]);
    assert.equal(h.elements['cockpit-active-stage-label'].textContent, 'Assess next Sprint');
    assert.equal(h.elements['workbench-stage-title'].textContent, 'Develop & verify');
    assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Viewing · Delivery Loop');
    assert.match(map.querySelector('#workspace-stage-9').textContent, /Viewing/);
    await map.querySelector('[data-workspace-return-current]').click();
    assert.equal(h.state('workspaceView.stageId'), 13);
    assert.equal(h.elements['workbench-stage-title'].textContent, 'Assess next Sprint');
    assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Delivery Loop');
    assert.equal(map.querySelector('[data-workspace-return-current]'), null);
    assert.equal(responses.length, 0);
    for (const request of h.requests) assert.equal(request.options.method ?? 'GET', 'GET');
});

test('deliberate Sprint view survives same-project refresh and browser Back/Forward', async () => {
    const seededView = { stageId: 4, sprintId: null, taskId: 91, tab: 'checks', filter: 'blocked', scopeKey: null };
    const h = projectLifecycleHarness({ template: true, seededView });
    await h.start();
    const map = h.elements['workspace-map-host'];
    await map.querySelector('#workspace-stage-8').click();
    assert.equal(h.state('workspaceView.stageId'), 8);
    assert.ok(map.querySelector('[data-workspace-return-current]'), 'Mounted map exposes Return to current work when viewing Sprint');
    assert.equal(h.history.state.agileForgeWorkspace.stageId, 8);
    assert.equal(await h.context.loadDashboard(), true);
    assert.equal(h.state('workspaceView.stageId'), 8);
    assert.equal(h.elements['workbench-stage-title'].textContent, 'Sprint plan & start');
    assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Viewing · Delivery Loop');
    assert.equal(h.elements['cockpit-active-stage-label'].textContent, 'Specification');
    assert.equal(map.querySelector('#workspace-stage-4').getAttribute('aria-current'), 'step');
    assert.equal(map.querySelector('#workspace-stage-8').getAttribute('aria-current'), null);
    assert.match(map.querySelector('#workspace-stage-8').textContent, /Viewing/);
    assert.doesNotMatch(map.querySelector('#workspace-stage-4').textContent, /Viewing/);
    for (const [key, value] of Object.entries(seededView).filter(([key]) => key !== 'stageId')) assert.equal(h.state(`workspaceView.${key}`), value, key);
    await map.querySelector('[data-workspace-return-current]').click();
    assert.equal(h.state('workspaceView.stageId'), 4);
    assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Project Framing');
    await h.history.back();
    assert.equal(h.state('workspaceView.stageId'), 8);
    assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Viewing · Delivery Loop');
    assert.match(map.querySelector('#workspace-stage-8').textContent, /Viewing/);
    await h.history.forward();
    assert.equal(h.state('workspaceView.stageId'), 4);
    assert.equal(h.elements['workbench-stage-title'].textContent, 'Specification');
    assert.equal(h.elements['workbench-stage-kicker'].textContent, 'Project Framing');
    for (const [key, value] of Object.entries(seededView).filter(([key]) => key !== 'stageId')) {
        assert.equal(h.state(`workspaceView.${key}`), value, key);
        assert.equal(h.history.state.agileForgeWorkspace[key], value, `history ${key}`);
    }
    assert.ok(h.requests.length > 0);
    for (const request of h.requests) {
        assert.equal(request.url, '/api/projects/7/dashboard');
        assert.equal(request.options.method ?? 'GET', 'GET');
    }
});

test('accepted Vision remains the summary anchor during revision', () => {
    const h = projectLifecycleHarness();
    const state = sourceOnlyLifecycleState();
    state.vision.candidate = { statement: 'Candidate direction', review_fingerprint: 'sha256:vision-revision' };
    state.vision.review = { state: 'pending', rationale: null };
    state.vision.draft = { statement: 'Unfinished direction', is_complete: false, clarifying_questions: [] };
    const display = h.display(state);
    assert.equal(display.visionLabel, 'Vision: Accepted · Accepted direction');
    assert.equal(display.badges.Vision.label, 'Complete');
    assert.match(display.badges.Vision.detail, /pending/i);
});

test('long markup-looking Vision statements remain text in the rendered summary', () => {
    const h = projectLifecycleHarness();
    const state = sourceOnlyLifecycleState();
    state.vision.current.statement = '<b>Accepted direction</b> ' + 'Evidence stays identifiable. '.repeat(20);
    h.setState(state); h.context.renderTopCockpit();
    const anchor = h.elements['cockpit-vision-anchor'];
    assert.ok(anchor.textContent.startsWith('Vision: Accepted · <b>Accepted direction'));
    assert.equal(anchor.children.length, 0);
});

test('draft and absent Vision are distinct from accepted direction', () => {
    const h = projectLifecycleHarness();
    const state = sourceOnlyLifecycleState(); state.vision.current = null;
    state.vision.candidate = { statement: 'Draft direction', review_fingerprint: 'sha256:draft' };
    state.vision.review = { state: 'pending' };
    assert.equal(h.display(state).visionLabel, 'Vision: Draft · Draft direction');
    state.vision.candidate = null; state.vision.review = null;
    assert.equal(h.display(state).visionLabel, 'Vision: Direction pending');
    assert.notEqual(h.display(state).badges.Vision.label, 'Complete');
});

test('current work follows workspace semantics for permutations, concurrency, and locked transport', () => {
    const h = projectLifecycleHarness();
    const source = sourceOnlyLifecycleState();
    const optional = source.position.decisions[0];
    const required = source.position.decisions[1];
    const other = { request_kind: 'record_story_draft', category: 'available', recommendation_kind: 'required' };
    const cases = [
        { decisions: [...source.position.decisions].reverse(), label: 'Specification', current: [4], primary: 4 },
        { decisions: [optional], label: 'No current stage', current: [], primary: null },
        { decisions: [required, other], label: 'Multiple current stages', current: [4, 7], primary: null },
        { decisions: [other, required], label: 'Multiple current stages', current: [4, 7], primary: null },
        { decisions: [required], locked: true, label: 'Specification', current: [4], primary: 4 },
    ];
    for (const fixture of cases) {
        const state = sourceOnlyLifecycleState(); state.position.decisions = fixture.decisions;
        if (fixture.locked) state.actions[0].availability = 'locked';
        const display = h.display(state);
        assert.deepEqual(Array.from(display.currentStageIds), fixture.current);
        assert.equal(display.primaryStageId, fixture.primary);
        assert.equal(display.phaseLabel, fixture.label);
        assert.deepEqual(Array.from(display.currentStageIds), Array.from(h.workspace().currentStageIds(state.position, state)));
        assert.equal(display.primaryStageId, h.workspace().initialStageId(state.position, state));
    }
});

test('accepted artifacts do not invent current work from absent satisfied decisions', () => {
    const h = projectLifecycleHarness(); const display = h.display(acceptedLifecycleState());
    assert.equal(display.phaseLabel, 'No current stage');
    assert.deepEqual(Array.from(display.currentStageIds), []);
    assert.equal(display.badges.Specification.label, 'Accepted');
    assert.equal(display.badges.Backlog.label, 'Accepted');
    assert.equal(display.badges.Roadmap.label, 'Accepted');
});

test('validated active Sprint and optional source reentry keep delivery primary', async () => {
    const h = projectLifecycleHarness(); const state = activeSprintLifecycleState();
    const validated = await h.context.validateSprintStatusProjection(state.sprintStatus.data, 7);
    assert.ok(validated, 'The fixture must pass the shipped Sprint validator');
    state.sprintStatus = { kind: 'ready', data: validated };
    state.position.decisions = [sourceOnlyLifecycleState().position.decisions[0]];
    const display = h.display(state);
    assert.deepEqual(Array.from(display.currentStageIds), [9]);
    assert.equal(display.primaryStageId, 9);
    assert.equal(display.phaseLabel, 'Develop & verify');
    assert.deepEqual(Array.from(display.currentStageIds), Array.from(h.workspace().currentStageIds(state.position, state)));
    assert.equal(display.primaryStageId, h.workspace().initialStageId(state.position, state));
    assert.equal(display.badges.Sprint.label, 'Active');
});

test('accepted Backlog survives empty coverage and pending or Feedback successor review', () => {
    const h = projectLifecycleHarness();
    for (const reviewState of [null, 'pending', 'feedback']) {
        const state = acceptedLifecycleState(); state.acceptedRoadmap.data.state = 'absent';
        if (reviewState) {
            const packet = { phase: 'backlog', project_id: 7, candidate: { backlog_artifact_id: 52, artifact_fingerprint: 'sha256:backlog-52', supersedes_backlog_artifact_id: 51, backlog_items: [], is_complete: true, clarifying_questions: [] }, review: { state: reviewState, rationale: 'Review the successor.' } };
            state.planningReviews.backlog = reviewState === 'feedback' ? { continuation: { review: packet } } : { review: packet };
        }
        const badge = h.display(state).badges.Backlog;
        assert.equal(badge.label, 'Accepted');
        if (reviewState) assert.match(badge.detail.toLowerCase(), new RegExp(reviewState));
    }
});

test('Backlog acceptance uses a valid same-project identity rather than counts or reviews', () => {
    const h = projectLifecycleHarness();
    for (const accepted of [undefined, {}, { backlog_artifact_id: 0, artifact_fingerprint: 'sha256:b' }, { backlog_artifact_id: 51, artifact_fingerprint: '' }]) {
        const state = acceptedLifecycleState(); state.storyPending.accepted_backlog = accepted;
        state.storyPending.count = 9; state.storyPending.items = [{ backlog_item_id: 'PBI-1' }];
        assert.equal(h.display(state).badges.Backlog.label, 'Unavailable');
    }
    const wrongProject = acceptedLifecycleState(); wrongProject.storyPending.project_id = 8;
    assert.equal(h.display(wrongProject).badges.Backlog.label, 'Unavailable');
    const missing = acceptedLifecycleState(); delete missing.storyPending.accepted_backlog;
    assert.equal(h.display(missing).badges.Backlog.label, 'Unavailable');
    const absent = acceptedLifecycleState(); absent.storyPending.accepted_backlog = null;
    absent.planningReviews.backlog = { review: { phase: 'backlog', candidate: { backlog_artifact_id: 51 }, review: { state: 'pending' } } };
    assert.equal(h.display(absent).badges.Backlog.label, 'Review');
});

test('accepted Roadmap preserves its base while a successor awaits review', () => {
    const h = projectLifecycleHarness();
    for (const reviewState of [null, 'pending', 'feedback']) {
        const state = acceptedLifecycleState();
        if (reviewState) state.planningReviews.roadmap = { review: {
            phase: 'roadmap', project_id: 7,
            candidate: { roadmap_artifact_id: 62, artifact_fingerprint: 'sha256:roadmap-62', supersedes_roadmap_artifact_id: 61, roadmap_releases: [], is_complete: true, clarifying_questions: [] },
            review: { state: reviewState, rationale: 'Review the revised Roadmap.' },
        } };
        const badge = h.display(state).badges.Roadmap;
        assert.equal(badge.label, 'Accepted');
        if (reviewState) assert.match(badge.detail.toLowerCase(), new RegExp(reviewState));
    }
});

test('Roadmap read errors remain unavailable despite accepted historical review', () => {
    const h = projectLifecycleHarness(); const state = acceptedLifecycleState();
    state.acceptedRoadmap = { kind: 'error', message: 'Roadmap read failed.' };
    state.planningReviews.roadmap = { review: { review: { state: 'accepted' } } };
    assert.equal(h.display(state).badges.Roadmap.label, 'Unavailable');
    assert.equal(h.display(state).badges.Roadmap.detail, 'Roadmap read failed.');
    state.acceptedRoadmap = { kind: 'ready', data: { state: 'absent', project_id: 7 } };
    state.planningReviews.roadmap = {};
    assert.equal(h.display(state).badges.Roadmap.label, 'Not started');
});

test('Goal terminal labels consume the producer outcome enum', () => {
    const h = projectLifecycleHarness();
    for (const [outcome, label] of [['fulfilled', 'Fulfilled'], ['abandoned', 'Abandoned']]) {
        const state = sourceOnlyLifecycleState(); state.goal.active = null;
        state.goal.outcome = { product_goal_artifact_id: 21, fingerprint: 'sha256:goal-21', statement: 'Resolved outcome.', outcome, rationale: 'Outcome recorded.', decided_by: 'test-operator' };
        assert.equal(h.display(state).badges['Product Goal'].label, label);
        h.setState(state); h.context.renderTopCockpit();
        assert.equal(h.elements['cockpit-goal-status'].textContent, label);
    }
});

for (const outcome of ['fulfilled', 'abandoned']) {
    for (const [reviewState, label, detail] of [
        ['pending', 'Review', 'Review: Pending · Review the next Goal.'],
        ['feedback', 'Feedback', 'Review: Feedback · Review the next Goal.'],
    ]) {
        test(`current Goal ${reviewState} review takes precedence over prior ${outcome} outcome`, () => {
            const h = projectLifecycleHarness({ template: true });
            const state = sourceOnlyLifecycleState();
            state.goal.active = null;
            state.goal.candidate = {
                product_goal_artifact_id: 22, fingerprint: 'sha256:goal-22',
                statement: 'Goal 2 awaiting review.', goal_number: 2, revision_number: 1,
            };
            state.goal.review = { state: reviewState, rationale: 'Review the next Goal.' };
            state.goal.outcome = {
                product_goal_artifact_id: 21, fingerprint: 'sha256:goal-21',
                statement: 'Goal 1 resolved.', outcome, rationale: 'Prior Goal resolved.', decided_by: 'test-operator',
            };
            h.setState(state);
            h.context.renderTopCockpit();
            h.context.renderMasterStageNav();
            assert.equal(h.elements['cockpit-goal-statement'].textContent, 'Goal 2 awaiting review.');
            assert.equal(h.elements['cockpit-goal-status'].textContent, label);
            assert.equal(h.elements['nav-goal-badge'].textContent, label);
            assert.equal(h.elements['nav-goal-badge'].getAttribute('title'), detail);
            assert.equal(h.display(state).badges['Product Goal'].detail, detail);
        });
    }
}

for (const outcome of [null, 'fulfilled', 'abandoned']) {
    test(`retained rejected Goal candidate renders rejection ${outcome ? `before prior ${outcome} outcome` : 'without a prior outcome'}`, async () => {
        const bundle = sourceOnlyDashboardBundle();
        bundle.data.goal.body.data = {
            accepted_vision: { vision_artifact_id: 12, fingerprint: 'sha256:vision-12', statement: 'Accepted direction' },
            active: null, transcript: [], latest_questions: [],
            effective_questions: {
                questions: ['What valuable outcome should this Project achieve next?', 'What observable result will prove success?', 'What boundary keeps this Goal focused?'],
                source: 'builtin_starter',
            },
            candidate: {
                product_goal_artifact_id: 22, vision_artifact_id: 12, vision_fingerprint: 'sha256:vision-12',
                goal_number: outcome ? 2 : 1, revision_number: 1, fingerprint: 'sha256:goal-22',
                statement: 'Retained rejected Goal candidate.', components: {},
                supersedes_product_goal_artifact_id: null, source_interview_turn_id: 23,
                created_by: 'test-operator', created_at: '2026-10-06T12:00:00Z',
            },
            review: {
                state: 'rejected', product_goal_artifact_decision_id: 24, decision: 'rejected',
                rationale: 'A clearer success measure is required.', reviewer: 'test-operator', decided_at: '2026-10-06T12:30:00Z',
            },
            outcome: outcome ? {
                product_goal_artifact_id: 21, fingerprint: 'sha256:goal-21', statement: 'Prior Goal resolved.',
                outcome, rationale: 'Prior outcome recorded.', decided_by: 'test-operator',
            } : null,
            stale_reason: outcome ? 'GOAL_RESOLVED' : 'GOAL_NOT_ACTIVE',
        };
        const h = projectLifecycleHarness({ template: true, bundle });
        assert.equal(await h.context.loadDashboard(), true);
        assert.equal(h.elements['cockpit-goal-statement'].textContent, 'Retained rejected Goal candidate.');
        assert.equal(h.elements['cockpit-goal-status'].textContent, 'Rejected');
        assert.equal(h.elements['nav-goal-badge'].textContent, 'Rejected');
        assert.equal(h.elements['nav-goal-badge'].getAttribute('title'), 'Review: Rejected · A clearer success measure is required.');
        assert.equal(h.display(h.state('lifecycleState')).badges['Product Goal'].detail, 'Review: Rejected · A clearer success measure is required.');
    });
}

test('rejected Goal revision retains Active priority and conflict remains unavailable', () => {
    const h = projectLifecycleHarness({ template: true });
    const state = sourceOnlyLifecycleState();
    state.goal.candidate = { product_goal_artifact_id: 22, fingerprint: 'sha256:goal-22', statement: 'Rejected Goal revision.' };
    state.goal.review = { state: 'rejected', decision: 'rejected', rationale: 'Keep the accepted outcome.' };
    h.setState(state); h.context.renderTopCockpit(); h.context.renderMasterStageNav();
    assert.equal(h.elements['cockpit-goal-status'].textContent, 'Active');
    assert.equal(h.elements['nav-goal-badge'].textContent, 'Active');
    assert.equal(h.elements['nav-goal-badge'].getAttribute('title'), 'Revision: Rejected · Keep the accepted outcome.');
    state.goal.stale_reason = 'PRODUCT_GOAL_FACT_CONFLICT';
    h.setState(state); h.context.renderTopCockpit(); h.context.renderMasterStageNav();
    assert.equal(h.elements['cockpit-goal-status'].textContent, 'Unavailable');
    assert.equal(h.elements['nav-goal-badge'].textContent, 'Unavailable');
    assert.equal(h.elements['nav-goal-badge'].getAttribute('title'), 'PRODUCT_GOAL_FACT_CONFLICT');
});

test('Specification review reports exact acceptance, source-only and conflict states', () => {
    const h = projectLifecycleHarness(); const state = acceptedLifecycleState();
    state.specification.review = null;
    assert.equal(h.display(state).badges.Specification.label, 'Unavailable');
    state.specification.stale_reason = 'SPECIFICATION_REVIEW_CONFLICT';
    assert.equal(h.display(state).badges.Specification.label, 'Unavailable');
    assert.equal(h.display(state).badges.Specification.detail, 'SPECIFICATION_REVIEW_CONFLICT');
    const sourceOnly = sourceOnlyLifecycleState();
    assert.equal(h.display(sourceOnly).badges.Specification.label, 'Source registered');
});

test('missing Specification source identity cannot claim registration or exact acceptance', () => {
    const h = projectLifecycleHarness();
    const sourceOnly = sourceOnlyLifecycleState(); sourceOnly.specification.source = {};
    assert.equal(h.display(sourceOnly).badges.Specification.label, 'Unavailable');
    const accepted = acceptedLifecycleState(); accepted.specification.source = {};
    delete accepted.specification.candidate.specification_source_id;
    delete accepted.specification.candidate.registered_source_fingerprint;
    assert.equal(h.display(accepted).badges.Specification.label, 'Unavailable');
});

test('#267 null non-current source stays truthful beside accepted Backlog and active Sprint', () => {
    const h = projectLifecycleHarness(); const state = activeSprintLifecycleState();
    state.specification = { schema_version: 'agileforge.specification_review.v2', source: null, candidate: null, review: null, stale_reason: 'SPECIFICATION_SOURCE_NOT_REGISTERED' };
    state.position.decisions = [];
    const display = h.display(state);
    assert.equal(display.badges.Specification.label, 'Source not current');
    assert.equal(display.badges.Specification.detail, 'SPECIFICATION_SOURCE_NOT_REGISTERED');
    assert.equal(display.badges.Backlog.label, 'Accepted');
    assert.equal(display.badges.Sprint.label, 'Active');
    assert.deepEqual(Array.from(display.currentStageIds), [9]);
    assert.equal(display.phaseLabel, 'Develop & verify');
});

test('Sprint labels follow validated read kinds and effective retry status', () => {
    const h = projectLifecycleHarness(); const state = activeSprintLifecycleState();
    state.sprintStatus.data.sprint.status = 'completed'; state.sprintStatus.data.effective_status = 'active';
    assert.equal(h.display(state).badges.Sprint.label, 'Active');
    state.sprintStatus = { kind: 'error', message: 'Sprint read failed.' };
    assert.equal(h.display(state).badges.Sprint.label, 'Unavailable');
    assert.equal(h.display(state).badges.Sprint.detail, 'Sprint read failed.');
    state.sprintStatus = { kind: 'absent' };
    assert.equal(h.display(state).badges.Sprint.label, 'Not started');
});

for (const fixture of [
    { name: 'validated planned Sprint retains the existing 80 percent progress', status: 'planned', stage: 8, label: 'Sprint plan & start', width: '80%' },
    { name: 'validated active Sprint execution retains the existing 80 percent progress', status: 'active', stage: 9, label: 'Develop & verify', width: '80%' },
    { name: 'validated current Sprint review retains the existing 95 percent progress', status: 'review', stage: 11, label: 'Review & close Sprint', width: '95%' },
]) {
    test(fixture.name, async () => {
        const h = projectLifecycleHarness();
        const state = activeSprintLifecycleState();
        const data = state.sprintStatus.data;
        if (fixture.status === 'planned') {
            data.sprint.status = 'planned'; data.effective_status = 'planned';
            data.accepted_plan.status = 'planned'; data.start = null;
        } else if (fixture.status === 'review') {
            data.tasks[0].status = 'Done';
            data.story_completions = [{ story_id: 81, sprint_id: 71 }];
        }
        const validated = await h.context.validateSprintStatusProjection(data, 7);
        assert.ok(validated, 'The progress fixture must pass the shipped Sprint validator');
        state.sprintStatus = { kind: 'ready', data: validated };
        assert.equal(h.display(state).primaryStageId, fixture.stage);
        h.elements['cockpit-progress-bar'] = lifecycleElement();
        h.setState(state); h.context.renderTopCockpit();
        assert.equal(h.elements['cockpit-active-stage-label'].textContent, fixture.label);
        assert.equal(h.elements['cockpit-progress-bar'].style.width, fixture.width);
    });
}

test('ready Sprint metadata with no positive owning project remains unavailable', () => {
    const h = projectLifecycleHarness();
    for (const projectId of [undefined, null, 0]) {
        const state = activeSprintLifecycleState();
        state.project.id = projectId; state.position.project_id = projectId;
        state.sprintStatus.data.project_id = projectId;
        assert.equal(h.display(state).badges.Sprint.label, 'Unavailable');
    }
});

test('loading and unavailable reads claim no current work or artifact acceptance', () => {
    const h = projectLifecycleHarness();
    for (const [kind, label] of [['loading', 'Loading…'], ['unavailable', 'Unavailable']]) {
        const display = h.display(acceptedLifecycleState(), kind);
        assert.deepEqual(Array.from(display.currentStageIds), []); assert.equal(display.primaryStageId, null);
        assert.equal(display.phaseLabel, label); assert.equal(display.visionLabel, `Vision: ${label}`);
        for (const badge of Object.values(display.badges)) assert.equal(badge.label, label);
    }
});

test('missing workspace support produces an unavailable phase without progress inference', () => {
    const h = projectLifecycleHarness({ workspace: false });
    const display = h.display(activeSprintLifecycleState());
    assert.equal(display.phaseLabel, 'Unavailable');
    assert.equal(display.primaryStageId, null);
    assert.deepEqual(Array.from(display.currentStageIds), []);
});

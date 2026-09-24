import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const source = fs.readFileSync(path.resolve('frontend/project.js'), 'utf8');
const names = [
    'project', 'position', 'vision', 'goal', 'specification', 'repository',
    'backlogReview', 'roadmapReview', 'acceptedRoadmap', 'storyReviews',
    'sprintPlanReview', 'storyPending', 'storyDependencies', 'sprintCandidates',
    'sprintStatusResponse', 'sprintHistory',
];

function bundle() {
    return { status: 'success', data: Object.fromEntries(names.map((name) => [
        name, { status: 200, body: { data: { marker: name } } },
    ])) };
}

function harness(response) {
    const requests = [];
    const context = vm.createContext({
        AbortController, console, URLSearchParams,
        document: { addEventListener() {} },
        window: { addEventListener() {} },
        fetch: async (url, options) => {
            requests.push({ url, options });
            return { ok: true, status: 200, text: async () => JSON.stringify(response) };
        },
    });
    vm.runInContext(source, context);
    return { context, requests };
}

test('dashboard loads every projection with one GET and preserves slot order', async () => {
    const { context, requests } = harness(bundle());
    const controller = new AbortController();
    const result = await context.requestDashboard('/api/projects/7', { signal: controller.signal });
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, '/api/projects/7/dashboard');
    assert.equal(requests[0].options.signal, controller.signal);
    assert.deepEqual(Array.from(result, (item) => item.data.marker), names);
    assert.equal(result[8].kind, 'ready');
    assert.equal(result[14].kind, 'candidate');
});

test('bundle preserves absent reviews, missing Sprint, and optional Roadmap errors', async () => {
    const response = bundle();
    const absent = { status: 409, body: { detail: { error: { code: 'PLANNING_REVIEW_NOT_AVAILABLE' } } } };
    for (const name of ['backlogReview', 'roadmapReview', 'storyReviews', 'sprintPlanReview']) response.data[name] = absent;
    response.data.acceptedRoadmap = { status: 409, body: { detail: { error: { code: 'STALE_ROADMAP', message: 'Roadmap changed.' } } } };
    response.data.sprintStatusResponse = { status: 404, body: { code: 'SPRINT_NOT_FOUND' } };
    const { context } = harness(response);
    const result = await context.requestDashboard('/api/projects/7');
    for (const index of [6, 7, 9, 10]) assert.equal(JSON.stringify(result[index]), '{"data":{}}');
    assert.equal(result[8].kind, 'error');
    assert.equal(result[8].code, 'STALE_ROADMAP');
    assert.equal(result[8].message, 'Roadmap changed.');
    assert.equal(result[14].kind, 'absent');
});

test('required projection errors and unexpected review failures reject the bundle', async () => {
    for (const name of ['project', 'position', 'storyDependencies', 'backlogReview']) {
        const response = bundle();
        response.data[name] = { status: 409, body: { detail: { errors: [{ code: 'FACT_CONFLICT', message: 'Facts conflict.' }] } } };
        const { context } = harness(response);
        await assert.rejects(context.requestDashboard('/api/projects/7'), (error) => (
            error.status === 409 && error.code === 'FACT_CONFLICT' && error.message === 'Facts conflict.'
        ));
    }
});

test('internal errors stay local to optional sections but reject required sections', async () => {
    const failure = { status: 500, body: { detail: 'Internal Server Error' } };
    const response = bundle();
    response.data.acceptedRoadmap = failure;
    response.data.sprintStatusResponse = failure;
    const { context } = harness(response);
    const result = await context.requestDashboard('/api/projects/7');
    assert.equal(result[0].data.marker, 'project');
    assert.equal(result[8].kind, 'error');
    assert.equal(result[14].kind, 'error');
    assert.equal(result[8].message, 'The requested action failed.');
    assert.equal(result[14].message, 'The requested action failed.');

    for (const name of ['project', 'position', 'backlogReview']) {
        const requiredResponse = bundle();
        requiredResponse.data[name] = failure;
        const required = harness(requiredResponse);
        await assert.rejects(required.context.requestDashboard('/api/projects/7'), (error) => (
            error.status === 500 && error.message === 'The requested action failed.'
        ));
    }
});

test('missing or malformed slots cannot confirm an incomplete dashboard', async () => {
    for (const value of [undefined, null, {}, { status: '200', body: {} }, { status: 200, body: [] }]) {
        const response = bundle();
        response.data.acceptedRoadmap = value;
        const { context } = harness(response);
        await assert.rejects(context.requestDashboard('/api/projects/7'), /incomplete dashboard response/);
    }
});

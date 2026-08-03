import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

const fixture = JSON.parse(fs.readFileSync(
    path.resolve(import.meta.dirname, 'fixtures/workflow_position.json'),
    'utf8',
));
const sourcePath = path.resolve(import.meta.dirname, '../frontend/project.js');
const source = fs.readFileSync(sourcePath, 'utf8');

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

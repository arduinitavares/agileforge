import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const sourcePath = path.resolve(import.meta.dirname, '../frontend/lifecycle-workspace.js');

function api() {
    const context = vm.createContext({ AbortController, URLSearchParams, console });
    vm.runInContext(fs.readFileSync(sourcePath, 'utf8'), context);
    return vm.runInContext('AgileForgeWorkspace', context);
}

test('all thirteen stages retain logical navigation order', () => {
    assert.deepEqual(Array.from(api().stages(), (stage) => stage.id),
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]);
});

test('current work excludes optional retry and retains concurrent required work', () => {
    const position = { decisions: [
        { request_kind: 'retry_sprint', category: 'available', recommendation_kind: 'optional_reentry' },
        { request_kind: 'complete_task', category: 'available', recommendation_kind: 'required' },
        { request_kind: 'close_story', category: 'available', recommendation_kind: 'required' },
    ] };
    assert.deepEqual(Array.from(api().currentStageIds(position)), [9, 10]);
});

test('full inventory counts completion independently of task availability', () => {
    const result = api().taskCounts([
        { task_id: 14, status: 'Done' },
        { task_id: 15, status: 'To Do' },
        { task_id: 16, status: 'To Do' },
    ]);
    assert.equal(result.total, 3);
    assert.equal(result.done, 1);
    assert.equal(result.remaining, 2);
});

test('reconciliation retains the inspected task after another task completes', () => {
    const view = { ...api().createView(), stageId: 9, sprintId: 5, taskId: 14, tab: 'checks', filter: 'all' };
    const status = { sprint: { sprint_id: 5 }, current_retry: null, tasks: [
        { task_id: 14, status: 'Done' }, { task_id: 15, status: 'Done' }, { task_id: 16, status: 'Done' },
    ] };
    const result = api().reconcileView(view, status);
    assert.equal(result.taskId, 14);
    assert.equal(result.tab, 'checks');
    assert.equal(result.stageId, 9);
    assert.equal(result.taskAvailability, 'available');
});

test('task rows pair only exact advertised task actions and retain queued inventory rows', () => {
    const fingerprint = 'sha256:task-14';
    const position = { decisions: [{
        node_id: 'execution.task.complete', instance_key: 'task:14', request_kind: 'complete_task',
        category: 'available', recommendation_kind: 'required', reason_code: 'NEXT_TASK_READY',
        fact_references: [{ fact_type: 'task', fact_id: '14', fingerprint }],
    }] };
    const actions = [{ node_id: 'execution.task.complete', instance_key: 'task:14', request_kind: 'complete_task', endpoint: 'sprint/task/complete' }];
    const rows = api().taskRows({ tasks: [
        { task_id: 14, status: 'To Do', instance_key: 'task:14', fact_fingerprint: fingerprint, dependencies_satisfied: true },
        { task_id: 15, status: 'To Do', instance_key: 'task:15', dependencies_satisfied: true },
        { task_id: 16, status: 'Done', instance_key: 'task:16' },
    ] }, position, actions);
    assert.equal(rows[0].availability, 'Ready');
    assert.equal(rows[0].action.endpoint, 'sprint/task/complete');
    assert.equal(rows[1].availability, 'Queued / not current');
    assert.equal(rows[2].availability, 'Done');
});

test('controller ignores superseded and mismatched task detail responses', async () => {
    const deferred = [];
    const controller = api().createController({ requestJson: (url) => new Promise((resolve, reject) => deferred.push({ url, resolve, reject })) });
    const fourteen = controller.select({ projectId: 7, sprintId: 31, taskId: 14, scopeKey: 'sprint:31' });
    const fifteen = controller.select({ projectId: 7, sprintId: 31, taskId: 15, scopeKey: 'sprint:31' });
    deferred[2].resolve({ data: { project_id: 7, task: { task_id: 15, sprint_id: 31 }, current_retry: null } });
    deferred[3].resolve({ data: { project_id: 7, task: { task_id: 15, sprint_id: 31 }, current_retry: null, items: [] } });
    await fifteen;
    deferred[0].resolve({ data: { project_id: 7, task: { task_id: 14, sprint_id: 31 }, current_retry: null } });
    deferred[1].resolve({ data: { project_id: 7, task: { task_id: 14, sprint_id: 31 }, current_retry: null, items: [] } });
    await fourteen;
    assert.equal(controller.snapshot().selection.taskId, 15);
    assert.equal(controller.snapshot().data.task.task_id, 15);
});

test('controller retains confirmed detail as stale after a refresh failure', async () => {
    let fail = false;
    const controller = api().createController({ requestJson: async () => {
        if (fail) throw new Error('network unavailable');
        return { data: { project_id: 7, task: { task_id: 14, sprint_id: 31 }, current_retry: null, items: [] } };
    } });
    await controller.select({ projectId: 7, sprintId: 31, taskId: 14, scopeKey: 'sprint:31' });
    fail = true;
    await controller.refresh();
    assert.equal(controller.snapshot().kind, 'stale');
    assert.equal(controller.snapshot().data.task.task_id, 14);
});

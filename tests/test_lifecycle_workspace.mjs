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
        category: 'available', recommendation_kind: 'required', reason_code: 'NEXT_TASK_READY', decision_fingerprint: 'decision-14',
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

test('controller clears old confirmed detail when a different Task selection fails', async () => {
    let request = 0;
    const controller = api().createController({ requestJson: async () => {
        request += 1;
        if (request <= 2) {
            return { data: { project_id: 7, task: { task_id: 14, sprint_id: 31 }, current_retry: null, items: [] } };
        }
        throw new Error('Task 15 is unavailable');
    } });
    await controller.select({ projectId: 7, sprintId: 31, taskId: 14, scopeKey: 'sprint:31' });
    await controller.select({ projectId: 7, sprintId: 31, taskId: 15, scopeKey: 'sprint:31' });
    const snapshot = controller.snapshot();
    assert.equal(snapshot.kind, 'error');
    assert.equal(snapshot.selection.taskId, 15);
    assert.equal(snapshot.data, null);
});

test('controller settles malformed or mismatched current responses as an error', async () => {
    const controller = api().createController({ requestJson: async () => ({
        data: { project_id: 7, task: { task_id: 14, sprint_id: 31 }, current_retry: { retry_attempt_id: 8 } },
    }) });
    await controller.select({ projectId: 7, sprintId: 31, taskId: 14, scopeKey: 'sprint:31' });
    const snapshot = controller.snapshot();
    assert.equal(snapshot.kind, 'error');
    assert.equal(snapshot.data, null);
    assert.match(snapshot.error, /identity/i);
});

test('map renders graph authority independently from viewing and exposes return to current work', () => {
    const markup = api().mapMarkup({
        position: { decisions: [{ request_kind: 'complete_task', category: 'available', recommendation_kind: 'required' }] },
        view: { ...api().createView(), stageId: 7 },
        lastConfirmedAt: '2026-09-14T10:30:00Z',
    });
    assert.match(markup, /data-workspace-stage="7"[^>]*Viewing/);
    assert.match(markup, /data-workspace-stage="9"[^>]*aria-current="step"/);
    assert.match(markup, /Return to current work/);
    assert.match(markup, /Last confirmed 2026-09-14T10:30:00Z; manual refresh required/);
});


test('controller clears old detail when the current Task becomes unavailable', async () => {
    let request = 0;
    const controller = api().createController({ requestJson: async () => {
        request += 1;
        if (request <= 2) return { data: { project_id: 7, task: { task_id: 14, sprint_id: 31 }, current_retry: null, items: [] } };
        throw Object.assign(new Error('Task removed from Sprint scope'), { status: 404 });
    } });
    await controller.select({ projectId: 7, sprintId: 31, taskId: 14, scopeKey: 'sprint:31' });
    await controller.select({ projectId: 7, sprintId: 31, taskId: 15, scopeKey: 'sprint:31' });
    assert.equal(controller.snapshot().kind, 'unavailable');
    assert.equal(controller.snapshot().selection.taskId, 15);
    assert.equal(controller.snapshot().data, null);
});

test('map mount preserves logical keyboard navigation and returns to graph current work', () => {
    const listeners = new Map();
    const buttons = Array.from({ length: 13 }, (_, index) => ({
        dataset: { workspaceStage: String(index + 1) },
        focused: false,
        addEventListener(kind, listener) { listeners.set(`${index}:${kind}`, listener); },
        focus() { this.focused = true; },
    }));
    const returnListeners = new Map();
    const host = {
        innerHTML: '',
        querySelectorAll() { return buttons; },
        querySelector() { return { addEventListener(kind, listener) { returnListeners.set(kind, listener); } }; },
    };
    const selected = [];
    const returned = [];
    api().mount(host, {
        position: { decisions: [{ request_kind: 'complete_task', category: 'available', recommendation_kind: 'required' }] },
        view: { ...api().createView(), stageId: 7 },
        onStageSelect: (stageId) => selected.push(stageId),
        onReturnToCurrent: (stageId) => returned.push(stageId),
    });
    let prevented = false;
    listeners.get('6:keydown')({ key: 'ArrowRight', preventDefault() { prevented = true; } });
    returnListeners.get('click')();
    assert.equal(prevented, true);
    assert.equal(buttons[7].focused, true);
    assert.deepEqual(selected, [8]);
    assert.deepEqual(returned, [9]);
});


test('locked or fingerprintless Task actions never become executable', () => {
    const task = { task_id: 14, status: 'To Do', instance_key: 'task:14', fact_fingerprint: 'sha256:task-14', dependencies_satisfied: true };
    const position = { decisions: [{ node_id: 'execution.task.complete', instance_key: 'task:14', request_kind: 'complete_task', category: 'available', recommendation_kind: 'required', reason_code: 'NEXT_TASK_READY', fact_references: [{ fact_type: 'task', fact_id: '14', fingerprint: task.fact_fingerprint }] }] };
    const locked = [{ node_id: 'execution.task.complete', instance_key: 'task:14', request_kind: 'complete_task', endpoint: 'sprint/task/complete', availability: 'locked' }];
    assert.equal(api().taskRows({ tasks: [task] }, position, locked)[0].action, null);
    position.decisions[0].decision_fingerprint = 'decision-14';
    assert.equal(api().taskRows({ tasks: [task] }, position, locked)[0].action, null);
});

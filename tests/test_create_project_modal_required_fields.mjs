import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const indexHtmlPath = path.resolve(import.meta.dirname, '../frontend/index.html');
const appSourcePath = path.resolve(import.meta.dirname, '../frontend/app.js');
const indexHtmlSource = fs.readFileSync(indexHtmlPath, 'utf8');
const appSource = fs.readFileSync(appSourcePath, 'utf8');

function createControl(id, value = '') {
    return {
        id,
        value,
        disabled: false,
        textContent: '',
        classList: { add() {}, remove() {}, toggle() {} },
    };
}

test('create modal contains only the human project fields', () => {
    const modal = indexHtmlSource.match(
        /<div id="create-project-modal"[\s\S]*?<script src="\/dashboard\/app\.js"><\/script>/,
    )?.[0];

    assert.ok(modal, 'create modal should exist');
    assert.match(modal, /for="modal-project-name"[^>]*>Project Name/);
    assert.match(modal, /for="modal-project-description"[^>]*>Description/);
    assert.match(modal, /for="modal-repository-path"[^>]*>Repository Path/);
    assert.doesNotMatch(modal, /Origin|setup type|greenfield|brownfield/i);

    const businessControls = [...modal.matchAll(
        /<(?:input|textarea|select)\b[^>]*id="(modal-[^"]+)"[^>]*>/g,
    )].map((match) => match[1]);
    assert.deepEqual(businessControls, [
        'modal-project-name',
        'modal-project-description',
        'modal-repository-path',
    ]);
});

test('only Project Name is required', () => {
    const controlTags = [...indexHtmlSource.matchAll(
        /<(?:input|textarea)\b[^>]*id="(modal-(?:project-name|project-description|repository-path))"[^>]*>/g,
    )];
    assert.equal(controlTags.length, 3);
    const requiredIds = controlTags
        .filter((match) => /\brequired\b/.test(match[0]))
        .map((match) => match[1]);
    assert.deepEqual(requiredIds, ['modal-project-name']);
});

test('successful create posts semantic fields and opens the new Project page', async () => {
    const controls = new Map([
        ['modal-project-name', createControl('modal-project-name', '  Household Ledger  ')],
        ['modal-project-description', createControl('modal-project-description', '  Reconcile household movements.  ')],
        ['modal-repository-path', createControl('modal-repository-path', '  /tmp/household-ledger  ')],
        ['btn-submit-project', createControl('btn-submit-project')],
        ['create-project-error', createControl('create-project-error')],
    ]);
    const requests = [];
    const location = { href: '' };
    const context = vm.createContext({
        console,
        crypto: { randomUUID: () => 'create-uuid' },
        document: {
            createElement: () => createControl('generated'),
            getElementById: (id) => controls.get(id) ?? null,
        },
        fetch: async (url, options) => {
            requests.push({ url, options });
            return {
                ok: true,
                status: 201,
                json: async () => ({ data: { output: { project_id: 73 } } }),
            };
        },
        window: {
            addEventListener() {},
            location,
        },
    });
    vm.runInContext(appSource, context, { filename: appSourcePath });

    await context.submitNewProject();

    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, '/api/projects');
    assert.equal(requests[0].options.method, 'POST');
    assert.deepEqual(JSON.parse(requests[0].options.body), {
        name: 'Household Ledger',
        description: 'Reconcile household movements.',
        repository_path: '/tmp/household-ledger',
        idempotency_key: 'dashboard-create-uuid',
        actor: 'dashboard-ui',
    });
    assert.equal(location.href, '/dashboard/project.html?id=73');
});

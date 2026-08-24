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
        inert: false,
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
    assert.doesNotMatch(modal, /Origin|setup type/i);

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

test('create modal names its description and isolates the background', () => {
    assert.match(
        indexHtmlSource,
        /id="create-project-modal"[^>]*aria-labelledby="create-project-title"[^>]*aria-describedby="create-project-description"/,
    );
    assert.match(indexHtmlSource, /id="create-project-description"/);
    assert.match(indexHtmlSource, /id="dashboard-content"/);
});

test('create modal traps focus, closes on Escape, and restores its opener', () => {
    const controls = new Map();
    const listeners = new Map();
    const document = {
        activeElement: null,
        createElement: () => createControl('generated'),
        getElementById: (id) => controls.get(id) ?? null,
    };
    const focusableIds = [
        'close-create-project',
        'modal-project-name',
        'modal-project-description',
        'modal-repository-path',
        'cancel-create-project',
        'btn-submit-project',
    ];
    for (const id of focusableIds) {
        const control = createControl(id);
        control.focus = () => { document.activeElement = control; };
        controls.set(id, control);
    }
    const opener = createControl('open-create-project');
    opener.focus = () => { document.activeElement = opener; };
    controls.set(opener.id, opener);
    const modal = createControl('create-project-modal');
    modal.classList = {
        values: new Set(['hidden']),
        add(value) { this.values.add(value); },
        remove(value) { this.values.delete(value); },
        toggle() {},
        contains(value) { return this.values.has(value); },
    };
    controls.set(modal.id, modal);
    controls.set('dashboard-content', createControl('dashboard-content'));
    controls.set('create-project-error', createControl('create-project-error'));
    controls.set('create-project-form', {
        querySelectorAll: () => focusableIds.map((id) => controls.get(id)),
    });
    document.activeElement = opener;
    const context = vm.createContext({
        console,
        crypto: { randomUUID: () => 'modal-uuid' },
        document,
        fetch: async () => ({ ok: true, json: async () => ({ data: { items: [] } }) }),
        window: {
            addEventListener(type, callback) { listeners.set(type, callback); },
            location: { href: '' },
        },
    });
    vm.runInContext(appSource, context, { filename: appSourcePath });

    context.openCreateProjectModal();
    assert.equal(controls.get('dashboard-content').inert, true);
    assert.equal(document.activeElement.id, 'modal-project-name');

    controls.get('btn-submit-project').focus();
    let tabPrevented = false;
    context.handleCreateModalKeydown({
        key: 'Tab',
        shiftKey: false,
        preventDefault() { tabPrevented = true; },
    });
    assert.equal(tabPrevented, true);
    assert.equal(document.activeElement.id, 'close-create-project');

    context.handleCreateModalKeydown({ key: 'Escape', preventDefault() {} });
    assert.equal(modal.classList.contains('hidden'), true);
    assert.equal(controls.get('dashboard-content').inert, false);
    assert.equal(document.activeElement, opener);
});

test('create flow renders FastAPI validation locations and messages', async () => {
    const context = vm.createContext({
        console,
        document: { createElement: () => createControl('generated'), getElementById: () => null },
        fetch: async () => ({}),
        window: { addEventListener() {}, location: { href: '' } },
    });
    vm.runInContext(appSource, context, { filename: appSourcePath });
    const response = {
        ok: false,
        json: async () => ({
            detail: [
                { loc: ['body', 'repository_path'], msg: 'Input should be a valid path' },
                { loc: ['body', 'name'], msg: 'Field required' },
            ],
        }),
    };

    await assert.rejects(
        context.readResponse(response, 'Project creation failed.'),
        /Repository Path: Input should be a valid path\. Name: Field required\./,
    );
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

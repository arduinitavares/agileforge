import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const sourcePath = path.resolve(import.meta.dirname, '../frontend/project.js');
const source = fs.readFileSync(sourcePath, 'utf8');

test('unsupported Specification capture locks the form and submission binding', () => {
    const context = loadFrontend();
    const action = {
        node_id: 'specification.source.register',
        request_kind: 'register_specification_source',
        endpoint: 'specifications/source',
        availability: 'locked',
        reason_code: 'REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE',
    };
    const markup = context.specificationSourceRegistrationMarkup([action]);
    assert.doesNotMatch(markup, /<form/);
    assert.match(markup, /role="alert"/);
    assert.match(markup, /Specification source capture is unavailable on this runtime or filesystem\./);
    assert.doesNotMatch(markup, /Native Windows/);
    const binding = context.captureSpecificationSourceRegistrationBinding({
        actions: [action],
        position: { decisions: [{
            node_id: action.node_id,
            request_kind: action.request_kind,
            category: 'available',
            decision_fingerprint: 'current-decision',
        }] },
    });
    assert.equal(binding, null);
});

test('Specification source registration shows bounded size guidance and local status', () => {
    const context = loadFrontend();
    const markup = context.specificationSourceRegistrationMarkup([{
        node_id: 'specification.source.register',
        request_kind: 'register_specification_source',
        endpoint: 'specifications/source',
    }]);

    assert.match(markup, /data-specification-source-status="true"/);
    assert.match(markup, /96 KiB per document/);
    assert.match(markup, /192 KiB for the complete package/);
    assert.match(markup, /Check selected package/);
    assert.match(markup, /No provider run is performed/);
    assert.match(markup, /aria-atomic="true"/);
    assert.match(
        context.specificationSourcePreviewMessage({
            documents: [{ relative_path: 'specification.md', byte_length: 63682 }],
            total_bytes: 63682,
            document_limit_bytes: 98304,
            package_limit_bytes: 196608,
        }),
        /specification\.md: 63682 bytes.*No provider run was performed/,
    );

    const attributes = new Map();
    const status = {
        textContent: '',
        classList: { toggle() {} },
        setAttribute(name, value) { attributes.set(name, value); },
    };
    context.setSpecificationSourceRegistrationStatus(
        { querySelector() { return status; } },
        'The selected source is too large.',
        true,
    );
    assert.equal(attributes.get('role'), 'alert');
    assert.equal(attributes.get('aria-live'), 'assertive');
    assert.equal(attributes.get('aria-atomic'), 'true');
});

function loadFrontend({ fetchImpl, elements = {}, querySelectorImpl = null } = {}) {
    const documentListeners = {};
    const context = vm.createContext({
        console,
        crypto: { randomUUID: () => 'vision-uuid' },
        document: {
            createElement() {
                return {
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
                };
            },
            addEventListener(type, listener) { documentListeners[type] = listener; },
            getElementById(id) { return elements[id] ?? null; },
            querySelector(selector) {
                if (querySelectorImpl) return querySelectorImpl(selector);
                return null;
            },
        },
        fetch: fetchImpl ?? (async () => ({ ok: true, text: async () => '{}' })),
        URLSearchParams,
        window: { addEventListener() {}, location: { href: '' } },
    });
    context.__documentListeners = documentListeners;
    vm.runInContext(source, context, { filename: sourcePath });
    return context;
}

const respondAction = {
    request_kind: 'record_vision_interview_turn',
    endpoint: 'vision/respond',
};
const bootstrapAction = {
    request_kind: 'generate_vision_bootstrap',
    endpoint: 'vision/bootstrap',
};
const reviewAction = {
    request_kind: 'decide_vision_review',
    endpoint: 'vision/review',
};

function reviewMaterial({ complete = false } = {}) {
    return {
        statement: 'A <strong>durable</strong> Vision.',
        components: [
            {
                name: 'project_name',
                value: 'AgileForge <img src=x onerror=alert(1)>',
                source_kinds: ['evidence', 'human', 'inference'],
            },
            {
                name: 'competitors',
                value: complete ? 'Mutable chat history' : null,
                source_kinds: complete ? ['evidence'] : [],
            },
        ],
        assumptions: [
            {
                text: 'Teams can adopt <script>unsafe()</script>.',
                affected_components: ['key_benefit'],
            },
        ],
        conflicts: [
            {
                text: 'The <em>alternative</em> is disputed.',
                status: complete ? 'resolved' : 'unresolved',
                affected_components: ['competitors'],
                resolution: complete ? 'Use mutable chat history.' : null,
            },
        ],
        questions: complete ? [] : [
            {
                question_id: 'question-secret-id',
                text: 'Which <svg onload=alert(1)> alternative is used?',
                affected_components: ['competitors'],
            },
        ],
    };
}

test('initial Vision state offers one generation action without fallback input', () => {
    let fetchCalls = 0;
    const context = loadFrontend({ fetchImpl: async () => {
        fetchCalls += 1;
        return { ok: true, text: async () => '{}' };
    } });
    const markup = context.visionPanelMarkup(
        {
            bootstrap_available: true,
            current: null,
            draft: null,
            transcript: [],
            candidate: null,
            review: null,
        },
        [bootstrapAction],
        {
            project: {
                name: 'Project <script>unsafe()</script>',
                description: 'Durable & reviewable context',
            },
            repository: null,
        },
    );

    assert.equal((markup.match(/Generate Vision draft/g) ?? []).length, 1);
    assert.match(markup, /data-direct-action="generate_vision_bootstrap"/);
    assert.match(markup, /Project &lt;script&gt;unsafe\(\)&lt;\/script&gt;/);
    assert.match(markup, /Durable &amp; reviewable context/);
    assert.match(markup, /Not attached/);
    assert.doesNotMatch(markup, /<textarea\b/);
    assert.doesNotMatch(markup, /Who should benefit|What problem should|What principles should/);
    assert.equal(fetchCalls, 0);
});

test('locked Vision generation renders disabled and cannot execute from a stale control', async () => {
    let fetchCalls = 0;
    const cockpitButton = { disabled: false, onclick: null };
    const cockpitLabel = { textContent: '' };
    const cockpitChip = { textContent: '' };
    const cockpitDescription = { textContent: '' };
    const context = loadFrontend({
        fetchImpl: async () => {
            fetchCalls += 1;
            return { ok: true, text: async () => '{}' };
        },
        elements: {
            'cockpit-primary-action-btn': cockpitButton,
            'cockpit-primary-action-label': cockpitLabel,
            'cockpit-action-stage-chip': cockpitChip,
            'cockpit-action-description': cockpitDescription,
        },
    });
    const lockedAction = {
        ...bootstrapAction,
        availability: 'locked',
        reason_code: 'REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE',
    };
    const markup = context.visionPanelMarkup(
        {
            bootstrap_available: false,
            current: null,
            draft: null,
            transcript: [],
            candidate: null,
            review: null,
        },
        [lockedAction],
    );

    assert.match(markup, /data-direct-action="generate_vision_bootstrap"/);
    assert.match(markup, /data-action-availability="locked"/);
    assert.match(markup, /\bdisabled\b/);
    assert.match(markup, /Vision generation unavailable/);

    vm.runInContext(`
        selectedProjectId = 7;
        lifecycleState.actions = [${JSON.stringify(lockedAction)}];
    `, context);
    context.renderTopCockpit();

    assert.equal(cockpitButton.disabled, true);
    assert.equal(cockpitButton.onclick, null);
    assert.equal(cockpitLabel.textContent, 'Action Unavailable');
    assert.equal(cockpitChip.textContent, 'Locked');
    assert.equal(cockpitDescription.textContent, 'Vision generation unavailable.');
    assert.equal(
        context.stageReason(
            { request_kind: 'generate_vision_bootstrap' },
            [lockedAction],
        ),
        'Vision generation unavailable.',
    );
    const staleButton = { disabled: false };

    const submitted = await context.runDirectAction(
        'generate_vision_bootstrap',
        staleButton,
    );

    assert.equal(submitted, false);
    assert.equal(staleButton.disabled, true);
    assert.equal(fetchCalls, 0);
});

test('stale initial Vision lineage keeps the graph-advertised generation action', () => {
    const context = loadFrontend();
    const markup = context.visionPanelMarkup(
        {
            bootstrap_available: false,
            current: null,
            draft: reviewMaterial(),
            transcript: [{ turn_number: 1, user_text: 'Retained stale answer.' }],
            candidate: null,
            review: null,
        },
        [bootstrapAction],
    );

    assert.equal((markup.match(/Generate Vision draft/g) ?? []).length, 1);
    assert.match(markup, /data-direct-action="generate_vision_bootstrap"/);
    assert.doesNotMatch(markup, /<textarea\b/);
    assert.doesNotMatch(markup, /Who should benefit|What problem should|What principles should/);
    assert.doesNotMatch(markup, /Vision is waiting/);
});

test('stale accepted-Vision revision lineage keeps the graph-advertised generation action', () => {
    const context = loadFrontend();
    const markup = context.visionPanelMarkup(
        {
            bootstrap_available: false,
            current: { statement: 'Accepted <Vision>.' },
            draft: null,
            transcript: [{ turn_number: 1, user_text: 'Retained stale revision answer.' }],
            candidate: null,
            review: { state: 'accepted', rationale: 'Reviewed.' },
        },
        [bootstrapAction],
    );

    assert.equal((markup.match(/Generate Vision draft/g) ?? []).length, 1);
    assert.match(markup, /data-direct-action="generate_vision_bootstrap"/);
    assert.doesNotMatch(markup, /<textarea\b/);
    assert.doesNotMatch(markup, /Who should benefit|What problem should|What principles should/);
    assert.doesNotMatch(markup, /Vision is waiting/);
});

test('incomplete Vision draft shows safe provenance, questions, and one response field', () => {
    const context = loadFrontend();
    assert.equal(typeof context.visionPanelMarkup, 'function');
    const markup = context.visionPanelMarkup(
        {
            bootstrap_available: false,
            current: null,
            draft: reviewMaterial(),
            transcript: [{ user_text: 'Product teams need durable review.' }],
            candidate: null,
            review: null,
        },
        [respondAction],
    );

    assert.match(markup, /A &lt;strong&gt;durable&lt;\/strong&gt; Vision\./);
    assert.match(markup, /AgileForge &lt;img src=x onerror=alert\(1\)&gt;/);
    assert.match(markup, /Project evidence/);
    assert.match(markup, /Human input/);
    assert.match(markup, /Inferred/);
    assert.match(markup, /Teams can adopt &lt;script&gt;unsafe\(\)&lt;\/script&gt;\./);
    assert.match(markup, /The &lt;em&gt;alternative&lt;\/em&gt; is disputed\./);
    assert.match(markup, /Which &lt;svg onload=alert\(1\)&gt; alternative is used\?/);
    assert.match(markup, /Product teams need durable review\./);
    assert.equal((markup.match(/<textarea\b/g) ?? []).length, 1);
    assert.match(markup, /id="vision-response"/);
    assert.match(markup, /id="vision-response-status"/);
    assert.doesNotMatch(markup, /question-secret-id|JSON|fingerprint|graph|model|evidence_id/i);
    assert.doesNotMatch(markup, /<script>|<img|<svg|<em>/i);
});

test('complete Vision candidate shows provenance and review controls without internals', () => {
    const context = loadFrontend();
    const candidate = {
        ...reviewMaterial({ complete: true }),
        review_fingerprint: 'sha256:never-render-this',
    };
    const markup = context.visionPanelMarkup(
        {
            bootstrap_available: false,
            current: null,
            transcript: [{ turn_number: 1, user_text: 'Earlier answer.' }],
            draft: null,
            candidate,
            review: { state: 'pending', rationale: null },
        },
        [reviewAction],
    );

    assert.match(markup, /A &lt;strong&gt;durable&lt;\/strong&gt; Vision\./);
    assert.match(markup, /Mutable chat history/);
    assert.match(markup, /Project evidence/);
    assert.doesNotMatch(markup, /<textarea\b/);
    assert.doesNotMatch(markup, /sha256:never-render-this|question-secret-id|fingerprint/i);
    assert.match(markup, /data-review-scope="vision"[^>]*data-review-decision="accepted"/);
    assert.match(markup, /data-review-scope="vision"[^>]*data-review-decision="feedback"/);
    assert.match(markup, /data-review-scope="vision"[^>]*data-review-decision="rejected"/);
});

test('Vision feedback shows rationale with the revised draft context', () => {
    const context = loadFrontend();
    const revised = reviewMaterial();
    revised.statement = 'A revised <Vision> statement.';
    const markup = context.visionPanelMarkup(
        {
            bootstrap_available: false,
            current: null,
            draft: revised,
            transcript: [],
            candidate: {
                ...reviewMaterial({ complete: true }),
                review_fingerprint: 'sha256:hidden',
            },
            review: {
                state: 'rejected',
                rationale: 'Narrow the <target> audience.',
            },
        },
        [respondAction],
    );

    assert.match(markup, /Review response:/);
    assert.match(markup, /Narrow the &lt;target&gt; audience\./);
    assert.match(markup, /A revised &lt;Vision&gt; statement\./);
    assert.equal((markup.match(/<textarea\b/g) ?? []).length, 1);
});

test('accepted Vision retains revision and an open revision offers generation', () => {
    const context = loadFrontend();
    const accepted = {
        bootstrap_available: false,
        current: { statement: 'Accepted <Vision>.' },
        draft: null,
        transcript: [],
        candidate: null,
        review: { state: 'accepted', rationale: 'Reviewed.' },
    };
    const acceptedMarkup = context.visionPanelMarkup(
        accepted,
        [{ request_kind: 'begin_vision_revision', endpoint: 'vision/revision' }],
    );
    const revisionMarkup = context.visionPanelMarkup(
        { ...accepted, bootstrap_available: true },
        [bootstrapAction],
    );

    assert.match(acceptedMarkup, /Accepted &lt;Vision&gt;\./);
    assert.match(acceptedMarkup, /data-vision-revision="true"/);
    assert.match(revisionMarkup, /Accepted &lt;Vision&gt;\./);
    assert.match(revisionMarkup, /Generate Vision draft/);
    assert.doesNotMatch(revisionMarkup, /<textarea\b|data-vision-revision/);
});

test('Vision response submits ordinary text without question identity', async () => {
    const requests = [];
    const submit = { disabled: false };
    const context = loadFrontend({
        elements: {
            'vision-response': { value: 'Clarify the target team.' },
        },
        fetchImpl: async (...args) => {
            requests.push(args);
            return {
                ok: false,
                text: async () => JSON.stringify({ message: 'Expected test stop.' }),
            };
        },
    });
    vm.runInContext(`
        selectedProjectId = 9;
        lifecycleState.actions = [{
            request_kind: 'record_vision_interview_turn',
            endpoint: 'vision/respond',
        }];
    `, context);
    context.installInteractions();
    const form = {
        dataset: { interviewScope: 'vision' },
        querySelector() { return submit; },
    };

    await context.__documentListeners.submit({
        target: form,
        preventDefault() {},
    });

    assert.equal(requests.length, 1);
    assert.equal(requests[0][0], '/api/projects/9/vision/respond');
    const body = JSON.parse(requests[0][1].body);
    assert.equal(body.text, 'Clarify the target team.');
    assert.deepEqual(
        Object.keys(body).sort(),
        ['actor', 'idempotency_key', 'text'],
    );
    assert.equal(submit.disabled, false);
});

test('Vision response shows pending state and ignores duplicate submission', async () => {
    const pendingResponses = [];
    const label = { textContent: 'Send response' };
    const submit = {
        disabled: false,
        querySelector() { return label; },
        setAttribute() {},
        removeAttribute() {},
    };
    const textarea = {
        disabled: false,
        value: 'Clarify the target team.',
    };
    const context = loadFrontend({
        elements: { 'vision-response': textarea },
        fetchImpl: (...args) => new Promise((resolve) => {
            pendingResponses.push({ args, resolve });
        }),
    });
    vm.runInContext(`
        selectedProjectId = 9;
        lifecycleState.actions = [{
            request_kind: 'record_vision_interview_turn',
            endpoint: 'vision/respond',
        }];
    `, context);
    context.installInteractions();
    const form = {
        dataset: { interviewScope: 'vision' },
        querySelector() { return submit; },
    };
    const event = {
        target: form,
        preventDefault() {},
    };

    const first = context.__documentListeners.submit(event);
    const second = context.__documentListeners.submit(event);

    assert.equal(form.dataset.submitting, 'true');
    assert.equal(submit.disabled, true);
    assert.equal(textarea.disabled, true);
    assert.equal(label.textContent, 'Sending...');
    assert.equal(pendingResponses.length, 1);
    pendingResponses[0].resolve({
        ok: false,
        text: async () => JSON.stringify({ message: 'Expected test stop.' }),
    });
    await Promise.all([first, second]);
    assert.equal(form.dataset.submitting, undefined);
    assert.equal(submit.disabled, false);
    assert.equal(textarea.disabled, false);
    assert.equal(label.textContent, 'Send response');
});

test('Vision response shows an inline failure and preserves operator text', async () => {
    const submit = {
        disabled: false,
        querySelector() { return null; },
        setAttribute() {},
        removeAttribute() {},
    };
    const textarea = {
        disabled: false,
        value: 'Keep this response available for retry.',
    };
    const status = { hidden: true, textContent: '' };
    const context = loadFrontend({
        elements: {
            'vision-response': textarea,
            'vision-response-status': status,
        },
        fetchImpl: async () => ({
            ok: false,
            text: async () => JSON.stringify({
                message: 'The requested node is not currently available.',
            }),
        }),
    });
    vm.runInContext(`
        selectedProjectId = 9;
        lifecycleState.actions = [{
            request_kind: 'record_vision_interview_turn',
            endpoint: 'vision/respond',
        }];
    `, context);
    context.installInteractions();
    const form = {
        dataset: { interviewScope: 'vision' },
        querySelector() { return submit; },
    };

    await context.__documentListeners.submit({
        target: form,
        preventDefault() {},
    });

    assert.equal(status.hidden, false);
    assert.equal(
        status.textContent,
        'Response was not sent. The requested node is not currently available.',
    );
    assert.equal(textarea.value, 'Keep this response available for retry.');
    assert.equal(textarea.disabled, false);
    assert.equal(submit.disabled, false);
});

test('Vision generation disables immediately and ignores a second submission', async () => {
    const pendingResponses = [];
    const context = loadFrontend({
        fetchImpl: (...args) => new Promise((resolve) => {
            pendingResponses.push({ args, resolve });
        }),
    });
    vm.runInContext(`
        selectedProjectId = 7;
        lifecycleState.actions = [{
            request_kind: 'generate_vision_bootstrap',
            endpoint: 'vision/bootstrap',
        }];
    `, context);
    const button = { disabled: false };

    const first = context.runDirectAction('generate_vision_bootstrap', button);
    const second = context.runDirectAction('generate_vision_bootstrap', button);

    assert.equal(button.disabled, true);
    assert.equal(pendingResponses.length, 1);
    assert.equal(pendingResponses[0].args[0], '/api/projects/7/vision/bootstrap');
    pendingResponses[0].resolve({
        ok: false,
        text: async () => JSON.stringify({ message: 'Expected test stop.' }),
    });
    await Promise.all([first, second]);
    assert.equal(button.disabled, false);
});

test('Cockpit primary action disables immediately and ignores repeat clicks during Vision generation', async () => {
    const pendingResponses = [];
    const directButton = {
        disabled: false,
        dataset: { directAction: 'generate_vision_bootstrap' },
        closest(sel) { return sel === 'button' ? this : null; },
        click() {
            if (!this.disabled) {
                context.__documentListeners.click({
                    target: this,
                });
            }
        },
    };
    const cockpitAttrs = new Map();
    const cockpitButton = {
        disabled: false,
        onclick: null,
        setAttribute(name, value) { cockpitAttrs.set(name, value); },
        removeAttribute(name) { cockpitAttrs.delete(name); },
        getAttribute(name) { return cockpitAttrs.get(name) ?? null; },
        click() {
            if (!this.disabled && this.onclick) {
                this.onclick();
            }
        },
    };
    const cockpitLabel = { textContent: 'Execute Stage Action' };
    const cockpitChip = { textContent: 'Available' };
    const cockpitDescription = { textContent: 'Action available for current state.' };

    let currentDirectButton = directButton;
    const context = loadFrontend({
        fetchImpl: (...args) => new Promise((resolve) => {
            pendingResponses.push({ args, resolve });
        }),
        elements: {
            'cockpit-primary-action-btn': cockpitButton,
            'cockpit-primary-action-label': cockpitLabel,
            'cockpit-action-stage-chip': cockpitChip,
            'cockpit-action-description': cockpitDescription,
        },
        querySelectorImpl: (selector) => {
            if (selector === 'button[data-direct-action="generate_vision_bootstrap"]') {
                return currentDirectButton;
            }
            return null;
        },
    });

    const bootstrapAction = {
        request_kind: 'generate_vision_bootstrap',
        endpoint: 'vision/bootstrap',
        availability: 'available',
    };

    vm.runInContext(`
        selectedProjectId = 7;
        lifecycleState.actions = [${JSON.stringify(bootstrapAction)}];
        lifecycleState.position = {};
    `, context);
    context.installInteractions();
    context.renderTopCockpit();

    assert.equal(cockpitButton.disabled, false);
    assert.equal(cockpitLabel.textContent, 'Execute Stage Action');
    assert.equal(cockpitChip.textContent, 'Available');

    // Click the cockpit primary action button
    cockpitButton.click();

    // Assert that the cockpit button immediately entered busy/disabled state
    assert.equal(cockpitButton.disabled, true);
    assert.equal(cockpitButton.getAttribute('aria-busy'), 'true');
    assert.equal(cockpitLabel.textContent, 'Executing...');
    assert.equal(cockpitChip.textContent, 'Executing');
    assert.equal(pendingResponses.length, 1);

    // Second click on the cockpit button while in flight must be dropped
    cockpitButton.click();
    assert.equal(pendingResponses.length, 1);

    // Complete the in-flight request
    pendingResponses[0].resolve({
        ok: false,
        text: async () => JSON.stringify({ message: 'Expected test stop.' }),
    });

    // Wait for the mutation to finish
    await new Promise((resolve) => setTimeout(resolve, 20));

    // Assert that the cockpit button is unlocked and state is restored
    assert.equal(cockpitButton.disabled, false);
    assert.equal(cockpitButton.getAttribute('aria-busy'), null);
    assert.equal(cockpitChip.textContent, 'Available');
    assert.equal(cockpitLabel.textContent, 'Execute Stage Action');
});

test('Cockpit primary action synchronizes busy state when direct action in workbench is clicked', async () => {
    const pendingResponses = [];
    const directButton = { disabled: false };
    const cockpitAttrs = new Map();
    const cockpitButton = {
        disabled: false,
        onclick: null,
        setAttribute(name, value) { cockpitAttrs.set(name, value); },
        removeAttribute(name) { cockpitAttrs.delete(name); },
        getAttribute(name) { return cockpitAttrs.get(name) ?? null; },
    };
    const cockpitLabel = { textContent: 'Execute Stage Action' };
    const cockpitChip = { textContent: 'Available' };
    const cockpitDescription = { textContent: 'Action available for current state.' };

    const context = loadFrontend({
        fetchImpl: (...args) => new Promise((resolve) => {
            pendingResponses.push({ args, resolve });
        }),
        elements: {
            'cockpit-primary-action-btn': cockpitButton,
            'cockpit-primary-action-label': cockpitLabel,
            'cockpit-action-stage-chip': cockpitChip,
            'cockpit-action-description': cockpitDescription,
        },
    });

    const bootstrapAction = {
        request_kind: 'generate_vision_bootstrap',
        endpoint: 'vision/bootstrap',
        availability: 'available',
    };

    vm.runInContext(`
        selectedProjectId = 7;
        lifecycleState.actions = [${JSON.stringify(bootstrapAction)}];
        lifecycleState.position = {};
    `, context);
    context.renderTopCockpit();

    const promise = context.runDirectAction('generate_vision_bootstrap', directButton);

    assert.equal(directButton.disabled, true);
    assert.equal(cockpitButton.disabled, true);
    assert.equal(cockpitButton.getAttribute('aria-busy'), 'true');
    assert.equal(cockpitChip.textContent, 'Executing');
    assert.equal(cockpitLabel.textContent, 'Executing...');

    pendingResponses[0].resolve({
        ok: false,
        text: async () => JSON.stringify({ message: 'Expected test stop.' }),
    });

    await promise;

    assert.equal(directButton.disabled, false);
    assert.equal(cockpitButton.disabled, false);
    assert.equal(cockpitButton.getAttribute('aria-busy'), null);
});

test('Cockpit primary action preserves in-flight busy state during intermediate renderTopCockpit calls', async () => {
    const pendingResponses = [];
    const directButton = { disabled: false };
    const cockpitAttrs = new Map();
    const cockpitButton = {
        disabled: false,
        onclick: null,
        setAttribute(name, value) { cockpitAttrs.set(name, value); },
        removeAttribute(name) { cockpitAttrs.delete(name); },
        getAttribute(name) { return cockpitAttrs.get(name) ?? null; },
    };
    const cockpitLabel = { textContent: 'Execute Stage Action' };
    const cockpitChip = { textContent: 'Available' };
    const cockpitDescription = { textContent: 'Action available for current state.' };

    const context = loadFrontend({
        fetchImpl: (...args) => new Promise((resolve) => {
            pendingResponses.push({ args, resolve });
        }),
        elements: {
            'cockpit-primary-action-btn': cockpitButton,
            'cockpit-primary-action-label': cockpitLabel,
            'cockpit-action-stage-chip': cockpitChip,
            'cockpit-action-description': cockpitDescription,
        },
    });

    const bootstrapAction = {
        request_kind: 'generate_vision_bootstrap',
        endpoint: 'vision/bootstrap',
        availability: 'available',
    };

    vm.runInContext(`
        selectedProjectId = 7;
        lifecycleState.actions = [${JSON.stringify(bootstrapAction)}];
        lifecycleState.position = {};
    `, context);
    context.renderTopCockpit();

    const promise = context.runDirectAction('generate_vision_bootstrap', directButton);

    assert.equal(cockpitButton.disabled, true);
    assert.equal(cockpitButton.getAttribute('aria-busy'), 'true');
    assert.equal(cockpitLabel.textContent, 'Executing...');

    // Simulate an intermediate dashboard re-render while request is still pending
    context.renderTopCockpit();

    // Assert that the busy state was preserved
    assert.equal(cockpitButton.disabled, true);
    assert.equal(cockpitButton.getAttribute('aria-busy'), 'true');
    assert.equal(cockpitChip.textContent, 'Executing');
    assert.equal(cockpitLabel.textContent, 'Executing...');

    pendingResponses[0].resolve({
        ok: false,
        text: async () => JSON.stringify({ message: 'Expected test stop.' }),
    });

    await promise;

    assert.equal(cockpitButton.disabled, false);
    assert.equal(cockpitButton.getAttribute('aria-busy'), null);
});

function workflowPositionViewModel(position) {
    const decisions = Array.isArray(position?.decisions) ? position.decisions : [];
    const childGraphIds = [];
    decisions.forEach((decision) => {
        if (!childGraphIds.includes(decision.child_graph_id)) {
            childGraphIds.push(decision.child_graph_id);
        }
    });
    const nodeIds = (category) => decisions
        .filter((decision) => decision.category === category)
        .map((decision) => decision.node_id);
    return {
        childGraphIds,
        available: nodeIds('available'),
        waiting: nodeIds('waiting'),
        blocked: nodeIds('blocked'),
        invalid: nodeIds('invalid'),
        terminal: position?.terminal === true,
    };
}

function workflowActionPayload(position, decision, idempotencyKey, changedBy) {
    return {
        graph_version: position.graph_version,
        expected_fact_fingerprint: position.fact_fingerprint,
        expected_decision_fingerprint: decision.decision_fingerprint,
        idempotency_key: idempotencyKey,
        changed_by: changedBy,
    };
}

function workflowDecisionKey(decision) {
    return encodeURIComponent(JSON.stringify([
        decision.node_id,
        decision.instance_key ?? null,
        decision.decision_fingerprint,
    ]));
}

let selectedProjectId = null;
let currentWorkflowPosition = null;
let currentWorkflowActions = [];
let renderedWorkflowActions = new Map();
let pendingWorkflowAction = null;

function escapeWorkflowText(value) {
    const element = document.createElement('span');
    element.textContent = String(value ?? '');
    return element.innerHTML;
}

function categoryTone(category) {
    if (category === 'available') return 'border-emerald-300 bg-emerald-50 text-emerald-800';
    if (category === 'waiting') return 'border-sky-300 bg-sky-50 text-sky-800';
    if (category === 'blocked') return 'border-amber-300 bg-amber-50 text-amber-900';
    return 'border-red-300 bg-red-50 text-red-800';
}

function ensureWorkflowPanel() {
    let panel = document.getElementById('workflow-position-panel');
    if (panel) return panel;
    panel = document.createElement('section');
    panel.id = 'workflow-position-panel';
    document.querySelector('main')?.appendChild(panel);
    return panel;
}

function decisionAction(decision, action) {
    if (!action) return '';
    const decisionKey = workflowDecisionKey(decision);
    return `<button type="button" data-decision-key="${decisionKey}"
        class="workflow-action border border-slate-900 bg-slate-900 px-3 py-1.5 text-sm font-semibold text-white hover:bg-slate-700 disabled:opacity-50">
        Execute
    </button>`;
}

function renderWorkflowPosition(position, actions = []) {
    currentWorkflowPosition = position;
    currentWorkflowActions = Array.isArray(actions) ? actions : [];
    renderedWorkflowActions = new Map();
    const actionByDecision = new Map(
        currentWorkflowActions.map((action) => [workflowDecisionKey(action), action]),
    );
    const panel = ensureWorkflowPanel();
    const view = workflowPositionViewModel(position);
    const groups = view.childGraphIds.map((childGraphId) => {
        const decisions = position.decisions.filter(
            (decision) => decision.child_graph_id === childGraphId,
        );
        const rows = decisions.map((decision) => {
            const key = workflowDecisionKey(decision);
            const action = decision.category === 'available'
                ? actionByDecision.get(key)
                : null;
            if (action) {
                renderedWorkflowActions.set(key, {
                    position,
                    decision,
                    action,
                    idempotencyKey: `dashboard-${crypto.randomUUID()}`,
                });
            }
            const instance = decision.instance_key
                ? `<span class="font-mono text-xs text-slate-500">${escapeWorkflowText(decision.instance_key)}</span>`
                : '';
            return `
                <li class="grid min-h-16 grid-cols-[minmax(0,1fr)_auto] items-center gap-3 border-b border-slate-200 py-3 last:border-0">
                    <div class="min-w-0">
                        <div class="break-words font-mono text-sm font-semibold">${escapeWorkflowText(decision.node_id)}</div>
                        <div class="mt-1 flex flex-wrap gap-x-3 gap-y-1">
                            <span class="text-xs text-slate-500">${escapeWorkflowText(decision.reason_code)}</span>
                            ${instance}
                        </div>
                    </div>
                    <div class="flex items-center gap-2">
                        <span class="border px-2 py-1 text-xs font-semibold ${categoryTone(decision.category)}">${escapeWorkflowText(decision.category)}</span>
                        ${decisionAction(decision, action)}
                    </div>
                </li>
            `;
        }).join('');
        return `
            <section class="border-b border-slate-300 py-5 last:border-0">
                <h3 class="text-xs font-bold uppercase text-slate-500">${escapeWorkflowText(childGraphId)}</h3>
                <ul class="mt-2">${rows}</ul>
            </section>
        `;
    }).join('');
    panel.innerHTML = `
        <div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-300 pb-4">
            <div>
                <h2 class="text-xl font-bold">Workflow position</h2>
                <p class="mt-1 font-mono text-xs text-slate-500">${escapeWorkflowText(position.fact_fingerprint)}</p>
            </div>
            <span class="font-mono text-xs text-slate-500">${escapeWorkflowText(position.graph_version)}</span>
        </div>
        ${groups || '<p class="py-8 text-sm text-slate-500">No active decisions.</p>'}
    `;
    panel.querySelectorAll('.workflow-action').forEach((button) => {
        button.addEventListener('click', () => {
            const execution = renderedWorkflowActions.get(button.dataset.decisionKey);
            if (execution) beginWorkflowAction(execution, button);
        });
    });
}

function fieldLabel(name) {
    return name.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function workflowInputControl(field) {
    const id = `workflow-input-${field.name}`;
    const required = field.required === false ? '' : 'required';
    const label = escapeWorkflowText(fieldLabel(field.name));
    if (field.value_type === 'boolean') {
        return `
            <label class="flex items-center gap-3 text-sm font-medium" for="${id}">
                <input id="${id}" data-input-name="${escapeWorkflowText(field.name)}"
                    data-value-type="boolean" type="checkbox" class="size-4 border-slate-400 text-accent" />
                ${label}
            </label>
        `;
    }
    if (field.value_type === 'object' || field.value_type === 'array') {
        const initial = field.value_type === 'array' ? '[]' : '{}';
        return `
            <label class="block text-sm font-medium" for="${id}">${label}</label>
            <textarea id="${id}" data-input-name="${escapeWorkflowText(field.name)}"
                data-value-type="${escapeWorkflowText(field.value_type)}" ${required} rows="5"
                class="mt-1 w-full border-slate-300 font-mono text-sm focus:border-accent focus:ring-accent">${initial}</textarea>
        `;
    }
    const inputType = field.value_type === 'integer' ? 'number' : 'text';
    return `
        <label class="block text-sm font-medium" for="${id}">${label}</label>
        <input id="${id}" data-input-name="${escapeWorkflowText(field.name)}"
            data-value-type="${escapeWorkflowText(field.value_type)}" type="${inputType}" ${required}
            class="mt-1 w-full border-slate-300 text-sm focus:border-accent focus:ring-accent" />
    `;
}

function beginWorkflowAction(execution, button) {
    const fields = Array.isArray(execution.decision.required_inputs)
        ? execution.decision.required_inputs
        : [];
    if (fields.length === 0) {
        executeWorkflowAction(execution, {}, button);
        return;
    }
    const dialog = document.getElementById('workflow-action-dialog');
    const fieldsContainer = document.getElementById('workflow-action-fields');
    const title = document.getElementById('workflow-action-title');
    if (!dialog || !fieldsContainer || !title) return;
    pendingWorkflowAction = { execution, button };
    title.textContent = execution.decision.node_id;
    if (execution.action.transport === 'agentic') {
        fieldsContainer.innerHTML = workflowInputControl({
            name: 'payload',
            value_type: 'object',
            required: true,
        });
    } else {
        fieldsContainer.innerHTML = fields.map(workflowInputControl).join('');
    }
    setActionError('');
    dialog.showModal();
}

function readWorkflowInputs() {
    const fieldsContainer = document.getElementById('workflow-action-fields');
    const payload = {};
    fieldsContainer?.querySelectorAll('[data-input-name]').forEach((input) => {
        const name = input.dataset.inputName;
        const valueType = input.dataset.valueType;
        if (valueType === 'boolean') {
            payload[name] = input.checked;
            return;
        }
        if (!input.value && !input.required) return;
        if (valueType === 'integer') {
            payload[name] = Number.parseInt(input.value, 10);
            return;
        }
        if (valueType === 'object' || valueType === 'array') {
            payload[name] = JSON.parse(input.value);
            return;
        }
        payload[name] = input.value;
    });
    if (pendingWorkflowAction?.execution.action.transport === 'agentic') {
        return payload.payload;
    }
    return payload;
}

function setActionError(message) {
    const error = document.getElementById('workflow-action-error');
    if (!error) return;
    error.textContent = message;
    error.classList.toggle('hidden', !message);
}

function closeWorkflowActionDialog() {
    document.getElementById('workflow-action-dialog')?.close();
    pendingWorkflowAction = null;
    setActionError('');
}

async function executeWorkflowAction(execution, inputPayload, button) {
    const {
        position,
        decision,
        action,
        idempotencyKey,
    } = execution;
    const payload = workflowActionPayload(
        position,
        decision,
        idempotencyKey,
        'dashboard-ui',
    );
    if (action.transport === 'authority') {
        Object.assign(payload, inputPayload);
    } else {
        payload.instance_key = decision.instance_key;
        payload.input_payload = inputPayload;
    }
    button.disabled = true;
    try {
        const response = await fetch(
            `/api/projects/${selectedProjectId}/${action.endpoint}`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            },
        );
        const responsePayload = await response.json();
        const detail = responsePayload.detail;
        if (response.status === 409 && detail?.position) {
            closeWorkflowActionDialog();
            renderWorkflowPosition(detail.position, detail.actions ?? []);
            return;
        }
        if (!response.ok) {
            const message = detail?.error?.message || detail?.message || 'Workflow action failed.';
            throw new Error(message);
        }
        closeWorkflowActionDialog();
        await fetchWorkflowPosition();
    } catch (error) {
        setActionError(error.message);
        if (!document.getElementById('workflow-action-dialog')?.open) {
            window.alert(error.message);
        }
    } finally {
        button.disabled = false;
    }
}

async function fetchWorkflowPosition() {
    const response = await fetch(`/api/projects/${selectedProjectId}/position`);
    if (!response.ok) throw new Error('Failed to load workflow position.');
    const payload = await response.json();
    renderWorkflowPosition(payload.data, payload.actions ?? []);
}

async function fetchProjectDetail() {
    const response = await fetch(`/api/projects/${selectedProjectId}`);
    if (!response.ok) throw new Error('Failed to load project.');
    const payload = await response.json();
    const project = payload.data ?? {};
    const title = document.getElementById('project-page-title');
    if (title) title.textContent = project.name || `Project ${selectedProjectId}`;
    const origin = document.getElementById('project-origin');
    if (origin) origin.textContent = project.origin || 'Unknown';
    const storyCount = document.getElementById('project-story-count');
    if (storyCount) storyCount.textContent = String(project.structure_counts?.user_stories ?? 0);
    const sprintCount = document.getElementById('project-sprint-count');
    if (sprintCount) sprintCount.textContent = String(project.structure_counts?.sprints ?? 0);
}

function installWorkflowActionDialog() {
    const form = document.getElementById('workflow-action-form');
    form?.addEventListener('submit', (event) => {
        event.preventDefault();
        if (!pendingWorkflowAction) return;
        try {
            const inputPayload = readWorkflowInputs();
            executeWorkflowAction(
                pendingWorkflowAction.execution,
                inputPayload,
                pendingWorkflowAction.button,
            );
        } catch (error) {
            setActionError(error.message);
        }
    });
    document.getElementById('workflow-action-cancel')
        ?.addEventListener('click', closeWorkflowActionDialog);
    document.getElementById('workflow-action-cancel-icon')
        ?.addEventListener('click', closeWorkflowActionDialog);
}

window.addEventListener('DOMContentLoaded', async () => {
    const idValue = new URLSearchParams(window.location.search).get('id');
    selectedProjectId = Number.parseInt(idValue || '', 10);
    if (!Number.isInteger(selectedProjectId)) {
        window.location.href = '/dashboard';
        return;
    }
    installWorkflowActionDialog();
    document.getElementById('refresh-workflow-position')?.addEventListener(
        'click',
        fetchWorkflowPosition,
    );
    try {
        await Promise.all([fetchProjectDetail(), fetchWorkflowPosition()]);
    } catch (error) {
        ensureWorkflowPanel().textContent = error.message;
    }
});

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

const AGENTIC_ENDPOINTS = {
    'onboarding.brownfield.curation': 'brownfield/curate',
    'authority.compile': 'authority/compile',
    'authority.repair': 'authority/repair',
    'vision.generate': 'vision/generate',
    'backlog.generate': 'backlog/generate',
    'planning.roadmap.generate': 'roadmap/generate',
    'planning.story.generate': 'story/generate',
    'planning.sprint.plan': 'sprint/generate',
};

let selectedProjectId = null;
let currentWorkflowPosition = null;

function escapeWorkflowText(value) {
    const element = document.createElement('span');
    element.textContent = String(value ?? '');
    return element.innerHTML;
}

function categoryTone(category) {
    if (category === 'available') return 'text-emerald-700 bg-emerald-50 border-emerald-200';
    if (category === 'waiting') return 'text-sky-700 bg-sky-50 border-sky-200';
    if (category === 'blocked') return 'text-amber-700 bg-amber-50 border-amber-200';
    return 'text-red-700 bg-red-50 border-red-200';
}

function ensureWorkflowPanel() {
    let panel = document.getElementById('workflow-position-panel');
    if (panel) return panel;
    panel = document.createElement('section');
    panel.id = 'workflow-position-panel';
    panel.className = 'w-full border-y border-slate-200 dark:border-slate-700 py-5';
    const anchor = document.getElementById('setup-panel') || document.querySelector('main');
    anchor?.parentElement?.insertBefore(panel, anchor);
    return panel;
}

function decisionAction(decision) {
    const endpoint = AGENTIC_ENDPOINTS[decision.node_id];
    if (!endpoint) return '';
    return `<button type="button" data-node-id="${escapeWorkflowText(decision.node_id)}"
        class="workflow-action px-3 py-1.5 text-sm font-semibold bg-slate-900 text-white hover:bg-slate-700 disabled:opacity-50">
        Run
    </button>`;
}

function renderWorkflowPosition(position) {
    currentWorkflowPosition = position;
    const panel = ensureWorkflowPanel();
    const view = workflowPositionViewModel(position);
    const groups = view.childGraphIds.map((childGraphId) => {
        const decisions = position.decisions.filter(
            (decision) => decision.child_graph_id === childGraphId,
        );
        const rows = decisions.map((decision) => `
            <li class="grid grid-cols-[minmax(0,1fr)_auto] gap-3 items-center py-2 border-b border-slate-100 dark:border-slate-800 last:border-0">
                <div class="min-w-0">
                    <div class="font-mono text-sm break-words">${escapeWorkflowText(decision.node_id)}</div>
                    <div class="text-xs text-slate-500 break-words">${escapeWorkflowText(decision.reason_code)}</div>
                </div>
                <div class="flex items-center gap-2">
                    <span class="border px-2 py-1 text-xs ${categoryTone(decision.category)}">${escapeWorkflowText(decision.category)}</span>
                    ${decision.category === 'available' ? decisionAction(decision) : ''}
                </div>
            </li>
        `).join('');
        return `
            <section class="py-3">
                <h3 class="text-sm font-bold uppercase text-slate-500">${escapeWorkflowText(childGraphId)}</h3>
                <ul>${rows}</ul>
            </section>
        `;
    }).join('');
    panel.innerHTML = `
        <div class="flex items-center justify-between gap-3">
            <h2 class="text-xl font-bold">Workflow position</h2>
            <span class="text-xs font-mono text-slate-500">${escapeWorkflowText(position.graph_version)}</span>
        </div>
        ${groups || '<p class="py-4 text-sm text-slate-500">No active decisions.</p>'}
    `;
    panel.querySelectorAll('.workflow-action').forEach((button) => {
        button.addEventListener('click', () => runWorkflowAction(button.dataset.nodeId, button));
    });
}

async function runWorkflowAction(nodeId, button) {
    const decision = currentWorkflowPosition?.decisions?.find(
        (item) => item.node_id === nodeId && item.category === 'available',
    );
    const endpoint = AGENTIC_ENDPOINTS[nodeId];
    if (!decision || !endpoint) return;
    const rawInput = window.prompt('JSON input', '{}');
    if (rawInput === null) return;
    let inputPayload;
    try {
        inputPayload = JSON.parse(rawInput);
    } catch (_error) {
        window.alert('Invalid JSON input.');
        return;
    }
    button.disabled = true;
    try {
        const payload = workflowActionPayload(
            currentWorkflowPosition,
            decision,
            `dashboard-${crypto.randomUUID()}`,
            'dashboard-ui',
        );
        payload.instance_key = decision.instance_key;
        payload.input_payload = inputPayload;
        const response = await fetch(`/api/projects/${selectedProjectId}/${endpoint}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!response.ok) throw new Error('Workflow action failed.');
        await fetchWorkflowPosition();
    } catch (error) {
        window.alert(error.message);
    } finally {
        button.disabled = false;
    }
}

async function fetchWorkflowPosition() {
    const response = await fetch(`/api/projects/${selectedProjectId}/position`);
    if (!response.ok) throw new Error('Failed to load workflow position.');
    const payload = await response.json();
    renderWorkflowPosition(payload.data);
}

window.addEventListener('DOMContentLoaded', async () => {
    const idValue = new URLSearchParams(window.location.search).get('id');
    selectedProjectId = Number.parseInt(idValue || '', 10);
    if (!Number.isInteger(selectedProjectId)) {
        window.location.href = '/dashboard';
        return;
    }
    const title = document.getElementById('project-page-title');
    if (title) title.textContent = `Project ${selectedProjectId}`;
    try {
        await fetchWorkflowPosition();
    } catch (error) {
        ensureWorkflowPanel().textContent = error.message;
    }
});

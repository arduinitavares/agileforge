const STAGES = [
    'Vision',
    'Product Goal',
    'Specification',
    'Backlog',
    'Roadmap',
    'Stories',
    'Sprint',
    'Execution',
    'Review',
];

const REQUEST_STAGE = {
    abandon_product_goal: 'Product Goal',
    apply_story_dependencies: 'Stories',
    begin_vision_revision: 'Vision',
    close_sprint: 'Sprint',
    close_story: 'Stories',
    complete_task: 'Execution',
    decide_backlog: 'Backlog',
    decide_product_goal_review: 'Product Goal',
    decide_roadmap: 'Roadmap',
    decide_specification: 'Specification',
    decide_sprint_plan: 'Sprint',
    decide_story: 'Stories',
    fulfill_product_goal: 'Product Goal',
    generate_vision_bootstrap: 'Vision',
    record_backlog_draft: 'Backlog',
    record_post_sprint_triage: 'Review',
    record_product_goal_interview_turn: 'Product Goal',
    record_roadmap_draft: 'Roadmap',
    register_specification_source: 'Specification',
    record_sprint_plan: 'Sprint',
    record_story_draft: 'Stories',
    record_vision_interview_turn: 'Vision',
    repair_story_readiness: 'Stories',
    review_sprint: 'Review',
    start_sprint: 'Sprint',
    structure_specification: 'Specification',
};

const CHILD_STAGE = {
    backlog: 'Backlog',
    execution: 'Execution',
    product_goal: 'Product Goal',
    vision: 'Vision',
};

const DASHBOARD_CONTROL_REQUEST_KINDS = new Set([
    'abandon_product_goal',
    'apply_story_dependencies',
    'begin_vision_revision',
    'decide_backlog',
    'decide_product_goal_review',
    'decide_roadmap',
    'decide_specification',
    'decide_sprint_plan',
    'decide_story',
    'decide_vision_review',
    'fulfill_product_goal',
    'generate_vision_bootstrap',
    'record_backlog_draft',
    'record_product_goal_interview_turn',
    'record_roadmap_draft',
    'record_sprint_plan',
    'record_story_draft',
    'record_vision_interview_turn',
    'register_specification_source',
    'repair_story_readiness',
    'start_sprint',
    'structure_specification',
]);

const DELIVERY_ACTION_CONFIG = {
    record_backlog_draft: {
        label: 'Generate Backlog',
        busyLabel: 'Generating Backlog...',
        icon: 'auto_awesome',
        description: 'Generate the initial Product Backlog from the accepted Specification.',
    },
    record_roadmap_draft: {
        label: 'Generate Roadmap',
        busyLabel: 'Generating Roadmap...',
        icon: 'alt_route',
        description: 'Generate the delivery Roadmap from the accepted Product Backlog.',
    },
    record_story_draft: {
        label: 'Generate Stories',
        busyLabel: 'Generating Stories...',
        icon: 'auto_stories',
        description: 'Generate User Story drafts from the accepted Roadmap.',
    },
    record_sprint_plan: {
        label: 'Generate Sprint plan',
        busyLabel: 'Generating Sprint plan...',
        icon: 'flag',
        description: 'Generate the Sprint plan from the selected Sprint candidates.',
    },
};

const LIFECYCLE_CARD_STATES = {
    active: {
        status: 'Active',
        reason: null,
        tone: 'border-sky-300 bg-sky-50 text-sky-900',
    },
    complete: {
        status: 'Complete',
        reason: null,
        tone: 'border-emerald-200 bg-white text-slate-700',
    },
    failed_retry: {
        status: 'Failed',
        reason: 'Retry available.',
        tone: 'border-red-300 bg-red-50 text-red-800',
    },
    pending_human: {
        status: 'In progress',
        reason: 'A human decision is pending.',
        tone: 'border-sky-300 bg-sky-50 text-sky-900',
    },
};

const BUTTON_PRIMARY = 'inline-flex min-h-10 items-center justify-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-800 disabled:cursor-wait disabled:opacity-60';
const BUTTON_SECONDARY = 'inline-flex min-h-10 items-center justify-center gap-2 rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-slate-800 hover:bg-slate-100 disabled:cursor-wait disabled:opacity-60';
const BUTTON_DANGER = 'inline-flex min-h-10 items-center justify-center gap-2 rounded-lg border border-red-300 bg-white px-4 py-2 text-sm font-semibold text-red-700 hover:bg-red-50 disabled:cursor-wait disabled:opacity-60';

let selectedProjectId = null;
let pendingHumanAction = null;
let dashboardLoadSequence = 0;
let activeDashboardLoadController = null;
let activeStoryMutation = null;
let activeDependencyMutation = null;
let activeSpecificationMutation = null;
let activeBacklogCorrectionMutation = null;
let activeSprintMutation = null;
let sprintStartRetry = null;
let backlogFeedbackFocusIntent = false;
let lifecycleState = {
    project: {},
    position: {},
    actions: [],
    vision: {},
    goal: {},
    specification: {},
    planningReviews: {
        backlog: {},
        roadmap: {},
        stories: {},
        sprintPlan: {},
    },
    repository: {},
    sprintStatus: { kind: 'absent' },
};

function lifecycleStageLabels() {
    return [...STAGES];
}

function semanticMutationPayload(extra = {}) {
    return {
        idempotency_key: `dashboard-${crypto.randomUUID()}`,
        actor: 'dashboard-ui',
        ...extra,
    };
}

function structuralEligibilityMutationPayload(storyId) {
    return semanticMutationPayload({ story_ids: [storyId] });
}

function sprintSelectionMutationPayload(storyId, intent, expectedStateFingerprint) {
    const rationaleByIntent = {
        select: 'Selected for Sprint from dashboard.',
        remove: 'Removed from Sprint selection from dashboard.',
        defer: 'Deferred from Sprint selection from dashboard.',
    };
    return semanticMutationPayload({
        story_id: storyId,
        intent,
        expected_state_fingerprint: expectedStateFingerprint,
        rationale: rationaleByIntent[intent],
    });
}

function shouldUnlockDependencyMutation(mutationCompleted, refreshed) {
    return !mutationCompleted;
}

function captureStoryControlStates(controls) {
    return Array.from(controls, (control) => ({
        control,
        disabled: control.disabled,
        ariaDisabled: control.getAttribute('aria-disabled'),
        ariaBusy: control.getAttribute('aria-busy'),
    }));
}

function restoreStoryControlStates(states) {
    for (const state of states) {
        state.control.disabled = state.disabled;
        for (const [name, value] of [
            ['aria-disabled', state.ariaDisabled],
            ['aria-busy', state.ariaBusy],
        ]) {
            if (value === null) state.control.removeAttribute(name);
            else state.control.setAttribute(name, value);
        }
    }
}

function focusStoryReadiness(storyId) {
    const row = document.querySelector(`[data-story-readiness-row="${storyId}"]`);
    if (!row) return;
    const control = row.querySelector('[data-story-selection-intent]:not([disabled]), [data-story-structural-reconcile-id]:not([disabled])');
    if (control) {
        control.focus();
        return;
    }
    row.setAttribute('tabindex', '-1');
    row.focus();
}

async function postStorySelectionMutation(
    projectId,
    storyId,
    intent,
    expectedStateFingerprint,
    mutationPayload = null,
) {
    const payload = mutationPayload ?? sprintSelectionMutationPayload(
        storyId,
        intent,
        expectedStateFingerprint,
    );
    const path = `/api/projects/${projectId}/story/sprint-selection`;
    try {
        const response = await requestJson(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (response?.ok === false) {
            const failure = response.errors?.[0];
            const error = new Error(failure?.message || 'Story Sprint-selection was rejected.');
            error.status = 200;
            throw error;
        }
        return response;
    } catch (error) {
        if (error?.status) throw error;
        const response = await requestJson(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (response?.ok === false) {
            const failure = response.errors?.[0];
            const error = new Error(failure?.message || 'Story Sprint-selection was rejected.');
            error.status = 200;
            throw error;
        }
        return response;
    }
}

async function postStoryDependencyMutation(
    projectId,
    scopeIds,
    scopeEdges,
    scopeFingerprint,
    mutationPayload = null,
) {
    const payload = mutationPayload ?? semanticMutationPayload({
        selected_story_ids: scopeIds,
        selected_scope_fingerprint: scopeFingerprint,
        reviewed_edges: scopeEdges.map(({ dependent_story_id, prerequisite_story_id, reason }) => ({
            dependent_story_id,
            prerequisite_story_id,
            reason,
        })),
    });
    const path = `/api/projects/${projectId}/story/dependencies/apply`;
    const request = () => requestJson(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    try {
        return await request();
    } catch (error) {
        if (error?.status) throw error;
        return request();
    }
}

function escapeWorkflowText(value) {
    const element = document.createElement('span');
    element.textContent = String(value ?? '');
    return element.innerHTML;
}

function humanizeKey(value) {
    return String(value ?? '')
        .replaceAll('_', ' ')
        .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function readableScalar(value) {
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    return escapeWorkflowText(value);
}

function humanValueMarkup(value) {
    if (Array.isArray(value)) {
        if (value.length === 0) return '<p class="text-sm text-slate-500">None recorded.</p>';
        return `<ul class="space-y-2 text-sm leading-6 text-slate-700">${value.map((item) => (
            `<li class="flex min-w-0 gap-2"><span class="mt-2 size-1.5 shrink-0 rounded-full bg-slate-400"></span><div class="min-w-0 break-anywhere">${
                typeof item === 'object' && item !== null
                    ? humanValueMarkup(item)
                    : readableScalar(item)
            }</div></li>`
        )).join('')}</ul>`;
    }
    if (value && typeof value === 'object') {
        const entries = Object.entries(value).filter(([, item]) => item !== null);
        if (entries.length === 0) return '<p class="text-sm text-slate-500">None recorded.</p>';
        return `<dl class="grid min-w-0 gap-4 sm:grid-cols-2">${entries.map(([key, item]) => `
            <div class="min-w-0 border-l-2 border-slate-200 pl-3">
                <dt class="text-xs font-semibold uppercase text-slate-500">${escapeWorkflowText(humanizeKey(key))}</dt>
                <dd class="mt-1 min-w-0 break-anywhere text-sm leading-6 text-slate-700">${
                    typeof item === 'object' && item !== null
                        ? humanValueMarkup(item)
                        : readableScalar(item)
                }</dd>
            </div>
        `).join('')}</dl>`;
    }
    return `<p class="break-anywhere text-sm leading-6 text-slate-700">${readableScalar(value)}</p>`;
}

function findAction(actions, requestKind) {
    return (Array.isArray(actions) ? actions : []).find(
        (action) => action.request_kind === requestKind,
    ) ?? null;
}

function findDecisionAction(actions, decision) {
    const matches = (Array.isArray(actions) ? actions : []).filter((action) => (
        action?.request_kind === decision?.request_kind
        && (action?.instance_key ?? null) === (decision?.instance_key ?? null)
        && (!decision?.node_id || action?.node_id === decision.node_id)
    ));
    return matches.length === 1 ? matches[0] : null;
}

function decisionStage(decision) {
    return REQUEST_STAGE[decision?.request_kind]
        ?? CHILD_STAGE[decision?.child_graph_id]
        ?? null;
}

function decisionRank(category) {
    return {
        available: 4,
        waiting: 3,
        blocked: 2,
        invalid: 1,
    }[category] ?? 0;
}

function recommendationRank(kind) {
    return {
        required: 3,
        recovery: 2,
        optional_reentry: 1,
    }[kind] ?? 0;
}

function compareLifecycleDecisions(left, right, actions) {
    const leftPriority = [
        decisionRank(left?.category),
        isActionableDecision(left, actions) ? 1 : 0,
        recommendationRank(left?.recommendation_kind),
    ];
    const rightPriority = [
        decisionRank(right?.category),
        isActionableDecision(right, actions) ? 1 : 0,
        recommendationRank(right?.recommendation_kind),
    ];
    for (let index = 0; index < leftPriority.length; index += 1) {
        if (leftPriority[index] !== rightPriority[index]) {
            return leftPriority[index] - rightPriority[index];
        }
    }
    const leftKey = `${left?.request_kind ?? ''}:${left?.reason_code ?? ''}`;
    const rightKey = `${right?.request_kind ?? ''}:${right?.reason_code ?? ''}`;
    return rightKey.localeCompare(leftKey);
}

function isActionableDecision(decision, actions) {
    const action = findDecisionAction(actions, decision);
    return DASHBOARD_CONTROL_REQUEST_KINDS.has(decision?.request_kind)
        && action !== null
        && action.availability !== 'locked';
}

function lifecycleCardActions(position, actions, projections) {
    return (Array.isArray(actions) ? actions : []).filter((action) => {
        if (action?.request_kind !== 'structure_specification') return true;
        return captureSpecificationStructuringBinding({
            actions,
            position,
            specification: projections?.specification,
        }) !== null;
    });
}

function stageStatus(decision, actions, projections = {}) {
    if (findDecisionAction(actions, decision)?.availability === 'locked') {
        return 'Locked';
    }
    if (
        decision?.request_kind === 'start_sprint'
        && projections?.sprintStatus?.kind === 'ready'
        && sprintStartBinding(
            projections.sprintStatus.data,
            { decisions: [decision] },
            actions,
        )
    ) return 'Ready to start';
    const category = decision?.category;
    if (category === 'available') {
        return isActionableDecision(decision, actions) ? 'Ready' : 'Waiting';
    }
    if (category === 'waiting') {
        return isActionableDecision(decision, actions) ? 'In progress' : 'Waiting';
    }
    return {
        blocked: 'Waiting',
        invalid: 'Needs attention',
    }[category] ?? 'Upcoming';
}

function stageTone(decision, actions) {
    const category = decision?.category;
    if (
        ['available', 'waiting'].includes(category)
        && !isActionableDecision(decision, actions)
    ) {
        return 'border-slate-300 bg-white text-slate-700';
    }
    return {
        available: 'border-emerald-300 bg-emerald-50 text-emerald-900',
        waiting: 'border-sky-300 bg-sky-50 text-sky-900',
        blocked: 'border-slate-300 bg-white text-slate-700',
        invalid: 'border-red-300 bg-red-50 text-red-800',
    }[category] ?? 'border-slate-300 bg-white text-slate-600';
}

function waitingReason(decision) {
    const words = String(decision?.reason_code ?? '')
        .toLowerCase()
        .split('_')
        .filter(Boolean)
        .map((word) => (word === 'spec' ? 'Specification' : word));
    if (words.length === 0) return 'Finish the previous stage first.';
    const sentence = words.join(' ');
    return `${sentence[0].toUpperCase()}${sentence.slice(1)}.`;
}

function stageReason(decision, actions, projections = {}) {
    if (findDecisionAction(actions, decision)?.availability === 'locked') {
        return 'Correction input unavailable.';
    }
    if (
        decision?.request_kind === 'start_sprint'
        && projections?.sprintStatus?.kind === 'ready'
        && sprintStartBinding(
            projections.sprintStatus.data,
            { decisions: [decision] },
            actions,
        )
    ) return 'Accepted Sprint plan is ready to start.';
    const blockers = Array.isArray(decision?.blockers) ? decision.blockers : [];
    const blocker = blockers.find((item) => typeof item?.message === 'string');
    if (blocker) return blocker.message;
    if (decision?.category === 'waiting') {
        return isActionableDecision(decision, actions)
            ? 'A human decision is pending.'
            : waitingReason(decision);
    }
    if (decision?.category === 'available') {
        return isActionableDecision(decision, actions)
            ? 'Ready for your input.'
            : waitingReason(decision);
    }
    return {
        blocked: 'Finish the previous stage first.',
        invalid: 'Resolve the current lifecycle conflict.',
    }[decision?.category] ?? 'This stage follows the current work.';
}

function lifecycleCardProjection(position, actions = [], projections = {}) {
    const cardActions = lifecycleCardActions(position, actions, projections);
    const decisions = Array.isArray(position?.decisions) ? position.decisions : [];
    const byStage = new Map();
    decisions.forEach((decision) => {
        const stage = decisionStage(decision);
        if (!stage) return;
        const current = byStage.get(stage);
        if (!current || compareLifecycleDecisions(decision, current, cardActions) > 0) {
            byStage.set(stage, decision);
        }
    });
    cardActions.forEach((action) => {
        const stage = REQUEST_STAGE[action.request_kind];
        if (stage && !byStage.has(stage)) {
            byStage.set(stage, { category: 'available', ...action });
        }
    });

    const activeIndexes = STAGES
        .map((stage, index) => (byStage.has(stage) ? index : null))
        .filter((index) => index !== null);
    const firstActive = activeIndexes.length > 0 ? Math.min(...activeIndexes) : -1;

    const cards = STAGES.map((stage, index) => {
        const decision = byStage.get(stage);
        if (!decision && firstActive >= 0 && index < firstActive) {
            return {
                stage,
                ...LIFECYCLE_CARD_STATES.complete,
            };
        }
        return {
            stage,
            status: stageStatus(decision, cardActions, projections),
            reason: decision ? stageReason(decision, cardActions, projections) : null,
            tone: stageTone(decision, cardActions),
        };
    });
    const cardByStage = new Map(cards.map((card) => [card.stage, card]));
    const setCard = (stage, state) => {
        Object.assign(cardByStage.get(stage), LIFECYCLE_CARD_STATES[state]);
    };

    const vision = projections?.vision ?? {};
    if (vision?.candidate && vision?.review?.state === 'pending') {
        setCard('Vision', 'pending_human');
    } else if (vision?.current) {
        setCard('Vision', 'complete');
    }

    const goal = projections?.goal ?? {};
    if (goal?.candidate && goal?.review?.state === 'pending') {
        setCard('Product Goal', 'pending_human');
    } else if (goal?.active) {
        setCard('Product Goal', 'active');
    }

    const specification = projections?.specification ?? {};
    if (specification?.candidate && specification?.review?.state === 'pending') {
        setCard('Specification', 'pending_human');
    } else if (specification?.current || specification?.review?.state === 'accepted') {
        setCard('Specification', 'complete');
    }

    if (projections?.sprintStatus?.kind === 'ready') {
        const sprintStatus = projections.sprintStatus.data?.sprint?.status;
        if (sprintStatus === 'active') {
            setCard('Sprint', 'complete');
            setCard('Execution', 'active');
        } else if (sprintStatus === 'completed') {
            setCard('Sprint', 'complete');
            setCard('Execution', 'complete');
        }
    }

    return cards;
}

function workflowPositionMarkup(position, actions = [], projections = {}) {
    const cards = lifecycleCardProjection(position, actions, projections);

    return `<ol class="grid min-w-0 grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">${cards.map((card) => {
        return `<li class="min-w-0 rounded-lg border px-3 py-2 ${card.tone}" data-lifecycle-card="${escapeWorkflowText(card.stage)}">
            <p class="break-words text-xs font-semibold">${escapeWorkflowText(card.stage)}</p>
            <p class="mt-1 text-xs font-medium">${escapeWorkflowText(card.status)}</p>
            ${card.reason ? `<p class="mt-1 break-words text-xs leading-4 opacity-80">${escapeWorkflowText(card.reason)}</p>` : ''}
        </li>`;
    }).join('')}</ol>`;
}

function questionListMarkup(questions) {
    return `<ul class="space-y-2">${questions.map((question) => `
        <li class="flex min-w-0 gap-3 text-sm leading-6 text-slate-800">
            <span class="material-symbols-outlined mt-0.5 shrink-0 text-accent" aria-hidden="true">help</span>
            <span class="min-w-0 break-words">${escapeWorkflowText(
                typeof question === 'string' ? question : question?.text,
            )}</span>
        </li>
    `).join('')}</ul>`;
}

function transcriptMarkup(transcript, label) {
    if (!Array.isArray(transcript) || transcript.length === 0) return '';
    return `<div class="mt-6 border-t border-slate-200 pt-5">
        <h3 class="text-sm font-semibold">${escapeWorkflowText(label)} transcript</h3>
        <ol class="mt-3 space-y-3">${transcript.map((turn, index) => `
            <li class="grid min-w-0 grid-cols-[2rem_minmax(0,1fr)] gap-3 text-sm leading-6">
                <span class="grid size-8 place-items-center rounded-lg bg-slate-100 text-xs font-semibold text-slate-600">${index + 1}</span>
                <p class="min-w-0 break-words text-slate-700">${escapeWorkflowText(turn?.user_text ?? '')}</p>
            </li>
        `).join('')}</ol>
    </div>`;
}

function candidateMarkup(candidate, label) {
    const components = candidate?.components;
    return `<div class="max-w-3xl border-l-4 border-accent pl-4">
        <p class="text-xs font-semibold uppercase text-accent">Exact ${escapeWorkflowText(label)} candidate</p>
        <p class="mt-2 break-words text-base font-semibold leading-7">${escapeWorkflowText(candidate?.statement ?? '')}</p>
        ${components && Object.keys(components).length > 0
            ? `<div class="mt-4">${humanValueMarkup(components)}</div>`
            : ''}
    </div>`;
}

const VISION_COMPONENT_LABELS = {
    project_name: 'Project name',
    target_user: 'Target user',
    problem: 'Problem',
    product_category: 'Product category',
    key_benefit: 'Key benefit',
    competitors: 'Alternatives',
    differentiator: 'Differentiator',
};

const VISION_SOURCE_LABELS = {
    evidence: { icon: 'source', label: 'Project evidence' },
    human: { icon: 'person', label: 'Human input' },
    inference: { icon: 'lightbulb', label: 'Inferred' },
};

function visionComponentLabel(value) {
    return VISION_COMPONENT_LABELS[value] ?? humanizeKey(value);
}

function visionAffectedMarkup(components) {
    const labels = (Array.isArray(components) ? components : [])
        .map(visionComponentLabel)
        .filter(Boolean);
    if (labels.length === 0) return '';
    return `<p class="mt-1 break-words text-xs text-slate-500">${escapeWorkflowText(labels.join(', '))}</p>`;
}

function visionSourceKindsMarkup(sourceKinds) {
    const sources = (Array.isArray(sourceKinds) ? sourceKinds : [])
        .map((kind) => VISION_SOURCE_LABELS[kind])
        .filter(Boolean);
    if (sources.length === 0) {
        return '<span class="text-xs text-slate-500">Basis pending</span>';
    }
    return `<ul class="flex min-w-0 flex-wrap gap-x-4 gap-y-1">${sources.map((source) => `
        <li class="inline-flex min-w-0 items-center gap-1 text-xs text-slate-600">
            <span class="material-symbols-outlined shrink-0 text-[1rem]" aria-hidden="true">${source.icon}</span>
            <span class="break-words">${source.label}</span>
        </li>
    `).join('')}</ul>`;
}

function visionComponentsMarkup(components) {
    const items = Array.isArray(components) ? components : [];
    return `<section class="min-w-0">
        <h3 class="text-sm font-semibold">Components and basis</h3>
        <dl class="mt-3 divide-y divide-slate-200 border-y border-slate-200">${items.map((component) => `
            <div class="grid min-w-0 gap-2 py-3 sm:grid-cols-[10rem_minmax(0,1fr)] sm:gap-4">
                <dt class="break-words text-xs font-semibold uppercase text-slate-500">${escapeWorkflowText(visionComponentLabel(component?.name))}</dt>
                <dd class="min-w-0">
                    <p class="break-words text-sm leading-6 ${component?.value ? 'text-slate-800' : 'text-slate-500'}">${escapeWorkflowText(component?.value ?? 'Not yet defined')}</p>
                    <div class="mt-1">${visionSourceKindsMarkup(component?.source_kinds)}</div>
                </dd>
            </div>
        `).join('')}</dl>
    </section>`;
}

function visionAssumptionsMarkup(assumptions) {
    const items = Array.isArray(assumptions) ? assumptions : [];
    return `<section class="min-w-0">
        <h3 class="text-sm font-semibold">Assumptions</h3>
        ${items.length > 0 ? `<ul class="mt-3 space-y-3">${items.map((item) => `
            <li class="flex min-w-0 gap-2 border-l-2 border-amber-300 pl-3 text-sm leading-6 text-slate-700">
                <span class="material-symbols-outlined mt-0.5 shrink-0 text-amber-700" aria-hidden="true">lightbulb</span>
                <div class="min-w-0"><p class="break-words">${escapeWorkflowText(item?.text)}</p>${visionAffectedMarkup(item?.affected_components)}</div>
            </li>
        `).join('')}</ul>` : '<p class="mt-2 text-sm text-slate-500">None recorded.</p>'}
    </section>`;
}

function visionConflictsMarkup(conflicts) {
    const items = Array.isArray(conflicts) ? conflicts : [];
    return `<section class="min-w-0">
        <h3 class="text-sm font-semibold">Conflicts</h3>
        ${items.length > 0 ? `<ul class="mt-3 space-y-3">${items.map((item) => `
            <li class="flex min-w-0 gap-2 border-l-2 ${item?.status === 'resolved' ? 'border-emerald-300' : 'border-red-300'} pl-3 text-sm leading-6 text-slate-700">
                <span class="material-symbols-outlined mt-0.5 shrink-0 ${item?.status === 'resolved' ? 'text-emerald-700' : 'text-red-700'}" aria-hidden="true">${item?.status === 'resolved' ? 'check_circle' : 'error'}</span>
                <div class="min-w-0">
                    <p class="break-words">${escapeWorkflowText(item?.text)}</p>
                    ${visionAffectedMarkup(item?.affected_components)}
                    ${item?.resolution ? `<p class="mt-1 break-words text-xs text-slate-600"><strong>Resolution:</strong> ${escapeWorkflowText(item.resolution)}</p>` : ''}
                </div>
            </li>
        `).join('')}</ul>` : '<p class="mt-2 text-sm text-slate-500">None recorded.</p>'}
    </section>`;
}

function visionReviewMaterialMarkup(material, label) {
    return `<div class="max-w-4xl min-w-0">
        <div class="border-l-4 border-accent pl-4">
            <p class="text-xs font-semibold uppercase text-accent">${escapeWorkflowText(label)}</p>
            <p class="mt-2 break-words text-base font-semibold leading-7">${escapeWorkflowText(material?.statement)}</p>
        </div>
        <div class="mt-6 grid min-w-0 gap-6 lg:grid-cols-[minmax(0,1.5fr)_minmax(16rem,1fr)]">
            ${visionComponentsMarkup(material?.components)}
            <div class="grid min-w-0 content-start gap-6">
                ${visionAssumptionsMarkup(material?.assumptions)}
                ${visionConflictsMarkup(material?.conflicts)}
            </div>
        </div>
    </div>`;
}

function reviewControlsMarkup(scope, action) {
    if (!action) return '';
    return `<div class="mt-5 flex flex-wrap gap-3">
        <button type="button" data-review-scope="${scope}" data-review-decision="accepted" class="${BUTTON_PRIMARY}">
            <span class="material-symbols-outlined" aria-hidden="true">check</span><span>Accept</span>
        </button>
        <button type="button" data-review-scope="${scope}" data-review-decision="feedback" class="${BUTTON_SECONDARY}">
            <span class="material-symbols-outlined" aria-hidden="true">rate_review</span><span>Feedback</span>
        </button>
        <button type="button" data-review-scope="${scope}" data-review-decision="rejected" class="${BUTTON_DANGER}">
            <span class="material-symbols-outlined" aria-hidden="true">close</span><span>Reject</span>
        </button>
    </div>`;
}

function interviewFormMarkup(scope, questions, transcript, label) {
    return `<div class="grid min-w-0 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,28rem)]">
        <div class="min-w-0">
            <p class="mb-3 text-sm font-semibold">Focused questions</p>
            ${questions.length > 0
                ? questionListMarkup(questions)
                : '<p class="text-sm text-slate-500">No open questions recorded.</p>'}
            ${transcriptMarkup(transcript, label)}
        </div>
        <form data-interview-scope="${scope}" class="min-w-0 self-start border-l-2 border-slate-200 pl-4">
            <label for="${scope}-response" class="text-sm font-semibold">Your response</label>
            <textarea id="${scope}-response" rows="6" required
                class="mt-2 w-full resize-y rounded-lg border-slate-300 text-sm leading-6 focus:border-accent focus:ring-accent"></textarea>
            <p id="${scope}-response-status" hidden role="alert" aria-live="assertive"
                class="mt-3 rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800"></p>
            <div class="mt-3 flex justify-end">
                <button type="submit" class="${BUTTON_PRIMARY}">
                    <span class="material-symbols-outlined" aria-hidden="true">send</span><span data-interview-submit-label aria-live="polite">Send response</span>
                </button>
            </div>
        </form>
    </div>`;
}

function visionBootstrapContextMarkup(context) {
    const project = context?.project ?? {};
    return `<div class="grid min-w-0 gap-4 sm:grid-cols-2">
        <div class="min-w-0 border-l-2 border-slate-300 pl-3">
            <p class="text-xs font-semibold uppercase text-slate-500">Project</p>
            <p class="mt-1 break-words text-sm font-semibold text-slate-800">${escapeWorkflowText(project.name ?? 'Current Project')}</p>
            ${project.description ? `<p class="mt-1 break-words text-sm leading-6 text-slate-600">${escapeWorkflowText(project.description)}</p>` : ''}
        </div>
        <div class="min-w-0 border-l-2 border-slate-300 pl-3">
            <p class="text-xs font-semibold uppercase text-slate-500">Repository context</p>
            <p class="mt-1 text-sm text-slate-700">${context?.repository ? 'Attached' : 'Not attached'}</p>
        </div>
    </div>`;
}

function visionPanelMarkup(projection, actions = [], context = {}) {
    const candidate = projection?.candidate;
    const reviewState = projection?.review?.state;
    const reviewAction = findAction(actions, 'decide_vision_review');
    if (candidate && reviewState === 'pending') {
        return `${visionReviewMaterialMarkup(candidate, 'Vision candidate')}${reviewControlsMarkup('vision', reviewAction)}`;
    }

    const respondAction = findAction(actions, 'record_vision_interview_turn');
    if (respondAction) {
        const material = projection?.draft ?? candidate;
        const questions = Array.isArray(material?.questions) ? material.questions : [];
        const feedback = ['feedback', 'rejected'].includes(reviewState)
            ? `<p class="mb-5 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900"><strong>Review response:</strong> ${escapeWorkflowText(projection?.review?.rationale ?? 'Revise the Vision with the review in mind.')}</p>`
            : '';
        return `${feedback}${visionReviewMaterialMarkup(material, 'Vision draft')}<div class="mt-6">${interviewFormMarkup(
            'vision',
            questions,
            projection?.transcript ?? [],
            'Vision',
        )}</div>`;
    }

    const bootstrapAction = findAction(actions, 'generate_vision_bootstrap');
    if (bootstrapAction) {
        const current = projection?.current
            ? `<div class="mb-5 border-l-4 border-emerald-500 pl-4">
                <p class="text-xs font-semibold uppercase text-emerald-700">Accepted Vision</p>
                <p class="mt-2 break-words text-base font-semibold leading-7">${escapeWorkflowText(projection.current.statement)}</p>
            </div>`
            : '';
        return `${current}<p class="mb-4 text-sm leading-6 text-slate-600">Draft from available Project context.</p>
            ${visionBootstrapContextMarkup(context)}
            <button type="button" data-direct-action="generate_vision_bootstrap" class="mt-5 ${BUTTON_PRIMARY}">
                <span class="material-symbols-outlined" aria-hidden="true">auto_awesome</span><span>Generate Vision draft</span>
            </button>`;
    }

    if (projection?.current) {
        const revisionAction = findAction(actions, 'begin_vision_revision');
        return `<div class="max-w-3xl border-l-4 border-emerald-500 pl-4">
            <p class="text-xs font-semibold uppercase text-emerald-700">Accepted Vision</p>
            <p class="mt-2 break-words text-base font-semibold leading-7">${escapeWorkflowText(projection.current.statement)}</p>
        </div>
        ${revisionAction ? `<button type="button" data-vision-revision="true" class="mt-5 ${BUTTON_SECONDARY}"><span class="material-symbols-outlined" aria-hidden="true">edit</span><span>Revise Vision</span></button>` : ''}`;
    }

    return '<p class="text-sm text-slate-600">Vision is waiting for the current lifecycle state.</p>';
}

function acceptedVisionMarkup(acceptedVision) {
    if (!acceptedVision) return '';
    return `<aside class="mb-5 border-l-2 border-slate-300 pl-4">
        <p class="text-xs font-semibold uppercase text-slate-500">Accepted Vision context</p>
        <p class="mt-1 break-words text-sm leading-6 text-slate-700">${escapeWorkflowText(acceptedVision.statement)}</p>
    </aside>`;
}

function goalOutcomeMarkup(projection, actions) {
    if (projection?.outcome) {
        return `<div class="border-l-4 border-slate-400 pl-4">
            <p class="text-xs font-semibold uppercase text-slate-500">Goal ${escapeWorkflowText(projection.outcome.outcome)}</p>
            <p class="mt-2 break-words text-sm leading-6 text-slate-700">${escapeWorkflowText(projection.outcome.rationale)}</p>
        </div>`;
    }
    const fulfill = findAction(actions, 'fulfill_product_goal');
    const abandon = findAction(actions, 'abandon_product_goal');
    if (!fulfill && !abandon) return '';
    return `<div class="mt-5 flex flex-wrap gap-3">
        ${fulfill ? `<button type="button" data-goal-outcome="fulfilled" class="${BUTTON_PRIMARY}"><span class="material-symbols-outlined" aria-hidden="true">task_alt</span><span>Fulfill Goal</span></button>` : ''}
        ${abandon ? `<button type="button" data-goal-outcome="abandoned" class="${BUTTON_DANGER}"><span class="material-symbols-outlined" aria-hidden="true">flag</span><span>Abandon Goal</span></button>` : ''}
    </div>`;
}

function productGoalPanelMarkup(projection, actions = []) {
    const visionContext = acceptedVisionMarkup(projection?.accepted_vision);
    if (!projection?.accepted_vision) {
        return '<p class="text-sm text-slate-600">Accept the Project Vision before setting a Product Goal.</p>';
    }

    const candidate = projection?.candidate;
    const reviewState = projection?.review?.state;
    const reviewAction = findAction(actions, 'decide_product_goal_review');
    if (candidate && reviewState === 'pending') {
        return `${visionContext}${candidateMarkup(candidate, 'Product Goal')}${reviewControlsMarkup('goal', reviewAction)}`;
    }

    const respondAction = findAction(actions, 'record_product_goal_interview_turn');
    if (respondAction) {
        const questions = Array.isArray(projection?.latest_questions)
            && projection.latest_questions.length > 0
            ? projection.latest_questions
            : [
                'What valuable outcome should this Project achieve next?',
                'What observable result will prove success?',
                'What boundary keeps this Goal focused?',
            ];
        const feedback = ['feedback', 'rejected'].includes(reviewState)
            ? `<p class="mb-5 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900"><strong>Review response:</strong> ${escapeWorkflowText(projection?.review?.rationale ?? 'Revise the Product Goal with the review in mind.')}</p>`
            : '';
        return `${visionContext}${feedback}<p class="mb-5 text-sm font-semibold">Product Goal interview</p>${interviewFormMarkup(
            'goal',
            questions,
            projection?.transcript ?? [],
            'Product Goal',
        )}`;
    }

    if (projection?.active) {
        return `${visionContext}<div class="max-w-3xl border-l-4 border-sky-500 pl-4">
            <p class="text-xs font-semibold uppercase text-sky-700">Active Product Goal</p>
            <p class="mt-2 break-words text-base font-semibold leading-7">${escapeWorkflowText(projection.active.statement)}</p>
        </div>${goalOutcomeMarkup(projection, actions)}`;
    }

    return `${visionContext}${goalOutcomeMarkup(projection, actions) || '<p class="text-sm text-slate-600">Product Goal is waiting for the current lifecycle state.</p>'}`;
}

function specificationSourceSubmission(actions, sourcePath, preparationCapability, adrPathsText) {
    return {
        action: captureAction(findAction(actions, 'register_specification_source')),
        fields: {
            source_path: String(sourcePath ?? '').trim(),
            preparation_capability: String(preparationCapability ?? '').trim(),
            adr_paths: String(adrPathsText ?? '')
                .split(/\r?\n/)
                .map((path) => path.trim())
                .filter(Boolean),
        },
    };
}

function specificationSourceRegistrationMarkup(actions) {
    const action = findAction(actions, 'register_specification_source');
    return action
        ? `<form data-specification-source-form="true" class="max-w-3xl space-y-4">
                <p class="text-sm leading-6 text-slate-600">Register the repository document that contains the external Specification source.</p>
                <div>
                    <label for="specification-source-path" class="text-sm font-semibold">Source path</label>
                    <input id="specification-source-path" name="source_path" type="text" required autocomplete="off" placeholder="specification.md" class="mt-1.5 w-full rounded-lg border-slate-300 font-mono text-sm focus:border-accent focus:ring-accent" />
                    <p class="mt-1 text-xs leading-5 text-slate-500">Use a repository-relative path.</p>
                </div>
                <div>
                    <label for="specification-adr-paths" class="text-sm font-semibold">Applicable ADR paths <span class="font-normal text-slate-500">(optional)</span></label>
                    <textarea id="specification-adr-paths" name="adr_paths" rows="3" placeholder="docs/adr/0001-decision.md" class="mt-1.5 w-full resize-y rounded-lg border-slate-300 font-mono text-sm focus:border-accent focus:ring-accent"></textarea>
                    <p class="mt-1 text-xs leading-5 text-slate-500">Enter one repository-relative ADR path per line.</p>
                </div>
                <div>
                    <label for="specification-preparation-capability" class="text-sm font-semibold">Preparation capability</label>
                    <select id="specification-preparation-capability" name="preparation_capability" required class="mt-1.5 w-full rounded-lg border-slate-300 text-sm focus:border-accent focus:ring-accent">
                        <option value="">Select the capability that prepared this source</option>
                        <option value="grill-with-docs">grill-with-docs</option>
                    </select>
                    <p class="mt-1 text-xs leading-5 text-slate-500">External preparation attestation. AgileForge does not infer or prove the agent's reasoning.</p>
                </div>
                <div class="rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm leading-6 text-slate-600">
                    <p>Root CONTEXT.md is captured automatically when present; its absence is recorded.</p>
                </div>
                <button type="submit" class="${BUTTON_PRIMARY}"><span class="material-symbols-outlined" aria-hidden="true">inventory_2</span><span>Register Specification source</span></button>
            </form>`
        : '';
}

function currentSpecificationSourceDisplay(source) {
    if (!source || typeof source !== 'object') return null;
    const sourcePath = source?.source?.relative_path;
    const preparationCapability = source?.preparation_capability;
    if (typeof sourcePath !== 'string' || !sourcePath.trim()
        || typeof preparationCapability !== 'string' || !preparationCapability.trim()
        || !Array.isArray(source.adrs)) return null;
    const adrPaths = source.adrs.map((adr) => adr?.relative_path);
    if (adrPaths.some((path) => typeof path !== 'string' || !path.trim())) return null;
    return { sourcePath, preparationCapability, adrPaths };
}

function currentSpecificationSourceMarkup(source, display = currentSpecificationSourceDisplay(source)) {
    if (source === null || source === undefined) return '';
    if (!display) {
        return `<section data-current-specification-source-unavailable="true" role="alert" class="max-w-3xl rounded-lg border border-red-300 bg-red-50 p-4 text-sm leading-6 text-red-900">Current Specification source evidence is unavailable. Source registration and structuring controls are unavailable until the current source projection is complete.</section>`;
    }
    const adrs = display.adrPaths.length ? display.adrPaths.join(', ') : 'No ADRs';
    return `<section data-current-specification-source="true" role="status" class="max-w-3xl rounded-lg border border-sky-200 bg-sky-50 p-4">
        <p class="text-sm font-semibold text-sky-950">Current registered Specification source</p>
        <dl class="mt-3 grid gap-3 text-sm sm:grid-cols-3">
            <div class="min-w-0"><dt class="font-semibold text-slate-700">Source path</dt><dd class="mt-1 break-anywhere font-mono text-slate-900">${escapeWorkflowText(display.sourcePath)}</dd></div>
            <div class="min-w-0"><dt class="font-semibold text-slate-700">Applicable ADRs</dt><dd class="mt-1 break-anywhere font-mono text-slate-900">${escapeWorkflowText(adrs)}</dd></div>
            <div class="min-w-0"><dt class="font-semibold text-slate-700">Preparation capability</dt><dd class="mt-1 break-words font-mono text-slate-900">${escapeWorkflowText(display.preparationCapability)}</dd></div>
        </dl>
    </section>`;
}

function specificationRevisionRegistrationMarkup(registration) {
    if (!registration) return '';
    return `<details data-specification-revision-registration="true" class="max-w-3xl rounded-lg border border-slate-200 bg-white p-4">
        <summary class="cursor-pointer text-sm font-semibold text-slate-800">Register a revised source</summary>
        <div class="mt-4 border-t border-slate-200 pt-4">
            <p class="mb-4 text-sm leading-6 text-slate-600">Choose this path only when the external Specification source itself changed.</p>
            ${registration}
        </div>
    </details>`;
}

function acceptedSpecificationMarkup(current) {
    if (!current) return '';
    return `<div class="max-w-4xl space-y-3">
        <p class="text-sm font-semibold text-slate-700">Accepted Specification</p>
        <pre class="whitespace-pre-wrap break-words rounded-lg border border-slate-300 bg-white p-4 font-mono text-sm leading-6">${escapeWorkflowText(current.candidate?.rendered_markdown ?? '')}</pre>
    </div>`;
}

function singleDecisionReference(decision, factType) {
    const matches = (Array.isArray(decision?.fact_references)
        ? decision.fact_references
        : []
    ).filter((item) => item?.fact_type === factType);
    return matches.length === 1 ? matches[0] : null;
}

function referenceMatches(reference, id, fingerprint) {
    return reference?.fact_id === String(id)
        && reference?.fingerprint === fingerprint;
}

function captureSpecificationStructuringBinding(state) {
    const action = captureAction(findAction(state?.actions, 'structure_specification'));
    const decisions = (Array.isArray(state?.position?.decisions)
        ? state.position.decisions
        : []
    ).filter((decision) => decision?.node_id === 'specification.structure'
        && decision?.request_kind === 'structure_specification'
        && decision?.category === 'available');
    if (!action || decisions.length !== 1) return null;
    const decision = decisions[0];
    if (typeof decision.decision_fingerprint !== 'string'
        || !decision.decision_fingerprint) return null;
    const projection = state?.specification ?? {};
    const candidate = projection.candidate;
    if (!candidate) {
        const candidateReference = singleDecisionReference(
            decision,
            'specification_candidate',
        );
        const sourceReference = singleDecisionReference(
            decision,
            'specification_source',
        );
        if (!referenceMatches(
            sourceReference,
            projection.source?.specification_source_id,
            projection.source?.source_fingerprint,
        )) return null;
        const revisedSource = decision.reason_code === 'SPECIFICATION_REVISION_REQUIRED'
            && typeof candidateReference?.fact_id === 'string'
            && Boolean(candidateReference.fact_id)
            && typeof candidateReference?.fingerprint === 'string'
            && Boolean(candidateReference.fingerprint);
        if (candidateReference && !revisedSource) return null;
        return {
            action,
            expectedDecision: decision.decision_fingerprint,
            mode: revisedSource ? 'revised-source' : 'structure',
        };
    }
    if (projection.review?.state !== 'feedback') return null;
    const source = projection.source;
    const candidateReference = singleDecisionReference(
        decision,
        'specification_candidate',
    );
    const sourceReference = singleDecisionReference(
        decision,
        'specification_source',
    );
    if (!source
        || !referenceMatches(
            candidateReference,
            candidate.specification_candidate_id,
            candidate.candidate_fingerprint,
        )
        || !referenceMatches(
            sourceReference,
            source.specification_source_id,
            source.source_fingerprint,
        )) return null;
    const sameSource = candidate.specification_source_id
            === source.specification_source_id
        && candidate.registered_source_fingerprint === source.source_fingerprint;
    let mode = null;
    if ((decision.reason_code === 'SPECIFICATION_FEEDBACK_RETRY_AVAILABLE'
            || decision.reason_code === 'SPECIFICATION_STRUCTURER_FAILED')
        && sameSource) {
        mode = 'same-source-feedback';
    } else if (decision.reason_code === 'SPECIFICATION_REVISION_REQUIRED'
        && !sameSource) {
        mode = 'revised-source';
    }
    return mode
        ? { action, expectedDecision: decision.decision_fingerprint, mode }
        : null;
}

function captureSpecificationSourceRegistrationBinding(state) {
    const action = captureAction(
        findAction(state?.actions, 'register_specification_source'),
    );
    const decisions = (Array.isArray(state?.position?.decisions)
        ? state.position.decisions
        : []
    ).filter((decision) => decision?.node_id === 'specification.source.register'
        && decision?.request_kind === 'register_specification_source'
        && decision?.category === 'available');
    if (!action || decisions.length !== 1) return null;
    const decision = decisions[0];
    if (typeof decision.decision_fingerprint !== 'string'
        || !decision.decision_fingerprint) return null;
    const projection = state?.specification ?? {};
    const sourceReference = singleDecisionReference(
        decision,
        'specification_source',
    );
    const candidateReference = singleDecisionReference(
        decision,
        'specification_candidate',
    );
    if (sourceReference && !referenceMatches(
        sourceReference,
        projection.source?.specification_source_id,
        projection.source?.source_fingerprint,
    )) return null;
    if (candidateReference && !referenceMatches(
        candidateReference,
        projection.candidate?.specification_candidate_id,
        projection.candidate?.candidate_fingerprint,
    )) return null;
    if (projection.review?.state === 'feedback'
        && (decision.reason_code
                !== 'SPECIFICATION_FEEDBACK_SOURCE_REVISION_AVAILABLE'
            || !sourceReference
            || !candidateReference)) return null;
    return {
        action,
        expectedDecision: decision.decision_fingerprint,
    };
}

function specificationFeedbackContinuationMarkup(
    projection,
    actions,
    revisedRegistration,
    position,
) {
    if (projection?.review?.state !== 'feedback') return revisedRegistration;
    const binding = captureSpecificationStructuringBinding({
        actions,
        position,
        specification: projection,
    });
    if (!binding) return revisedRegistration;
    const rationale = projection.review.rationale
        ? `<p class="mt-2 text-sm leading-6 text-amber-900"><strong>Feedback:</strong> ${escapeWorkflowText(projection.review.rationale)}</p>`
        : '';
    const sameSource = binding.mode === 'same-source-feedback';
    const instruction = sameSource
        ? 'Retry with the unchanged registered source when the candidate transformation needs correction.'
        : 'Structure the genuinely revised source that is now registered.';
    const label = sameSource
        ? 'Retry structuring from unchanged source'
        : 'Structure revised source';
    return `<section data-specification-feedback-continuation="true" class="mt-5 max-w-4xl rounded-lg border border-amber-300 bg-amber-50 p-5">
        <p class="text-sm font-semibold text-amber-950">Choose how to address Specification Feedback</p>
        ${rationale}
        <p class="mt-4 text-sm leading-6 text-slate-700">${instruction}</p>
        ${specificationStructuringActionMarkup(label, 'refresh')}
        ${revisedRegistration}
    </section>`;
}

function specificationStructuringActionMarkup(label, icon) {
    return `<div data-specification-structuring-action="true" class="mt-4">
        <button type="button" data-direct-action="structure_specification" class="${BUTTON_PRIMARY}">
            <span class="material-symbols-outlined" aria-hidden="true">${icon}</span>
            <span data-specification-structuring-label="true">${label}</span>
        </button>
        <p data-specification-structuring-status="true" hidden role="status" aria-live="polite" aria-atomic="true"
            class="mt-3 text-sm leading-6 text-slate-700"></p>
    </div>`;
}

function specificationPanelMarkup(projection, actions = [], position = {}) {
    const candidate = projection?.candidate;
    const source = projection?.source;
    const hasCurrentSource = source !== null && source !== undefined;
    const sourceDisplay = currentSpecificationSourceDisplay(source);
    const hasValidCurrentSource = sourceDisplay !== null;
    const registration = hasCurrentSource && !hasValidCurrentSource
        ? ''
        : specificationSourceRegistrationMarkup(actions);
    const currentSource = currentSpecificationSourceMarkup(source, sourceDisplay);
    const revisedRegistration = hasValidCurrentSource
        ? specificationRevisionRegistrationMarkup(registration)
        : registration;
    const withSourceState = (markup) => hasCurrentSource
        ? `<div data-specification-source-state="true" class="space-y-5">${markup}</div>`
        : markup;
    if (!candidate) {
        const structureBinding = hasValidCurrentSource
            ? captureSpecificationStructuringBinding({
                actions,
                position,
                specification: projection,
            })
            : null;
        const revisedSource = structureBinding?.mode === 'revised-source';
        const structure = structureBinding
            ? `<div class="max-w-3xl">
                <p class="mb-4 text-sm leading-6 text-slate-600">${revisedSource ? 'Structure the genuinely revised registered source with exact prior review lineage.' : 'Structure the registered source into an exact reviewable Specification candidate.'}</p>
                ${specificationStructuringActionMarkup(
                    revisedSource ? 'Structure revised source' : 'Structure Specification',
                    'schema',
                )}
            </div>`
            : '';
        const current = acceptedSpecificationMarkup(projection?.current);
        if (revisedSource) {
            return withSourceState([
                current,
                currentSource,
                `<section data-specification-feedback-continuation="true" class="max-w-4xl space-y-5 rounded-lg border border-amber-300 bg-amber-50 p-5">
                    ${structure}
                    ${revisedRegistration}
                </section>`,
            ].filter(Boolean).join(''));
        }
        const markup = [current, currentSource, revisedRegistration, structure]
            .filter(Boolean).join('');
        return withSourceState(markup)
            || '<p class="text-sm text-slate-600">Specification preparation is waiting for the current lifecycle state.</p>';
    }
    const review = projection?.review;
    const reviewAction = findAction(actions, 'decide_specification');
    const decisionCopy = review?.state && review.state !== 'pending'
        ? `<p class="mb-4 text-sm font-semibold text-slate-700">Review: ${escapeWorkflowText(humanizeKey(review.state))}</p>`
        : '<p class="mb-4 text-sm font-semibold text-slate-700">Exact Specification candidate</p>';
    const controls = review?.state === 'pending'
        ? reviewControlsMarkup('specification', reviewAction)
        : '';
    const reentry = review?.state === 'pending'
        ? ''
        : (hasValidCurrentSource ? specificationFeedbackContinuationMarkup(
            projection,
            actions,
            revisedRegistration,
            position,
        ) : '');
    return withSourceState(`${currentSource}<div class="max-w-4xl">${decisionCopy}<pre class="whitespace-pre-wrap break-words rounded-lg border border-slate-300 bg-white p-4 font-mono text-sm leading-6">${escapeWorkflowText(candidate.rendered_markdown ?? '')}</pre></div>
        ${controls}${reentry}`);
}

function findingMarkup(finding) {
    const message = typeof finding === 'string' ? finding : finding?.message;
    if (!message) return '';
    return `<li class="flex min-w-0 gap-2 text-sm leading-6 text-slate-700">
        <span class="material-symbols-outlined mt-0.5 shrink-0 text-amber-700" aria-hidden="true">warning</span>
        <span class="min-w-0 break-words">${escapeWorkflowText(message)}</span>
    </li>`;
}

function formatInspectedAt(value) {
    if (!value) return 'Not available';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short',
    });
}

function repositoryPanelMarkup(projection) {
    const repository = projection?.repository;
    if (!repository) {
        return `<p class="mb-4 text-sm text-slate-600">No repository is attached.</p>
            <button type="button" data-repository-action="attach" class="${BUTTON_PRIMARY}"><span class="material-symbols-outlined" aria-hidden="true">folder_open</span><span>Attach</span></button>`;
    }

    const shortSha = String(repository.head_sha ?? '').slice(0, 8);
    const location = repository.detached_head
        ? `Detached at ${shortSha}`
        : (repository.branch_name || 'Branch unavailable');
    const warnings = Array.isArray(repository.warnings) ? repository.warnings : [];
    return `<div class="grid min-w-0 gap-5 sm:grid-cols-2 lg:grid-cols-4">
        <div class="min-w-0">
            <p class="text-xs font-semibold uppercase text-slate-500">Path</p>
            <p class="mt-1 break-anywhere font-mono text-sm">${escapeWorkflowText(repository.worktree_path)}</p>
        </div>
        <div class="min-w-0">
            <p class="text-xs font-semibold uppercase text-slate-500">Revision</p>
            <p class="mt-1 break-words text-sm font-medium">${escapeWorkflowText(location)}</p>
            ${repository.detached_head ? '' : `<p class="mt-1 font-mono text-xs text-slate-500">${escapeWorkflowText(shortSha)}</p>`}
        </div>
        <div class="min-w-0">
            <p class="text-xs font-semibold uppercase text-slate-500">Working tree</p>
            <p class="mt-1 text-sm font-semibold ${repository.dirty ? 'text-amber-800' : 'text-emerald-700'}">${repository.dirty ? 'Dirty' : 'Clean'}</p>
        </div>
        <div class="min-w-0">
            <p class="text-xs font-semibold uppercase text-slate-500">Inspected</p>
            <p class="mt-1 break-words text-sm">${escapeWorkflowText(formatInspectedAt(repository.inspected_at))}</p>
        </div>
    </div>
    ${warnings.length > 0 ? `<ul class="mt-5 space-y-2 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3">${warnings.map(findingMarkup).join('')}</ul>` : ''}
    <div class="mt-5 flex flex-wrap gap-3">
        <button type="button" data-repository-action="attach" class="${BUTTON_SECONDARY}"><span class="material-symbols-outlined" aria-hidden="true">drive_file_move</span><span>Replace</span></button>
        <button type="button" data-repository-action="refresh" class="${BUTTON_SECONDARY}"><span class="material-symbols-outlined" aria-hidden="true">refresh</span><span>Refresh</span></button>
    </div>`;
}

function reviewObject(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
}

function reviewItems(value) {
    return Array.isArray(value) ? value : null;
}

function reviewValue(value) {
    if (value === null || value === undefined || value === '') return 'Not specified';
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    return String(value);
}

function sprintOwnerKindLabel(kind) {
    return {
        solo_project: 'Solo project',
        named_team: 'Named team',
        legacy_named_team: 'Legacy named team',
    }[kind] ?? 'Unknown owner';
}

const validatedSprintOwnerProjections = new WeakMap();

function sprintOwnerProjectionShape(value) {
    const owner = reviewObject(value);
    if (!owner || typeof owner !== 'object' || Array.isArray(owner)) return null;
    if (!['solo_project', 'named_team', 'legacy_named_team'].includes(owner.kind)) return null;
    if (typeof owner.key !== 'string' || !owner.key.trim()) return null;
    if (typeof owner.label !== 'string' || !owner.label.trim()) return null;
    if (typeof owner.display_label !== 'string' || !owner.display_label.trim()) return null;
    if (owner.display_label.includes(owner.key)) return null;
    if (owner.kind === 'solo_project') {
        if (!owner.display_label.startsWith('Solo operator for ')) return null;
        if (owner.label !== `[${owner.key}] ${owner.display_label}`) return null;
    } else if (owner.display_label !== owner.label) {
        return null;
    }
    return owner;
}

function sprintOwnerDisplayProjection(value, projectId) {
    const owner = sprintOwnerProjectionShape(value);
    if (!owner || validatedSprintOwnerProjections.get(owner) !== projectId) return null;
    return owner;
}

async function validateSprintOwnerProjection(value, projectId) {
    const owner = sprintOwnerProjectionShape(value);
    if (!owner || !Number.isInteger(projectId) || projectId < 1) return null;
    let expectedKey;
    if (owner.kind === 'solo_project') {
        expectedKey = `agileforge:sprint-owner:solo-project:v1:project:${projectId}`;
    } else {
        if (owner.kind === 'named_team' && (
            owner.label !== owner.label.trim()
            || sprintOwnerNamespaceCasefold(owner.label).startsWith('[agileforge:sprint-owner:')
        )) return null;
        if (!crypto?.subtle || typeof TextEncoder !== 'function') return null;
        const digest = await crypto.subtle.digest(
            'SHA-256',
            new TextEncoder().encode(owner.label),
        );
        const hex = Array.from(new Uint8Array(digest), (byte) => (
            byte.toString(16).padStart(2, '0')
        )).join('');
        const kind = owner.kind === 'named_team' ? 'named-team' : 'legacy-named-team';
        expectedKey = `agileforge:sprint-owner:${kind}:v1:sha256:${hex}`;
    }
    if (owner.key !== expectedKey) return null;
    validatedSprintOwnerProjections.set(owner, projectId);
    return owner;
}

function positiveInteger(value) {
    return Number.isSafeInteger(value) && value > 0;
}

function exactFactReference(decision, factType, factId, fingerprint) {
    const references = Array.isArray(decision?.fact_references)
        ? decision.fact_references
        : [];
    const matches = references.filter((reference) => reference?.fact_type === factType);
    return isSha256Fingerprint(fingerprint)
        && matches.length === 1
        && matches[0].fact_id === String(factId)
        && matches[0].fingerprint === fingerprint;
}

async function validateSprintStatusProjection(value, projectId) {
    const data = reviewObject(value);
    const sprint = reviewObject(data?.sprint);
    const plan = reviewObject(data?.accepted_plan);
    const acceptance = reviewObject(plan?.acceptance);
    const stories = reviewItems(plan?.selected_stories);
    const tasks = reviewItems(data?.tasks);
    if (
        data?.project_id !== projectId
        || !positiveInteger(sprint?.sprint_id)
        || !['planned', 'active', 'completed'].includes(sprint?.status)
        || plan?.sprint_id !== sprint.sprint_id
        || plan?.status !== sprint.status
        || typeof plan?.goal !== 'string'
        || !plan.goal.trim()
        || !positiveInteger(plan?.sprint_plan_artifact_id)
        || !positiveInteger(plan?.sprint_plan_artifact_decision_id)
        || !isSha256Fingerprint(plan?.plan_fingerprint)
        || !isSha256Fingerprint(plan?.candidate_set_fingerprint)
        || !isSha256Fingerprint(plan?.task_content_fingerprint)
        || typeof acceptance?.rationale !== 'string'
        || !acceptance.rationale.trim()
        || typeof acceptance?.reviewer !== 'string'
        || !acceptance.reviewer.trim()
        || typeof acceptance?.decided_at !== 'string'
        || !acceptance.decided_at.trim()
        || !stories?.length
        || !tasks
    ) return null;
    const owner = await validateSprintOwnerProjection(plan.owner, projectId);
    if (!owner) return null;

    const storyIds = new Set();
    let totalPoints = 0;
    let taskCount = 0;
    for (const story of stories) {
        if (
            !positiveInteger(story?.story_id)
            || storyIds.has(story.story_id)
            || typeof story?.story_item_id !== 'string'
            || !story.story_item_id.trim()
            || typeof story?.title !== 'string'
            || !story.title.trim()
            || !positiveInteger(story?.story_points)
            || !positiveInteger(story?.task_count)
        ) return null;
        storyIds.add(story.story_id);
        totalPoints += story.story_points;
        taskCount += story.task_count;
    }
    const taskIds = new Set();
    for (const task of tasks) {
        if (
            !positiveInteger(task?.task_id)
            || taskIds.has(task.task_id)
            || task?.sprint_id !== sprint.sprint_id
            || !storyIds.has(task?.story_id)
            || typeof task?.description !== 'string'
            || !task.description.trim()
            || typeof task?.status !== 'string'
            || !task.status.trim()
            || !isSha256Fingerprint(task?.fact_fingerprint)
        ) return null;
        taskIds.add(task.task_id);
    }
    if (
        plan.total_points !== totalPoints
        || plan.task_count !== taskCount
        || tasks.length !== taskCount
    ) return null;

    const start = data.start;
    if (sprint.status === 'planned') {
        if (start !== null) return null;
    } else if (
        !reviewObject(start)
        || start.sprint_id !== sprint.sprint_id
        || start.sprint_plan_artifact_id !== plan.sprint_plan_artifact_id
        || start.sprint_plan_artifact_decision_id
            !== plan.sprint_plan_artifact_decision_id
        || start.plan_fingerprint !== plan.plan_fingerprint
        || start.candidate_set_fingerprint !== plan.candidate_set_fingerprint
        || start.task_content_fingerprint !== plan.task_content_fingerprint
    ) return null;
    return data;
}

function sprintStartBinding(status, position, actions) {
    const sprint = reviewObject(status?.sprint);
    const plan = reviewObject(status?.accepted_plan);
    if (sprint?.status !== 'planned' || plan?.status !== 'planned') return null;
    const decisions = (Array.isArray(position?.decisions) ? position.decisions : [])
        .filter((decision) => (
            decision?.request_kind === 'start_sprint'
            && decision.category === 'available'
            && decision.recommendation_kind === 'required'
            && decision.reason_code === 'SPRINT_READY_TO_START'
        ));
    if (decisions.length !== 1) return null;
    const decision = decisions[0];
    const action = findDecisionAction(actions, decision);
    if (
        !action
        || action.endpoint !== 'sprint/start'
        || action.transport !== 'semantic'
        || !isSha256Fingerprint(decision.decision_fingerprint)
        || !exactFactReference(
            decision,
            'sprint_plan',
            plan.sprint_plan_artifact_id,
            plan.plan_fingerprint,
        )
        || !exactFactReference(
            decision,
            'candidate_set',
            status.project_id,
            plan.candidate_set_fingerprint,
        )
        || !exactFactReference(
            decision,
            'sprint_plan_tasks',
            sprint.sprint_id,
            plan.task_content_fingerprint,
        )
    ) return null;
    return {
        action: captureAction(action),
        decisionFingerprint: decision.decision_fingerprint,
        sprintId: sprint.sprint_id,
        sprintPlanArtifactId: plan.sprint_plan_artifact_id,
        sprintPlanArtifactDecisionId: plan.sprint_plan_artifact_decision_id,
        planFingerprint: plan.plan_fingerprint,
        candidateSetFingerprint: plan.candidate_set_fingerprint,
        taskContentFingerprint: plan.task_content_fingerprint,
    };
}

function sprintCorrectionBinding(status, position, actions) {
    if (status?.sprint?.status !== 'planned') return null;
    const decisions = (Array.isArray(position?.decisions) ? position.decisions : [])
        .filter((decision) => (
            decision?.request_kind === 'record_sprint_plan'
            && decision.category === 'available'
            && decision.recommendation_kind === 'optional_reentry'
            && decision.reason_code === 'SPRINT_PLAN_CORRECTION_AVAILABLE'
        ));
    if (decisions.length !== 1) return null;
    const action = findDecisionAction(actions, decisions[0]);
    return action?.endpoint === 'sprint/generate' ? captureAction(action) : null;
}

function sprintExecutionProjection(status, position, actions) {
    if (status?.sprint?.status !== 'active') return { kind: 'absent', items: [] };
    const taskById = new Map((Array.isArray(status?.tasks) ? status.tasks : [])
        .map((task) => [task.task_id, task]));
    const decisions = (Array.isArray(position?.decisions) ? position.decisions : [])
        .filter((decision) => decision?.request_kind === 'complete_task');
    const items = [];
    const projectedTaskIds = new Set();
    for (const decision of decisions) {
        const taskId = Number.parseInt(
            String(decision?.instance_key ?? '').replace(/^task:/, ''),
            10,
        );
        const action = findDecisionAction(actions, decision);
        const task = taskById.get(taskId);
        if (
            !positiveInteger(taskId)
            || projectedTaskIds.has(taskId)
            || decision.instance_key !== `task:${taskId}`
            || decision.category !== 'available'
            || !['NEXT_TASK_READY', 'IN_PROGRESS_TASK_REQUIRED'].includes(
                decision.reason_code,
            )
            || !action
            || action.endpoint !== 'sprint/task/complete'
            || !exactFactReference(
                decision,
                'task',
                taskId,
                task?.fact_fingerprint,
            )
            || !task
            || ['Done', 'Cancelled'].includes(task.status)
        ) return { kind: 'error', items: [] };
        projectedTaskIds.add(taskId);
        items.push({ task, action: captureAction(action), decision });
    }
    items.sort((left, right) => left.task.task_id - right.task.task_id);
    return { kind: items.length ? 'ready' : 'absent', items };
}

function sprintOwnerProjection(context = {}) {
    const owner = sprintOwnerDisplayProjection(
        context?.sprintCandidates?.sprint_owner,
        context?.sprintCandidates?.project_id,
    );
    if (!owner || owner.kind !== 'solo_project') return null;
    if (owner.named_team_override_allowed !== true) return null;
    return owner;
}

function sprintCapacityPoints(value) {
    const text = String(value ?? '');
    if (!/^[1-9]\d*$/.test(text)) return null;
    const points = Number(text);
    return Number.isSafeInteger(points) ? points : null;
}

function sprintCapacityProjectionPoints(value) {
    if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1) {
        return null;
    }
    return value;
}

function sprintCapacityProjection(context = {}) {
    const capacity = reviewObject(context?.sprintCandidates?.capacity);
    if (!capacity) return null;
    const rationale = capacity.rationale;
    if (typeof rationale !== 'string' || !rationale.trim()) return null;
    if (capacity.status === 'recommended') {
        const points = sprintCapacityProjectionPoints(
            capacity.recommended_max_story_points,
        );
        if (points === null || capacity.source !== 'project_metrics') return null;
        return { status: 'recommended', points, source: capacity.source, rationale };
    }
    if (capacity.status === 'manual_required' || capacity.status === 'unavailable') {
        if (capacity.recommended_max_story_points !== null || capacity.source !== null) return null;
        return { status: capacity.status, points: null, source: null, rationale };
    }
    return null;
}

function sprintCapacityFields(context, value) {
    const capacity = sprintCapacityProjection(context);
    const maxStoryPoints = sprintCapacityPoints(value);
    if (!capacity || capacity.status === 'unavailable' || maxStoryPoints === null) {
        throw new Error('Enter a positive whole-number Maximum story points value before generating a Sprint plan.');
    }
    return { max_story_points: maxStoryPoints };
}

function syncSprintCapacityButton(form) {
    const input = form?.querySelector?.('[name="max_story_points"]');
    const submit = form?.querySelector?.('button[type="submit"]');
    if (!input || !submit || form.dataset.submitting === 'true') return false;
    const valid = sprintCapacityPoints(input.value) !== null;
    submit.disabled = !valid;
    return valid;
}

const SPRINT_OWNER_CASEFOLD_EXPANSIONS = new Map([
    ['ß', 'ss'],
    ['ſ', 's'],
    ['ﬀ', 'ff'],
    ['ﬁ', 'fi'],
    ['ﬂ', 'fl'],
    ['ﬃ', 'ffi'],
    ['ﬄ', 'ffl'],
    ['ﬅ', 'st'],
    ['ﬆ', 'st'],
]);

function sprintOwnerNamespaceCasefold(value) {
    return Array.from(
        value.toLowerCase(),
        (character) => SPRINT_OWNER_CASEFOLD_EXPANSIONS.get(character) ?? character,
    ).join('');
}

function sprintTeamOverrideFields(rawValue) {
    const teamName = String(rawValue ?? '').trim();
    if (!teamName) return {};
    if (sprintOwnerNamespaceCasefold(teamName).startsWith('[agileforge:sprint-owner:')) {
        throw new Error('A named Team override cannot use the reserved Sprint-owner namespace.');
    }
    return { team_name: teamName };
}

function reviewListMarkup(label, values) {
    const items = reviewItems(values);
    if (!items) return '';
    const content = items.length
        ? items.map((item) => `<li>${escapeWorkflowText(reviewValue(item))}</li>`).join('')
        : '<li>None</li>';
    return `<div><p class="text-xs font-semibold uppercase text-slate-500">${escapeWorkflowText(label)}</p><ul class="mt-1 list-disc space-y-1 pl-5 text-sm">${content}</ul></div>`;
}

function specificationEvidenceMarkup(values) {
    const items = reviewItems(values);
    if (!items) return '';
    return `<section class="space-y-3"><h4 class="text-xs font-semibold uppercase text-slate-500">Specification evidence</h4>${items.map((rawItem) => {
        const item = reviewObject(rawItem);
        if (!item) return '';
        return `<div class="rounded-md border border-slate-200 bg-slate-50 p-3">
            <p class="font-semibold">${escapeWorkflowText(reviewValue(item.title))}</p>
            <p class="mt-1 text-sm leading-6">${escapeWorkflowText(reviewValue(item.statement))}</p>
            <dl class="mt-2 grid gap-2 text-sm sm:grid-cols-2">
                <div><dt class="font-semibold">Level</dt><dd>${escapeWorkflowText(reviewValue(item.level))}</dd></div>
                <div><dt class="font-semibold">Verification</dt><dd>${escapeWorkflowText(reviewValue(item.verification_method))}</dd></div>
            </dl>
            <div class="mt-2">${reviewListMarkup('Acceptance criteria', item.acceptance_criteria)}</div>
        </div>`;
    }).join('')}</section>`;
}

function backlogItemMarkup(value) {
    const item = reviewObject(value);
    if (!item) return '';
    return `<section class="space-y-3 rounded-md border border-slate-200 p-3">
        <div><p class="text-xs font-semibold uppercase text-slate-500">Requirement</p><p class="mt-1 text-sm leading-6">${escapeWorkflowText(reviewValue(item.requirement))}</p></div>
        <dl class="grid gap-2 text-sm sm:grid-cols-2">
            <div><dt class="font-semibold">Priority</dt><dd>${escapeWorkflowText(reviewValue(item.priority))}</dd></div>
            <div><dt class="font-semibold">Value driver</dt><dd>${escapeWorkflowText(reviewValue(item.value_driver))}</dd></div>
            <div><dt class="font-semibold">Estimated effort</dt><dd>${escapeWorkflowText(reviewValue(item.estimated_effort))}</dd></div>
        </dl>
        <div><p class="font-semibold text-sm">Justification</p><p class="text-sm leading-6">${escapeWorkflowText(reviewValue(item.justification))}</p></div>
        ${item.technical_note ? `<div><p class="font-semibold text-sm">Implementation note</p><p class="text-sm leading-6">${escapeWorkflowText(reviewValue(item.technical_note))}</p></div>` : ''}
        ${specificationEvidenceMarkup(item.specification_evidence)}
    </section>`;
}

const INVEST_DIMENSION_KEYS = [
    'independent',
    'negotiable',
    'valuable',
    'estimable',
    'small',
    'testable',
];

function isWellFormedInvestDimension(rawDim) {
    if (typeof rawDim !== 'object' || rawDim === null || Array.isArray(rawDim)) {
        return false;
    }
    const keys = Object.keys(rawDim);
    if (keys.length !== 3) return false;
    for (const key of ['result', 'rationale', 'evidence']) {
        if (!keys.includes(key)) return false;
    }
    if (typeof rawDim.result !== 'string') return false;
    if (rawDim.result !== 'pass' && rawDim.result !== 'concern' && rawDim.result !== 'fail') {
        return false;
    }
    if (typeof rawDim.rationale !== 'string' || rawDim.rationale.trim().length === 0) {
        return false;
    }
    if (typeof rawDim.evidence !== 'string' || rawDim.evidence.trim().length === 0) {
        return false;
    }
    return true;
}

function isWellFormedInvestAssessment(rawAssessment) {
    if (typeof rawAssessment !== 'object' || rawAssessment === null || Array.isArray(rawAssessment)) {
        return false;
    }
    const keys = Object.keys(rawAssessment);
    if (keys.length !== INVEST_DIMENSION_KEYS.length) return false;
    for (const key of INVEST_DIMENSION_KEYS) {
        if (!keys.includes(key)) return false;
        if (!isWellFormedInvestDimension(rawAssessment[key])) {
            return false;
        }
    }
    return true;
}

function isStoryReviewAcceptable(review) {
    const candidate = reviewObject(review?.candidate);
    if (!candidate) return false;
    const items = reviewItems(candidate.story_items);
    if (!items || items.length === 0) return false;
    for (const item of items) {
        const story = reviewObject(item);
        if (!story || !isWellFormedInvestAssessment(story.invest_assessment)) {
            return false;
        }
        if (typeof story.effort_rationale !== 'string' || !story.effort_rationale.trim()) {
            return false;
        }
        if (typeof story.order_rationale !== 'string' || !story.order_rationale.trim()) {
            return false;
        }
    }
    return true;
}

function investDimensionResultBadge(result) {
    if (result === 'pass') {
        return '<span class="inline-flex items-center rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-semibold text-emerald-800">Pass</span>';
    }
    if (result === 'concern') {
        return '<span class="inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-800">Concern</span>';
    }
    if (result === 'fail') {
        return '<span class="inline-flex items-center rounded-full bg-rose-100 px-2 py-0.5 text-xs font-semibold text-rose-800">Fail</span>';
    }
    return `<span class="inline-flex items-center rounded-full bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-800">${escapeWorkflowText(typeof result === 'string' && result ? result : 'Unknown')}</span>`;
}

function investAssessmentMarkup(value) {
    const assessment = reviewObject(value);
    const isValid = isWellFormedInvestAssessment(assessment);
    const dimensions = [
        ['Independent', assessment?.independent],
        ['Negotiable', assessment?.negotiable],
        ['Valuable', assessment?.valuable],
        ['Estimable', assessment?.estimable],
        ['Small', assessment?.small],
        ['Testable', assessment?.testable],
    ];

    if (!isValid) {
        return `<section class="space-y-2 rounded-md border border-rose-200 bg-rose-50 p-3 text-xs text-rose-800" data-invest-assessment="invalid">
        <div class="flex items-center gap-2 font-semibold">
            <span class="inline-flex items-center rounded-full bg-rose-200 px-2 py-0.5 text-xs font-semibold text-rose-900">Quality Assessment Incomplete</span>
            <span>Required INVEST quality assessment is missing or malformed. Acceptance is disabled.</span>
        </div>
        ${assessment ? `<div class="mt-2 grid gap-2 sm:grid-cols-2">
            ${dimensions.map(([name, rawDim]) => {
                const dim = reviewObject(rawDim);
                const dimValid = isWellFormedInvestDimension(dim);
                return `<div class="rounded-md border ${dimValid ? 'border-slate-200 bg-white' : 'border-rose-300 bg-rose-100/50'} p-2.5 space-y-1 text-xs">
                    <div class="flex items-center justify-between">
                        <strong class="font-semibold text-slate-900">${escapeWorkflowText(name)}</strong>
                        ${dimValid ? investDimensionResultBadge(dim.result) : '<span class="inline-flex items-center rounded-full bg-rose-200 px-2 py-0.5 text-xs font-semibold text-rose-900">Missing / Invalid</span>'}
                    </div>
                    <p class="text-slate-700"><strong>Rationale:</strong> ${typeof dim?.rationale === 'string' && dim.rationale.trim() ? escapeWorkflowText(dim.rationale) : '<em class="text-rose-700">Missing / Invalid rationale</em>'}</p>
                    <p class="text-slate-600"><strong>Evidence:</strong> ${typeof dim?.evidence === 'string' && dim.evidence.trim() ? escapeWorkflowText(dim.evidence) : '<em class="text-rose-700">Missing / Invalid evidence</em>'}</p>
                </div>`;
            }).join('')}
        </div>` : ''}
    </section>`;
    }

    return `<section class="space-y-2" data-invest-assessment="true">
        <h4 class="text-xs font-semibold uppercase text-slate-500">INVEST assessment</h4>
        <div class="grid gap-2 sm:grid-cols-2">
            ${dimensions.map(([name, rawDim]) => {
                const dim = reviewObject(rawDim);
                return `<div class="rounded-md border border-slate-200 bg-slate-50 p-2.5 space-y-1 text-xs">
                    <div class="flex items-center justify-between">
                        <strong class="font-semibold text-slate-900">${escapeWorkflowText(name)}</strong>
                        ${investDimensionResultBadge(dim.result)}
                    </div>
                    <p class="text-slate-700"><strong>Rationale:</strong> ${escapeWorkflowText(dim.rationale)}</p>
                    <p class="text-slate-600"><strong>Evidence:</strong> ${escapeWorkflowText(dim.evidence)}</p>
                </div>`;
            }).join('')}
        </div>
    </section>`;
}

function dependencyCandidatesMarkup(values) {
    const items = reviewItems(values);
    if (!items) return '';
    if (items.length === 0) {
        return `<section class="space-y-1">
            <h4 class="text-xs font-semibold uppercase text-slate-500">Proposed dependencies</h4>
            <p class="text-xs text-slate-500 italic">None proposed</p>
        </section>`;
    }
    return `<section class="space-y-2">
        <h4 class="text-xs font-semibold uppercase text-slate-500">Proposed dependencies</h4>
        <ul class="list-disc space-y-1 pl-5 text-sm">
            ${items.map((rawItem) => {
                const item = reviewObject(rawItem);
                if (!item) return '';
                return `<li><strong>Prerequisite:</strong> ${escapeWorkflowText(reviewValue(item.prerequisite_ref))} (${escapeWorkflowText(reviewValue(item.confidence))}) &mdash; ${escapeWorkflowText(reviewValue(item.reason))}</li>`;
            }).join('')}
        </ul>
    </section>`;
}

function storyItemMarkup(value) {
    const story = reviewObject(value);
    if (!story) return '';
    const pointsText = story.story_points != null ? ` (derived: ${escapeWorkflowText(reviewValue(story.story_points))} pts)` : '';
    return `<section class="space-y-3 rounded-md border border-slate-200 p-3">
        <div><p class="text-xs font-semibold uppercase text-slate-500">Story</p><p class="mt-1 font-semibold">${escapeWorkflowText(reviewValue(story.story_title ?? story.title))}</p></div>
        <p class="text-sm leading-6">${escapeWorkflowText(reviewValue(story.statement))}</p>
        <div class="flex flex-wrap gap-4 text-sm">
            <p><strong>Persona:</strong> ${escapeWorkflowText(reviewValue(story.persona))}</p>
            ${story.rank ? `<p><strong>Story order within PBI:</strong> ${escapeWorkflowText(reviewValue(story.order ?? '-'))} <span class="text-slate-500">(Derived rank: ${escapeWorkflowText(reviewValue(story.rank))})</span></p>` : ''}
            ${story.estimated_effort ? `<p><strong>Estimated effort:</strong> ${escapeWorkflowText(reviewValue(story.estimated_effort))}${pointsText}</p>` : ''}
        </div>
        ${story.order_rationale || story.effort_rationale ? `<div class="rounded-md border border-slate-200 bg-slate-50 p-2.5 space-y-1 text-xs">
            ${story.order_rationale ? `<p class="text-slate-700"><strong>Order rationale:</strong> ${escapeWorkflowText(reviewValue(story.order_rationale))}</p>` : ''}
            ${story.effort_rationale ? `<p class="text-slate-700"><strong>Effort rationale:</strong> ${escapeWorkflowText(reviewValue(story.effort_rationale))}</p>` : ''}
        </div>` : ''}
        ${reviewListMarkup('Acceptance criteria', story.acceptance_criteria)}
        ${specificationEvidenceMarkup(story.specification_evidence)}
        ${investAssessmentMarkup(story.invest_assessment)}
        ${reviewListMarkup('Research caveats', story.research_caveats)}
        ${dependencyCandidatesMarkup(story.dependency_candidates)}
        ${story.reason_for_selection ? `<p class="text-sm"><strong>Reason for selection:</strong> ${escapeWorkflowText(reviewValue(story.reason_for_selection))}</p>` : ''}
    </section>`;
}

function taskMarkup(value) {
    const task = reviewObject(value);
    if (!task) return '';
    return `<section class="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3">
        <p class="font-semibold">${escapeWorkflowText(reviewValue(task.description))}</p>
        <p class="text-sm"><strong>Kind:</strong> ${escapeWorkflowText(reviewValue(task.task_kind))}</p>
        ${reviewListMarkup('Checklist', task.checklist_items)}
        ${specificationEvidenceMarkup(task.specification_evidence)}
    </section>`;
}

function backlogReviewMarkup(candidate) {
    const items = reviewItems(candidate.backlog_items);
    if (!items) return '';
    return `<div class="space-y-4">${items.map(backlogItemMarkup).join('')}
        <p class="text-sm"><strong>Complete:</strong> ${escapeWorkflowText(reviewValue(candidate.is_complete))}</p>
        ${reviewListMarkup('Clarifying questions', candidate.clarifying_questions)}
    </div>`;
}

function roadmapReviewMarkup(candidate) {
    const releases = reviewItems(candidate.roadmap_releases);
    if (!releases) return '';
    return `<div class="space-y-4">
        <p class="text-sm leading-6"><strong>Summary:</strong> ${escapeWorkflowText(reviewValue(candidate.roadmap_summary))}</p>
        ${releases.map((rawRelease) => {
            const release = reviewObject(rawRelease);
            if (!release) return '';
            const items = reviewItems(release.backlog_items);
            if (!items) return '';
            return `<section class="space-y-3"><h4 class="font-semibold">Release: ${escapeWorkflowText(reviewValue(release.release_name))}</h4>
                <p class="text-sm"><strong>Theme:</strong> ${escapeWorkflowText(reviewValue(release.theme))}</p>
                <p class="text-sm"><strong>Focus:</strong> ${escapeWorkflowText(reviewValue(release.focus_area))}</p>
                <p class="text-sm leading-6"><strong>Reasoning:</strong> ${escapeWorkflowText(reviewValue(release.reasoning))}</p>
                <div class="space-y-3">${items.map(backlogItemMarkup).join('')}</div>
            </section>`;
        }).join('')}
        <p class="text-sm"><strong>Complete:</strong> ${escapeWorkflowText(reviewValue(candidate.is_complete))}</p>
        ${reviewListMarkup('Clarifying questions', candidate.clarifying_questions)}
    </div>`;
}

function storyReviewMarkup(review, candidate) {
    const lineage = reviewObject(review.lineage);
    const items = reviewItems(candidate.story_items);
    if (!lineage || !items) return '';
    return `<div class="space-y-4"><section><h4 class="mb-2 font-semibold">Source requirement</h4>${backlogItemMarkup(lineage.backlog_item)}</section>
        ${items.map(storyItemMarkup).join('')}
        <p class="text-sm"><strong>Complete:</strong> ${escapeWorkflowText(reviewValue(candidate.is_complete))}</p>
        ${reviewListMarkup('Clarifying questions', candidate.clarifying_questions)}
    </div>`;
}

function sprintReviewMarkup(candidate, projectId) {
    const stories = reviewItems(candidate.selected_stories);
    if (!stories) return '';
    const owner = sprintOwnerDisplayProjection(candidate.sprint_owner, projectId);
    if (!owner) return '';
    return `<div class="space-y-4">
        <p class="text-sm"><strong>Sprint owner:</strong> ${escapeWorkflowText(sprintOwnerKindLabel(owner.kind))} — ${escapeWorkflowText(owner.display_label)}</p>
        <p class="text-sm"><strong>Sprint goal:</strong> ${escapeWorkflowText(reviewValue(candidate.sprint_goal))}</p>
        ${stories.map((rawStory) => {
            const story = reviewObject(rawStory);
            const tasks = reviewItems(story?.tasks);
            if (!story || !tasks) return '';
            return `${storyItemMarkup(story)}<div class="mt-3 space-y-3"><h4 class="font-semibold">Tasks</h4>${tasks.map(taskMarkup).join('')}</div>`;
        }).join('')}
    </div>`;
}

function planningReviewContentMarkup(review) {
    const candidate = reviewObject(review?.candidate);
    if (!candidate) return '';
    if (review.phase === 'backlog') return backlogReviewMarkup(candidate);
    if (review.phase === 'roadmap') return roadmapReviewMarkup(candidate);
    if (review.phase === 'story') return storyReviewMarkup(review, candidate);
    if (review.phase === 'sprint_plan') return sprintReviewMarkup(candidate, review.project_id);
    return '';
}

function planningReviewAcceptButtonMarkup(scope, index, isAcceptable) {
    if (isAcceptable) {
        return `<button type="button" data-planning-review="${escapeWorkflowText(scope)}" data-review-index="${index}" data-review-decision="accepted" class="${BUTTON_PRIMARY}">Accept</button>`;
    }
    return `<button type="button" data-planning-review="${escapeWorkflowText(scope)}" data-review-index="${index}" data-review-decision="accepted" disabled class="${BUTTON_PRIMARY} opacity-50 cursor-not-allowed" title="Acceptance disabled: required INVEST, sizing, or ordering evidence is missing or malformed">Accept</button>`;
}

function planningReviewCardMarkup(label, selected, scope, index = 0) {
    if (!selected?.review || !selected?.binding || selected.review.review?.state !== 'pending') return '';
    const content = planningReviewContentMarkup(selected.review);
    if (!content) return '';
    const isStory = scope === 'story';
    const isAcceptable = !isStory || isStoryReviewAcceptable(selected.review);

    const candidate = reviewObject(selected.review.candidate);
    const correctedIdentity = scope === 'backlog'
        && Number.isInteger(candidate?.backlog_artifact_id)
        && Number.isInteger(candidate?.version_number)
        && Number.isInteger(candidate?.supersedes_backlog_artifact_id)
        ? `Corrected Backlog candidate v${escapeWorkflowText(candidate.version_number)} (#${escapeWorkflowText(candidate.backlog_artifact_id)}), replacing #${escapeWorkflowText(candidate.supersedes_backlog_artifact_id)}`
        : null;
    const tabIndex = scope === 'backlog' ? ' tabindex="-1"' : '';

    return `<article class="rounded-lg border border-slate-300 bg-white p-4" data-planning-review-card="${escapeWorkflowText(scope)}"${tabIndex}>
        <h3 class="text-sm font-semibold">${correctedIdentity ? `${correctedIdentity} - ` : ''}${escapeWorkflowText(label)}</h3>
        ${!isAcceptable ? `<div class="mt-2 rounded-md border border-rose-300 bg-rose-50 p-3 text-xs text-rose-800 font-medium" data-review-error="invalid-story-evidence">Story proposal cannot be accepted: required INVEST, sizing, or ordering evidence is missing or malformed. Acceptance is disabled.</div>` : ''}
        <div class="mt-3 space-y-4">${content}</div>
        <div class="mt-4 flex flex-wrap gap-2">
            ${planningReviewAcceptButtonMarkup(scope, index, isAcceptable)}
            <button type="button" data-planning-review="${escapeWorkflowText(scope)}" data-review-index="${index}" data-review-decision="feedback" class="${BUTTON_SECONDARY}">Request changes</button>
            <button type="button" data-planning-review="${escapeWorkflowText(scope)}" data-review-index="${index}" data-review-decision="rejected" class="${BUTTON_DANGER}">Reject</button>
        </div>
    </article>`;
}

const BACKLOG_FEEDBACK_MODES = {
    BACKLOG_REVISION_REQUIRED: {
        mode: 'revision-ready', category: 'available', recommendation: 'recovery', attempt: false,
        status: 'Backlog Feedback recorded',
    },
    BACKLOG_GENERATION_ACTIVE: {
        mode: 'active', category: 'waiting', recommendation: 'required', attempt: false,
        status: 'Backlog correction is in progress. The recorded Feedback remains current.',
    },
    BACKLOG_GENERATION_FAILED: {
        mode: 'failed-retry', category: 'available', recommendation: 'recovery', attempt: true,
        status: 'Backlog correction failed. No corrected candidate was produced; the recorded Feedback remains current.',
    },
    BACKLOG_GENERATION_RECOVERY_REQUIRED: {
        mode: 'expired-recovery', category: 'available', recommendation: 'recovery', attempt: true,
        status: 'The previous Backlog correction attempt expired. The recorded Feedback remains current and can be retried.',
    },
};

function isConcreteReviewIdentity(id, fingerprint) {
    return Number.isInteger(id) && id > 0
        && typeof fingerprint === 'string' && Boolean(fingerprint.trim());
}

function isCanonicalFactReferenceId(value) {
    return typeof value === 'string' && /^[1-9][0-9]*$/.test(value);
}

function isBacklogFeedbackContinuationDecision(decision) {
    return decision?.node_id === 'backlog.generate'
        && decision.instance_key === null
        && decision.request_kind === 'record_backlog_draft'
        && Boolean(BACKLOG_FEEDBACK_MODES[decision.reason_code]);
}

function backlogFeedbackContinuationProjection(state) {
    const backlog = reviewObject(state?.planningReviews?.backlog);
    if (backlog !== null && Object.keys(backlog).length === 0) {
        return { kind: 'absent' };
    }
    if (backlog === null
        || !Object.prototype.hasOwnProperty.call(backlog, 'continuation')) {
        return { kind: 'error', code: 'BACKLOG_FEEDBACK_PROJECTION_INVALID' };
    }
    const continuation = reviewObject(backlog.continuation);
    const binding = reviewObject(continuation?.binding);
    const review = reviewObject(continuation?.review);
    const candidate = reviewObject(review?.candidate);
    const lineage = reviewObject(review?.lineage);
    const specification = reviewObject(lineage?.specification);
    const productGoal = reviewObject(lineage?.product_goal);
    if (!binding || !review || !candidate || !specification || !productGoal
        || binding.node_id !== 'backlog.generate'
        || binding.instance_key !== null
        || typeof binding.decision_fingerprint !== 'string'
        || !binding.decision_fingerprint
        || review.phase !== 'backlog'
        || review.review?.state !== 'feedback'
        || typeof review.review?.rationale !== 'string'
        || !review.review.rationale.trim()) {
        return { kind: 'error', code: 'BACKLOG_FEEDBACK_PROJECTION_INVALID' };
    }
    const decisions = Array.isArray(state?.position?.decisions) ? state.position.decisions : [];
    const continuationDecisions = decisions.filter(isBacklogFeedbackContinuationDecision);
    if (continuationDecisions.length !== 1) return { kind: 'error', code: 'BACKLOG_FEEDBACK_PROJECTION_INVALID' };
    const decision = continuationDecisions[0];
    if (decision.decision_fingerprint !== binding.decision_fingerprint) {
        return { kind: 'error', code: 'BACKLOG_FEEDBACK_PROJECTION_INVALID' };
    }
    const mode = BACKLOG_FEEDBACK_MODES[decision?.reason_code];
    if (!mode || decision.node_id !== 'backlog.generate'
        || decision.instance_key !== null
        || decision.request_kind !== 'record_backlog_draft'
        || decision.category !== mode.category
        || decision.recommendation_kind !== mode.recommendation) {
        return { kind: 'error', code: 'BACKLOG_FEEDBACK_PROJECTION_INVALID' };
    }
    const references = Array.isArray(decision.fact_references) ? decision.fact_references : [];
    const expected = {
        backlog: [candidate.backlog_artifact_id, candidate.artifact_fingerprint],
        specification: [specification.spec_version_id, specification.spec_hash],
        product_goal: [productGoal.product_goal_artifact_id, productGoal.product_goal_fingerprint],
    };
    if (Object.values(expected).some(([id, fingerprint]) => !isConcreteReviewIdentity(id, fingerprint))) {
        return { kind: 'error', code: 'BACKLOG_FEEDBACK_PROJECTION_INVALID' };
    }
    const allowed = mode.attempt ? new Set([...Object.keys(expected), 'node_attempt']) : new Set(Object.keys(expected));
    if (references.some((reference) => !allowed.has(reference?.fact_type))) {
        return { kind: 'error', code: 'BACKLOG_FEEDBACK_PROJECTION_INVALID' };
    }
    for (const [factType, [factId, fingerprint]] of Object.entries(expected)) {
        const matches = references.filter((reference) => reference?.fact_type === factType);
        if (matches.length !== 1
            || !isCanonicalFactReferenceId(matches[0].fact_id)
            || matches[0].fact_id !== String(factId)
            || matches[0].fingerprint !== fingerprint) {
            return { kind: 'error', code: 'BACKLOG_FEEDBACK_PROJECTION_INVALID' };
        }
    }
    const attempts = references.filter((reference) => reference?.fact_type === 'node_attempt');
    if (attempts.length !== (mode.attempt ? 1 : 0)
        || (mode.attempt && (!isCanonicalFactReferenceId(attempts[0].fact_id)
            || typeof attempts[0].fingerprint !== 'string'
            || !attempts[0].fingerprint.trim()))) {
        return { kind: 'error', code: 'BACKLOG_FEEDBACK_PROJECTION_INVALID' };
    }
    return { kind: 'display', mode: mode.mode, decision, candidate, review };
}

function backlogCorrectionActionBinding(state, continuation) {
    if (continuation?.kind === 'absent') return { kind: 'unavailable', reason: 'absent' };
    if (continuation?.kind !== 'display') return { kind: 'error', code: 'BACKLOG_CORRECTION_ACTION_INVALID' };
    if (continuation.mode === 'active') return { kind: 'unavailable', reason: 'active' };
    const matches = (Array.isArray(state?.actions) ? state.actions : []).filter((action) => (
        action?.node_id === 'backlog.generate'
        && action.instance_key === null
        && action.request_kind === 'record_backlog_draft'
        && action.endpoint === 'backlog/generate'
        && action.transport === 'semantic'
    ));
    return matches.length === 1
        ? { kind: 'ready', action: matches[0] }
        : { kind: 'error', code: 'BACKLOG_CORRECTION_ACTION_INVALID' };
}

function backlogCorrectionActionDetails(action) {
    if (!action) return null;
    return {
        label: 'Regenerate Backlog from feedback',
        busyLabel: 'Regenerating Backlog from feedback...',
        description: 'Generate a corrected Product Backlog from the recorded Feedback.',
        icon: 'refresh',
    };
}

function backlogFeedbackContinuationMarkup(continuation, actionBinding) {
    if (continuation?.kind !== 'display') {
        return `<section data-backlog-feedback-projection-error="true" role="alert" tabindex="-1" class="rounded-lg border border-red-300 bg-red-50 p-4 text-sm text-red-800">Backlog Feedback state is unavailable. Reload before taking another action.</section>`;
    }
    const candidate = continuation.candidate;
    const details = actionBinding?.kind === 'ready'
        ? backlogCorrectionActionDetails(actionBinding.action)
        : null;
    const action = details ? `<div data-backlog-correction-action="true" data-delivery-generation-action="record_backlog_draft" ${deliveryActionBindingAttributes(actionBinding.action)} tabindex="-1" class="mt-4">
        <p class="mb-3 text-sm leading-6 text-slate-600">${details.description}</p>
        <button type="button" data-direct-action="record_backlog_draft" class="${BUTTON_PRIMARY}">
            <span class="material-symbols-outlined" aria-hidden="true">${details.icon}</span>
            <span data-delivery-action-label="true">${details.label}</span>
        </button>
        <p data-delivery-action-status="true" hidden role="status" aria-live="polite" aria-atomic="true" class="mt-3 text-sm leading-6 text-slate-700"></p>
    </div>` : '';
    const actionError = actionBinding?.kind === 'error'
        ? '<p data-backlog-feedback-projection-error="true" role="alert" tabindex="-1" class="mt-4 text-sm text-red-800">Backlog correction action is unavailable. Reload before taking another action.</p>'
        : '';
    return `<section data-backlog-feedback-continuation="true" tabindex="-1" class="rounded-lg border border-amber-300 bg-amber-50 p-4">
        <h3 class="text-sm font-semibold text-amber-950">${escapeWorkflowText(BACKLOG_FEEDBACK_MODES[continuation.decision.reason_code].status)}</h3>
        <p class="mt-2 text-sm font-semibold text-slate-800">Backlog candidate v${escapeWorkflowText(candidate.version_number)} (#${escapeWorkflowText(candidate.backlog_artifact_id)})</p>
        <div class="mt-3 space-y-4">${backlogReviewMarkup(candidate)}</div>
        <p class="mt-4 text-sm leading-6 text-amber-950"><strong>Feedback:</strong> ${escapeWorkflowText(continuation.review.review.rationale)}</p>
        ${actionError}${action}
    </section>`;
}

function backlogPendingReviewIsValid(backlog) {
    const binding = reviewObject(backlog?.binding);
    return binding !== null
        && typeof binding.decision_fingerprint === 'string'
        && Boolean(binding.decision_fingerprint.trim())
        && binding.instance_key === null
        && reviewObject(backlog?.review) !== null
        && backlog.review.review?.state === 'pending';
}

function backlogProjectionErrorMarkup() {
    return '<section data-backlog-feedback-projection-error="true" role="alert" tabindex="-1" class="rounded-lg border border-red-300 bg-red-50 p-4 text-sm text-red-800">Backlog review state is unavailable. Reload before taking another action.</section>';
}

function deliveryActionBindingAttributes(action) {
    return [
        `data-delivery-action-node="${escapeWorkflowText(action.node_id)}"`,
        `data-delivery-action-instance="${escapeWorkflowText(action.instance_key ?? '')}"`,
        `data-delivery-action-has-instance="${action.instance_key === null || action.instance_key === undefined ? 'false' : 'true'}"`,
        `data-delivery-action-endpoint="${escapeWorkflowText(action.endpoint)}"`,
        `data-delivery-action-transport="${escapeWorkflowText(action.transport ?? '')}"`,
    ].join(' ');
}

function deliveryGenerationActionDetails(action, position = {}, reviews = {}, context = {}) {
    const config = DELIVERY_ACTION_CONFIG[action?.request_kind];
    if (!config) return null;
    if (action.request_kind !== 'record_story_draft') {
        return {
            label: config.label,
            busyLabel: config.busyLabel,
            description: config.description,
            icon: config.icon,
        };
    }
    const instanceKey = action.instance_key ?? '';
    const pbiId = instanceKey.startsWith('backlog_item:')
        ? instanceKey.slice('backlog_item:'.length)
        : (instanceKey || null);

    const decisions = Array.isArray(position?.decisions) ? position.decisions : [];
    const decision = decisions.find(
        (d) => d.node_id === action.node_id && d.instance_key === action.instance_key,
    );
    const reasonCode = decision?.reason_code ?? '';
    const isRevision = reasonCode === 'STORY_REVISION_REQUIRED' || decision?.recommendation_kind === 'recovery';
    const isCorrection = reasonCode === 'STORY_CORRECTION_AVAILABLE' || decision?.recommendation_kind === 'optional_reentry';

    let correctionBinding = null;
    if (isCorrection) {
        const references = Array.isArray(decision?.fact_references)
            ? decision.fact_references
            : [];
        const storyReferences = references.filter((reference) => reference?.fact_type === 'story');
        const artifactId = storyReferences.length === 1
            ? Number.parseInt(storyReferences[0].fact_id, 10)
            : null;
        if (
            action.endpoint !== 'story/correct'
            || !isSha256Fingerprint(decision?.decision_fingerprint)
            || !Number.isInteger(artifactId)
            || artifactId <= 0
            || !isSha256Fingerprint(storyReferences[0]?.fingerprint)
        ) return null;
        correctionBinding = {
            expectedDecision: decision.decision_fingerprint,
            acceptedStoryArtifactId: artifactId,
            acceptedStoryArtifactFingerprint: storyReferences[0].fingerprint,
        };
    }

    const acceptedStories = Array.isArray(context?.storyDependencies?.stories)
        ? context.storyDependencies.stories
        : [];
    const isBindingRecovery = isCorrection && acceptedStories.some((story) => (
        (
            story?.backlog_item_id === pbiId
            || Number(story?.source_story_artifact_id)
                === correctionBinding.acceptedStoryArtifactId
        )
        && (
            (Array.isArray(story?.readiness_blockers)
                && story.readiness_blockers.includes('STORY_ITEM_BINDING_INVALID'))
            || (Array.isArray(story?.validation_failures)
                && story.validation_failures.some(
                    (failure) => (failure?.code ?? failure?.rule_name)
                        === 'STORY_ITEM_BINDING_INVALID',
                ))
        )
    ));

    let intentVerb = 'Generate';
    let busyVerb = 'Generating';
    let description = config.description;
    if (isRevision) {
        intentVerb = 'Revise';
        busyVerb = 'Revising';
        description = 'Revise User Story drafts for this requirement based on review feedback.';
    } else if (isCorrection) {
        intentVerb = 'Correct';
        busyVerb = 'Correcting';
        description = isBindingRecovery
            ? 'Replace the binding-invalid accepted Story set from its exact immutable artifact.'
            : 'Generate replacement User Story drafts for this accepted requirement.';
    }

    let requirement = '';
    if (pbiId) {
        const pendingItems = Array.isArray(context?.storyPending?.items)
            ? context.storyPending.items
            : (Array.isArray(lifecycleState?.storyPending?.items)
                ? lifecycleState.storyPending.items
                : []);
        const pending = pendingItems.find((item) => item?.backlog_item_id === pbiId);
        if (pending?.requirement) {
            requirement = pending.requirement;
        }
    }

    const summarySuffix = requirement ? `: ${requirement}` : '';
    const label = pbiId ? `${intentVerb} Stories for ${pbiId}${summarySuffix}` : `${intentVerb} Stories`;
    const busyLabel = pbiId ? `${busyVerb} Stories for ${pbiId}...` : `${busyVerb} Stories...`;

    return {
        label,
        busyLabel,
        description,
        icon: config.icon,
        pbiId,
        requirement,
        intent: isRevision ? 'revision' : isCorrection ? 'correction' : 'generation',
        intentVerb,
        intentLabel: isRevision ? 'revision' : isCorrection ? 'correction' : 'generation',
        correctionBinding,
        isBindingRecovery,
    };
}

function deliveryGenerationActionMarkup(action, position = {}, reviews = {}, index = 0, context = {}) {
    const details = deliveryGenerationActionDetails(action, position, reviews, context);
    if (!details) return '';
    const bindingAttributes = deliveryActionBindingAttributes(action);
    const content = `<p class="mb-3 text-sm leading-6 text-slate-600">${escapeWorkflowText(details.description)}</p>`;
    if (action.request_kind === 'record_sprint_plan') {
        if (!canGenerateSprintPlan(context)) {
            return `<section role="alert" data-sprint-candidate-projection-error="true" class="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">${content}<p><strong>Sprint candidate projection unavailable.</strong> Reload after current selected-scope dependency confirmation; Sprint generation remains blocked.</p></section>`;
        }
        const owner = sprintOwnerProjection(context);
        if (!owner) {
            return `<section role="alert" data-sprint-owner-projection-error="true" class="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">${content}<p><strong>Sprint owner projection unavailable.</strong> Reload before generating a Sprint plan.</p></section>`;
        }
        const capacity = sprintCapacityProjection(context);
        if (!capacity || capacity.status === 'unavailable') {
            return `<section role="alert" data-sprint-capacity-projection-error="true" class="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">${content}<p><strong>Sprint capacity recommendation unavailable.</strong> Reload before generating a Sprint plan.</p></section>`;
        }
        const recommendedValue = capacity.points === null ? '' : ` value="${capacity.points}"`;
        const disabledAttr = capacity.points === null ? ' disabled' : '';
        const capacityGuidance = capacity.status === 'recommended'
            ? `<p class="mt-1 text-xs leading-5 text-slate-500">Recommendation from completed Sprint metrics: ${escapeWorkflowText(capacity.rationale)}</p>`
            : `<p class="mt-1 text-xs leading-5 text-slate-500">${escapeWorkflowText(capacity.rationale)}</p>`;
        return `<form data-delivery-generation-action="${escapeWorkflowText(action.request_kind)}"
            data-delivery-generation-form="${escapeWorkflowText(action.request_kind)}" ${bindingAttributes}
            class="space-y-4 rounded-lg border border-slate-200 p-4">
            ${content}
            <div class="max-w-xl">
                <p class="text-sm font-semibold">Sprint owner</p>
                <p class="mt-1 break-anywhere text-sm leading-6 text-slate-700" data-sprint-owner-kind="${escapeWorkflowText(owner.kind)}">${escapeWorkflowText(owner.display_label)}</p>
                <label for="delivery-team-name-${index}" class="mt-4 block text-sm font-semibold">Named team override</label>
                <input id="delivery-team-name-${index}" name="team_name" type="text" autocomplete="organization"
                    class="mt-1.5 w-full rounded-lg border-slate-300 text-sm focus:border-accent focus:ring-accent" />
                <p class="mt-1 text-xs leading-5 text-slate-500">Optional. Leave blank to use the resolved Sprint owner.</p>
                <label for="delivery-max-story-points-${index}" class="mt-4 block text-sm font-semibold">Maximum story points</label>
                <input id="delivery-max-story-points-${index}" name="max_story_points" type="number" min="1" step="1" inputmode="numeric" required${recommendedValue}
                    class="mt-1.5 w-full rounded-lg border-slate-300 text-sm focus:border-accent focus:ring-accent" />
                ${capacityGuidance}
            </div>
            <button type="submit"${disabledAttr} class="${BUTTON_PRIMARY}">
                <span class="material-symbols-outlined" aria-hidden="true">${details.icon}</span>
                <span data-delivery-action-label="true">${escapeWorkflowText(details.label)}</span>
            </button>
            <p data-delivery-action-status="true" hidden role="status" aria-live="polite" aria-atomic="true"
                class="text-sm leading-6 text-slate-700"></p>
        </form>`;
    }
    if (details.intent === 'correction' && action.availability === 'locked') {
        return `<section role="alert" data-story-correction-input-unavailable="true" ${bindingAttributes}
            class="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">
            <p class="mb-2 font-semibold">${escapeWorkflowText(details.label)}</p>
            ${content}
            <p><strong>Correction unavailable.</strong> The accepted Story artifact cannot be safely reconstructed as provider input. This action is locked.</p>
        </section>`;
    }
    const isMissingRequirement = action.request_kind === 'record_story_draft' && !details.requirement;
    const disabledAttr = isMissingRequirement ? ' disabled title="Requirement summary unavailable"' : '';
    const correctionPriority = details.intent === 'correction'
        ? ` data-story-correction-priority="${details.isBindingRecovery ? 'recovery' : 'secondary'}"`
        : '';
    const buttonClass = details.intent === 'correction' && !details.isBindingRecovery
        ? BUTTON_SECONDARY
        : BUTTON_PRIMARY;
    return `<div data-delivery-generation-action="${escapeWorkflowText(action.request_kind)}" ${bindingAttributes}${correctionPriority} class="mt-4">
        ${content}
        <button type="button" data-direct-action="${escapeWorkflowText(action.request_kind)}"${disabledAttr} class="${buttonClass}">
            <span class="material-symbols-outlined" aria-hidden="true">${details.icon}</span>
            <span data-delivery-action-label="true">${escapeWorkflowText(details.label)}</span>
        </button>
        <p data-delivery-action-status="true" hidden role="status" aria-live="polite" aria-atomic="true"
            class="mt-3 text-sm leading-6 text-slate-700"></p>
    </div>`;
}

function isSha256Fingerprint(value) {
    return typeof value === 'string' && /^sha256:[0-9a-f]{64}$/.test(value);
}

function isStructuralDiagnostic(value) {
    return Boolean(value && typeof value === 'object'
        && typeof (value.code ?? value.rule_name) === 'string'
        && (value.code ?? value.rule_name).trim()
        && typeof value.message === 'string'
        && value.message.trim());
}

const STRUCTURAL_EVIDENCE_PROVES = [
    'exact Story identity',
    'immutable accepted Story artifact/item binding',
    'accepted Backlog and Specification lineage',
    'parent-bounded Specification references',
    'required Story shape',
    'non-empty acceptance criteria',
    'current evidence and input fingerprints',
];
const STRUCTURAL_EVIDENCE_DOES_NOT_PROVE = [
    'semantic/model quality',
    'product value',
    'human Sprint selection',
    'dependency safety',
    'Sprint candidacy',
    'Sprint-generation readiness',
];

function parseStructuralEvidenceScope(value) {
    if (!value || typeof value !== 'object'
        || !Array.isArray(value.proves)
        || !Array.isArray(value.does_not_prove)) return null;
    const exact = (received, expected) => received.length === expected.length
        && received.every((item, index) => item === expected[index]);
    if (!exact(value.proves, STRUCTURAL_EVIDENCE_PROVES)
        || !exact(value.does_not_prove, STRUCTURAL_EVIDENCE_DOES_NOT_PROVE)) {
        return null;
    }
    return value;
}

function structuralEvidenceScopeMarkup(scope) {
    if (!scope) {
        return `<p role="alert" class="text-xs leading-5 text-red-700" data-story-evidence-scope-unavailable="true">Structural evidence scope is unavailable. Story controls are locked until the current exact proof boundary is loaded.</p>`;
    }
    const list = (items) => `<ul role="list" class="list-disc pl-5 space-y-1">${items.map((item) => `<li>${escapeWorkflowText(item)}</li>`).join('')}</ul>`;
    return `<section class="rounded border border-slate-200 bg-slate-50 p-3 text-xs leading-5 text-slate-700" data-story-evidence-scope="true"><p><strong>Provider-free structural evidence proves:</strong></p>${list(scope.proves)}<p class="mt-2"><strong>It does not prove:</strong></p>${list(scope.does_not_prove)}</section>`;
}

function storyMutationPhase() {
    return activeStoryMutation?.phase ?? null;
}

function storyMutationLocked() {
    return ['submitting', 'awaiting_authority'].includes(storyMutationPhase());
}

function storyMutationStatusMarkup() {
    const phase = storyMutationPhase();
    if (phase === 'submitting') {
        return `<p role="status" aria-live="polite" class="text-xs text-slate-700">Story update is being submitted; controls remain locked.</p>`;
    }
    if (phase === 'awaiting_authority') {
        return `<p role="status" aria-live="polite" class="text-xs text-slate-700">Story update was accepted. Current project projection is reloading; controls remain locked.</p>`;
    }
    return '';
}

function parseStoryReadinessProjection(story) {
    if (!story || typeof story !== 'object' || !Number.isInteger(story.story_id) || story.story_id <= 0) return null;
    const eligibilityStatus = story.structural_eligibility_status;
    const selectionState = story.sprint_selection_state;
    const validationStatus = story.validation_status;
    const failures = story.validation_failures;
    if (
        typeof story.is_superseded !== 'boolean'
        || typeof story.structurally_eligible !== 'boolean'
        || !['eligible', 'ineligible', 'stale'].includes(eligibilityStatus)
        || !['unselected', 'selected', 'deferred'].includes(selectionState)
        || !isSha256Fingerprint(story.sprint_selection_state_fingerprint)
        || !(story.selected_scope_fingerprint === null || isSha256Fingerprint(story.selected_scope_fingerprint))
        || typeof story.dependency_safe !== 'boolean'
        || typeof story.sprint_candidate !== 'boolean'
        || !['validated', 'failed', 'unvalidated'].includes(validationStatus)
        || !Array.isArray(failures)
    ) return null;
    const isEligible = eligibilityStatus === 'eligible';
    const isCurrentFailure = eligibilityStatus === 'ineligible';
    const isMissingEvidence = eligibilityStatus === 'stale' && validationStatus === 'unvalidated';
    const isStaleEvidence = eligibilityStatus === 'stale' && validationStatus === 'validated';
    if (isEligible && (!story.structurally_eligible || validationStatus !== 'validated' || failures.length !== 0)) return null;
    if (isCurrentFailure && (story.structurally_eligible || validationStatus !== 'failed' || failures.length === 0 || !failures.every(isStructuralDiagnostic))) return null;
    if ((isMissingEvidence || isStaleEvidence) && (story.structurally_eligible || failures.length !== 0)) return null;
    if (!isEligible && !isCurrentFailure && !isMissingEvidence && !isStaleEvidence) return null;
    if (selectionState === 'selected' && isEligible && !isSha256Fingerprint(story.selected_scope_fingerprint)) return null;
    if (story.dependency_safe && (selectionState !== 'selected' || !isEligible || !isSha256Fingerprint(story.selected_scope_fingerprint))) return null;
    const expectedCandidate = !story.is_superseded
        && selectionState === 'selected'
        && isEligible
        && story.dependency_safe
        && story.selected_scope_fingerprint !== null;
    if (story.sprint_candidate !== expectedCandidate) return null;
    return {
        story,
        eligibilityStatus,
        selectionState,
        failures,
        isMissingEvidence,
        isStaleEvidence,
    };
}

function storySelectionButtons(projection, controlsLocked = false) {
    const { story, selectionState } = projection;
    const binding = `data-story-selection-id="${story.story_id}" data-story-selection-fingerprint="${story.sprint_selection_state_fingerprint}"`;
    const button = (intent, label, disabled = false) => {
        const locked = disabled || controlsLocked;
        return `<button type="button" ${binding} data-story-selection-intent="${intent}"${locked ? ' disabled aria-disabled="true"' : ''}${controlsLocked ? ' aria-busy="true"' : ''} aria-label="${label} for ${escapeWorkflowText(story.source_story_item_id || `Story #${story.story_id}`)}" class="${BUTTON_SECONDARY}"><span data-story-selection-label="true">${label}</span></button>`;
    };
    if (selectionState === 'selected') return `${button('remove', 'Remove from Sprint selection')}${button('defer', 'Defer')}`;
    if (selectionState === 'deferred') return `${button('select', 'Select for Sprint', !story.structurally_eligible)}${button('remove', 'Remove from Sprint selection')}`;
    return `${button('select', 'Select for Sprint', !story.structurally_eligible)}${button('defer', 'Defer')}`;
}

function activeStoryProjectionRows(stories) {
    if (!Array.isArray(stories)) return [];
    return stories.filter((story) => story?.is_superseded !== true);
}

function storyReadinessMarkup(stories, context = {}) {
    const activeStories = activeStoryProjectionRows(stories);
    if (activeStories.length === 0) return '';
    const pendingItems = Array.isArray(context?.storyPending?.items) ? context.storyPending.items : [];
    const evidenceScope = parseStructuralEvidenceScope(
        context?.storyDependencies?.structural_evidence_scope,
    );
    const controlsLocked = storyMutationLocked() || evidenceScope === null;
    const storyRows = activeStories.map((story) => {
        const projection = parseStoryReadinessProjection(story);
        const pbiId = story?.backlog_item_id || '';
        const pending = pbiId ? pendingItems.find((item) => item?.backlog_item_id === pbiId) : null;
        const requirement = pending?.requirement || '';
        const storyIdText = story?.source_story_item_id || `Story #${story?.story_id ?? '?'}`;
        if (!projection) {
            return `<div class="py-3" data-story-readiness-row="${escapeWorkflowText(story?.story_id ?? 'unknown')}"><p role="alert" class="text-sm text-red-700">Story state unavailable. Dependent Sprint-selection controls are locked until a complete current projection is available.</p><button type="button" disabled aria-disabled="true" class="${BUTTON_SECONDARY}">Sprint selection unavailable</button></div>`;
        }
        const { eligibilityStatus, selectionState, failures, isMissingEvidence, isStaleEvidence } = projection;
        const eligibilityLabel = eligibilityStatus === 'eligible' ? 'Structurally eligible' : (isMissingEvidence ? 'Structural evidence missing' : (isStaleEvidence ? 'Structural evidence stale' : 'Structural eligibility failed'));
        const selectionLabel = selectionState === 'selected' ? 'Selected for Sprint' : (selectionState === 'deferred' ? 'Deferred' : 'Unselected');
        const diagnostics = eligibilityStatus === 'ineligible' && failures.length > 0
            ? `<ul role="list" class="mt-2 space-y-1 text-xs text-red-700" data-story-validation-diagnostics="true">${failures.map((failure) => `<li><strong>${escapeWorkflowText(failure?.code || failure?.rule_name || 'Structural failure')}</strong>: ${escapeWorkflowText(failure?.message || 'Structural rule failed.')}</li>`).join('')}</ul>`
            : '';
        const reconcile = (isMissingEvidence || isStaleEvidence)
            ? `<button type="button" data-story-structural-reconcile-id="${story.story_id}"${controlsLocked ? ' disabled aria-disabled="true" aria-busy="true"' : ''} aria-label="Re-run structural checks for ${escapeWorkflowText(storyIdText)}" class="${BUTTON_SECONDARY}"><span data-story-reconcile-label="true">Re-run structural checks</span></button>`
            : '';
        return `<div class="py-3 first:pt-0 last:pb-0 flex flex-col gap-3" data-story-readiness-row="${story.story_id}">
            <div class="min-w-0 space-y-1"><div class="flex items-center gap-2 flex-wrap"><span class="font-semibold text-sm text-slate-900">${escapeWorkflowText(storyIdText)}</span>${pbiId ? `<span class="text-xs text-slate-500 font-mono">(${escapeWorkflowText(pbiId)})</span>` : ''}</div>${requirement ? `<p class="text-xs text-slate-600">${escapeWorkflowText(requirement)}</p>` : ''}<p class="text-xs text-slate-500">Rank: ${escapeWorkflowText(story.rank || '-')} · Points: ${escapeWorkflowText(story.story_points ?? '-')}</p></div>
            <ul role="list" class="flex flex-wrap gap-2 text-xs"><li class="rounded-full border border-slate-300 px-2 py-0.5">${eligibilityLabel}</li><li class="rounded-full border border-slate-300 px-2 py-0.5">${selectionLabel}</li><li class="rounded-full border border-slate-300 px-2 py-0.5">${story.dependency_safe ? 'Dependency confirmed' : 'Dependencies not confirmed'}</li><li class="rounded-full border border-slate-300 px-2 py-0.5">${story.sprint_candidate ? 'Sprint candidate' : 'Not a Sprint candidate'}</li></ul>
            ${diagnostics}
            <div class="flex flex-wrap gap-2">${reconcile}${storySelectionButtons(projection, controlsLocked)}</div>
        </div>`;
    });
    return `<section class="rounded-lg border border-slate-200 bg-white p-4 space-y-3" aria-labelledby="story-readiness-heading" data-story-readiness-section="true"><div class="flex items-center justify-between border-b border-slate-100 pb-2"><h3 id="story-readiness-heading" class="text-sm font-bold text-ink">Story readiness and Sprint selection</h3><span class="text-xs text-slate-500">${activeStories.length} accepted ${activeStories.length === 1 ? 'story' : 'stories'}</span></div>${structuralEvidenceScopeMarkup(evidenceScope)}${storyMutationStatusMarkup()}<div class="divide-y divide-slate-100">${storyRows.join('')}</div></section>`;
}

function validateCandidateProjection(candidates) {
    if (!Array.isArray(candidates) || candidates.length === 0) {
        return {
            isValid: false,
            candidateStories: [],
            candidateIds: [],
            scopeFingerprint: null,
        };
    }
    const seenIds = new Set();
    let scopeFingerprint = null;
    const validStories = [];
    const validIds = [];

    for (const s of candidates) {
        if (!s || typeof s !== 'object') {
            return {
                isValid: false,
                candidateStories: [],
                candidateIds: [],
                scopeFingerprint: null,
            };
        }
        const readiness = parseStoryReadinessProjection(s);
        if (!readiness || !s.sprint_candidate || !s.dependency_safe || readiness.selectionState !== 'selected' || !s.structurally_eligible || !isSha256Fingerprint(s.selected_scope_fingerprint)) {
            return {
                isValid: false,
                candidateStories: [],
                candidateIds: [],
                scopeFingerprint: null,
            };
        }
        if (scopeFingerprint === null) scopeFingerprint = s.selected_scope_fingerprint;
        else if (scopeFingerprint !== s.selected_scope_fingerprint) {
            return { isValid: false, candidateStories: [], candidateIds: [], scopeFingerprint: null };
        }
        if (seenIds.has(s.story_id)) {
            return {
                isValid: false,
                candidateStories: [],
                candidateIds: [],
                scopeFingerprint: null,
            };
        }
        seenIds.add(s.story_id);
        validStories.push(s);
        validIds.push(s.story_id);
    }

    return {
        isValid: true,
        candidateStories: validStories,
        candidateIds: validIds,
        scopeFingerprint,
    };
}

function sprintGenerationCandidateIds(context = {}) {
    const candidates = validateCandidateProjection(context?.sprintCandidates?.items);
    const dependencies = selectedScopeDependencies(
        context?.storyDependencies?.stories,
        context?.storyDependencies,
    );
    const candidateIds = [...candidates.candidateIds].sort((left, right) => left - right);
    const dependencyCandidateIds = dependencies.scopeStories
        .filter((story) => story.sprint_candidate)
        .map((story) => story.story_id)
        .sort((left, right) => left - right);
    const candidateVectorMatches = candidateIds.length === dependencyCandidateIds.length
        && candidateIds.every((storyId, index) => storyId === dependencyCandidateIds[index]);
    const isCurrent = candidates.isValid
        && dependencies.isWellFormed
        && candidates.scopeFingerprint === dependencies.scopeFingerprint
        && candidateVectorMatches;
    return isCurrent ? candidateIds : null;
}

function canGenerateSprintPlan(context = {}) {
    return sprintGenerationCandidateIds(context) !== null;
}

function selectedScopeDependencies(stories, dependencies) {
    if (!Array.isArray(stories) || !dependencies || typeof dependencies !== 'object'
        || !Array.isArray(dependencies.stories)
        || !Array.isArray(dependencies.edges)
        || !Array.isArray(dependencies.selected_story_ids)
        || !isSha256Fingerprint(dependencies.selected_scope_fingerprint)) {
        return { scopeStories: [], scopeIds: [], scopeEdges: [], isWellFormed: false };
    }
    const dependencyStories = activeStoryProjectionRows(dependencies.stories);
    const dependencyProjections = dependencyStories.map(parseStoryReadinessProjection);
    if (dependencyProjections.some((projection) => projection === null)) {
        return { scopeStories: [], scopeIds: [], scopeEdges: [], isWellFormed: false };
    }
    const dependencyById = new Map();
    for (const projection of dependencyProjections) {
        if (dependencyById.has(projection.story.story_id)) {
            return { scopeStories: [], scopeIds: [], scopeEdges: [], isWellFormed: false };
        }
        dependencyById.set(projection.story.story_id, projection);
    }
    const scopeIds = dependencies.selected_story_ids;
    if (scopeIds.length === 0
        || scopeIds.some((storyId) => !Number.isInteger(storyId) || storyId <= 0)
        || new Set(scopeIds).size !== scopeIds.length) {
        return { scopeStories: [], scopeIds: [], scopeEdges: [], isWellFormed: false };
    }
    const scopeProjections = scopeIds.map((storyId) => dependencyById.get(storyId));
    if (scopeProjections.some((projection) => !projection)
        || scopeProjections.some((projection) => !projection.story.structurally_eligible
            || projection.selectionState !== 'selected'
            || projection.story.selected_scope_fingerprint !== dependencies.selected_scope_fingerprint)) {
        return { scopeStories: [], scopeIds: [], scopeEdges: [], isWellFormed: false };
    }
    const scopeStories = scopeProjections.map((projection) => projection.story);
    const scopeIdSet = new Set(scopeIds);
    const dependencyStoryIds = new Set(dependencyById.keys());
    const rawEdges = dependencies.edges;
    const scopeEdges = [];
    const seenEdges = new Set();
    for (const edge of rawEdges) {
        if (!edge || typeof edge !== 'object'
            || !Number.isInteger(edge.dependent_story_id)
            || !Number.isInteger(edge.prerequisite_story_id)
            || edge.dependent_story_id <= 0
            || edge.prerequisite_story_id <= 0
            || edge.dependent_story_id === edge.prerequisite_story_id
            || !dependencyStoryIds.has(edge.dependent_story_id)
            || !dependencyStoryIds.has(edge.prerequisite_story_id)
            || !['proposed', 'active', 'rejected'].includes(edge.status)
            || typeof edge.reason !== 'string'
            || !edge.reason.trim()) {
            return { scopeStories: [], scopeIds: [], scopeEdges: [], isWellFormed: false };
        }
        const edgeKey = `${edge.dependent_story_id}:${edge.prerequisite_story_id}`;
        if (seenEdges.has(edgeKey)) return { scopeStories: [], scopeIds: [], scopeEdges: [], isWellFormed: false };
        seenEdges.add(edgeKey);
        if (scopeIdSet.has(edge.dependent_story_id) && ['proposed', 'active'].includes(edge.status)) {
            scopeEdges.push({
                dependent_story_id: edge.dependent_story_id,
                prerequisite_story_id: edge.prerequisite_story_id,
                reason: edge.reason,
                isExternal: !scopeIdSet.has(edge.prerequisite_story_id),
            });
        }
    }
    return {
        scopeStories,
        scopeIds,
        scopeEdges,
        scopeFingerprint: dependencies.selected_scope_fingerprint,
        isWellFormed: true,
    };
}

function storyDisplayLabel(story) {
    if (!story) return '';
    const id = story.source_story_item_id || (story.story_id != null ? `Story #${story.story_id}` : '');
    if (!id) return '';
    return story.backlog_item_id ? `${id} (${story.backlog_item_id})` : id;
}

function buildStoryLookupMap(candidateStories, dependencies) {
    const map = new Map();
    if (Array.isArray(dependencies?.stories)) {
        for (const s of activeStoryProjectionRows(dependencies.stories)) {
            if (s && typeof s === 'object' && s.story_id != null) {
                map.set(s.story_id, s);
            }
        }
    }
    if (Array.isArray(candidateStories)) {
        for (const s of candidateStories) {
            if (s && typeof s === 'object' && s.story_id != null) {
                map.set(s.story_id, s);
            }
        }
    }
    return map;
}

function storyDependencyReviewMarkup(action, stories, dependencies) {
    if (!action || action.request_kind !== 'apply_story_dependencies') return '';
    const { scopeStories, scopeEdges, isWellFormed } = selectedScopeDependencies(
        stories,
        dependencies,
    );
    const storyMap = buildStoryLookupMap(scopeStories, dependencies);
    const storySummary = isWellFormed
        ? (scopeStories.map((s) => storyDisplayLabel(s)).join(', ') || 'None')
        : 'Unavailable (current selected scope missing or malformed)';
    const edgeSummary = isWellFormed
        ? (scopeEdges.length > 0
            ? scopeEdges.map((e) => {
                const dep = storyMap.get(e.dependent_story_id);
                const prereq = storyMap.get(e.prerequisite_story_id);
                const depLabel = storyDisplayLabel(dep) || `Story #${e.dependent_story_id}`;
                const prereqLabel = storyDisplayLabel(prereq) || `Story #${e.prerequisite_story_id}`;
                const reason = e.reason ? ` - ${e.reason}` : '';
                const scopeLabel = e.isExternal ? ' (External/excluded prerequisite)' : '';
                return `${depLabel} -> ${prereqLabel}${scopeLabel}${reason}`;
            }).join('; ')
            : 'None (independent stories)')
        : 'Unavailable (current selected scope missing or malformed)';
    const bindingAttributes = deliveryActionBindingAttributes(action);
    const mutationPhase = activeDependencyMutation?.phase ?? null;
    const mutationSubmitting = mutationPhase === 'submitting';
    const mutationAwaitingProjection = mutationPhase === 'awaiting_authority';
    const mutationLocked = mutationSubmitting || mutationAwaitingProjection;
    const disabledAttr = isWellFormed && !mutationLocked ? '' : 'disabled aria-disabled="true"';
    const busyAttr = mutationLocked ? ' aria-busy="true"' : '';
    const buttonLabel = mutationSubmitting
        ? 'Submitting...'
        : (mutationAwaitingProjection ? 'Reloading...' : 'Confirm dependencies');
    const statusHidden = mutationLocked ? '' : 'hidden';
    const statusMessage = mutationSubmitting
        ? 'Dependency review is being submitted; controls remain locked.'
        : (mutationAwaitingProjection
            ? 'Dependency review was accepted. Current project projection is reloading; controls remain locked.'
            : '');

    return `<div class="rounded-lg border border-amber-200 bg-amber-50/50 p-4 space-y-3" data-dependency-review-section="true" ${bindingAttributes}>
        <div class="flex items-center gap-2">
            <span class="material-symbols-outlined text-amber-700" aria-hidden="true">account_tree</span>
            <h3 class="text-sm font-bold text-amber-900">Dependency review required</h3>
        </div>
        <p class="text-xs leading-5 text-amber-800">Review and confirm dependencies for the selected structurally eligible scope. This does not generate a Sprint.</p>
        <div class="text-xs text-slate-700 bg-white rounded border border-slate-200 p-2.5 space-y-1">
            <p><strong>Selected Stories:</strong> ${escapeWorkflowText(storySummary)}</p>
            <p><strong>Dependency edges:</strong> ${escapeWorkflowText(edgeSummary)}</p>
        </div>
        <button type="button" data-apply-dependencies="true" ${disabledAttr}${busyAttr} class="${BUTTON_PRIMARY}">
            <span class="material-symbols-outlined" aria-hidden="true">verified</span>
            <span data-delivery-action-label="true">${buttonLabel}</span>
        </button>
        <p data-delivery-action-status="true" ${statusHidden} role="status" aria-live="polite" aria-atomic="true"
            class="text-sm leading-6 text-slate-700">${statusMessage}</p>
    </div>`;
}

function sprintCandidatePoolMarkup(candidates) {
    const projection = validateCandidateProjection(candidates);
    if (!projection.isValid) return '';
    const { candidateStories } = projection;

    return `<div class="rounded-lg border border-emerald-200 bg-emerald-50/40 p-4 space-y-2" data-candidate-pool-section="true">
        <div class="flex items-center justify-between border-b border-emerald-100 pb-2">
            <div class="flex items-center gap-2">
                <span class="material-symbols-outlined text-accent" aria-hidden="true">view_list</span>
                <h3 class="text-sm font-bold text-emerald-950">Sprint candidate pool</h3>
            </div>
            <span class="text-xs font-semibold text-accent">${candidateStories.length} ${candidateStories.length === 1 ? 'candidate ready' : 'candidates ready'}</span>
        </div>
        <div class="grid gap-2">
            ${candidateStories.map((candidate) => `
                <div class="flex items-center justify-between rounded bg-white px-3 py-2 border border-emerald-100 text-xs">
                    <div class="flex items-center gap-2">
                        <span class="font-semibold text-slate-900">${escapeWorkflowText(candidate.source_story_item_id || (`Story #${candidate.story_id}`))}${candidate.backlog_item_id ? ` (${escapeWorkflowText(candidate.backlog_item_id)})` : ''}</span>
                    </div>
                    <div class="flex items-center gap-3 text-slate-600">
                        <span>Rank: ${escapeWorkflowText(candidate.rank || '-')}</span>
                        <span>Points: ${escapeWorkflowText(candidate.story_points ?? '-')}</span>
                        <span class="text-emerald-700 font-medium font-mono text-[11px] bg-emerald-50 px-1.5 py-0.5 rounded">Ready</span>
                    </div>
                </div>
            `).join('')}
        </div>
    </div>`;
}

function sprintAcceptedPlanEvidenceMarkup(plan) {
    const stories = plan.selected_stories.map((story) => `
        <li class="rounded-lg border border-slate-200 bg-white px-3 py-2">
            <p class="font-semibold text-slate-900">${escapeWorkflowText(story.story_item_id)} · ${escapeWorkflowText(story.title)}</p>
            <p class="mt-1 text-xs text-slate-600">${story.story_points} points · ${story.task_count} ${story.task_count === 1 ? 'task' : 'tasks'}</p>
        </li>
    `).join('');
    return `<details class="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-3" data-sprint-plan-evidence="true">
        <summary class="cursor-pointer text-sm font-semibold text-slate-800">Accepted planning evidence</summary>
        <div class="mt-3 space-y-3 text-sm text-slate-700">
            <p><strong>Accepted by:</strong> ${escapeWorkflowText(plan.acceptance.reviewer)} · ${escapeWorkflowText(plan.acceptance.decided_at)}</p>
            <p><strong>Rationale:</strong> ${escapeWorkflowText(plan.acceptance.rationale)}</p>
            <ul class="space-y-2">${stories}</ul>
        </div>
    </details>`;
}

function sprintStatusMarkup(sprintState, position = {}, actions = [], context = {}) {
    if (sprintState?.kind === 'absent') return '';
    if (sprintState?.kind !== 'ready') {
        return `<section role="alert" data-sprint-status-error="true" class="rounded-lg border border-red-300 bg-red-50 p-4 text-sm text-red-800"><strong>Sprint status unavailable.</strong> Reload before starting or continuing Sprint work. Controls remain locked.</section>`;
    }
    const status = sprintState.data;
    const sprint = status.sprint;
    const plan = status.accepted_plan;
    const heading = sprint.status === 'planned'
        ? `Sprint #${sprint.sprint_id} is planned`
        : (sprint.status === 'active'
            ? `Sprint #${sprint.sprint_id} is active`
            : `Sprint #${sprint.sprint_id} is complete`);
    const start = sprintStartBinding(status, position, actions);
    const correction = sprintCorrectionBinding(status, position, actions);
    const startBusy = activeSprintMutation !== null;
    const startMarkup = start ? `<button type="button" data-direct-action="start_sprint" ${deliveryActionBindingAttributes(start.action)}${startBusy ? ' disabled aria-disabled="true" aria-busy="true"' : ''} class="${BUTTON_PRIMARY}">
        <span class="material-symbols-outlined" aria-hidden="true">play_arrow</span>
        <span data-sprint-start-label="true">${startBusy ? 'Starting Sprint...' : 'Start Sprint'}</span>
    </button>` : (sprint.status === 'planned'
        ? '<p class="text-sm text-slate-600">Start is locked until the current graph and accepted-plan evidence agree.</p>'
        : '');
    const correctionMarkup = correction ? `<details class="rounded-lg border border-slate-200 bg-white p-3" data-sprint-correction="true">
        <summary class="cursor-pointer text-sm font-semibold text-slate-700">Correct accepted plan</summary>
        <div class="mt-3">${deliveryGenerationActionMarkup(
            correction,
            position,
            context.planningReviews ?? {},
            0,
            context,
        )}</div>
    </details>` : '';

    let executionMarkup = '';
    if (sprint.status === 'active') {
        const execution = sprintExecutionProjection(status, position, actions);
        if (execution.kind === 'error') {
            executionMarkup = '<p role="alert" class="mt-4 text-sm text-red-800">Current execution action projection is inconsistent. Task controls remain locked.</p>';
        } else if (execution.kind === 'absent') {
            executionMarkup = '<p class="mt-4 text-sm text-slate-600">No execution action is currently available.</p>';
        } else {
            executionMarkup = `<div class="mt-4 space-y-2" data-sprint-execution-actions="true">
                <p class="text-sm font-semibold text-slate-800">${execution.items.length} current execution ${execution.items.length === 1 ? 'action' : 'actions'}</p>
                <ul class="space-y-2">${execution.items.map(({ task }) => `<li class="rounded-lg border border-sky-200 bg-sky-50 p-3"><p class="text-sm font-semibold text-sky-950">Task #${task.task_id}</p><p class="mt-1 text-sm text-slate-700">${escapeWorkflowText(task.description)}</p></li>`).join('')}</ul>
                <p class="text-xs text-slate-500">Task completion evidence is recorded through the existing Task workflow.</p>
            </div>`;
        }
    }
    return `<section class="rounded-lg border border-emerald-300 bg-emerald-50 p-5" data-sprint-status="${escapeWorkflowText(sprint.status)}">
        <p class="text-xs font-semibold uppercase tracking-wide text-emerald-800">Current Sprint</p>
        <h3 class="mt-1 text-lg font-bold text-emerald-950">${escapeWorkflowText(heading)}</h3>
        <p class="mt-3 text-sm leading-6 text-slate-800"><strong>Goal:</strong> ${escapeWorkflowText(plan.goal)}</p>
        <dl class="mt-3 grid gap-2 text-sm sm:grid-cols-3">
            <div><dt class="font-semibold text-slate-600">Owner</dt><dd>${escapeWorkflowText(plan.owner.display_label)}</dd></div>
            <div><dt class="font-semibold text-slate-600">Scope</dt><dd>${plan.selected_stories.length} ${plan.selected_stories.length === 1 ? 'Story' : 'Stories'} · ${plan.total_points} points</dd></div>
            <div><dt class="font-semibold text-slate-600">Tasks</dt><dd>${plan.task_count}</dd></div>
        </dl>
        <div class="mt-4 flex flex-wrap items-start gap-3">${startMarkup}</div>
        ${executionMarkup}
        ${sprintAcceptedPlanEvidenceMarkup(plan)}
        ${correctionMarkup}
    </section>`;
}

function deliveryPanelMarkup(position, reviews = {}, actions = [], context = {}) {
    const storyItems = Array.isArray(reviews.stories?.items) ? reviews.stories.items : [];
    const backlogState = { position, planningReviews: reviews, actions };
    const backlogContinuation = backlogFeedbackContinuationProjection(backlogState);
    const backlogCorrection = backlogCorrectionActionBinding(backlogState, backlogContinuation);
    const backlog = reviewObject(reviews?.backlog);
    const hasContinuation = backlog !== null
        && Object.prototype.hasOwnProperty.call(backlog, 'continuation');
    const hasTopLevelReview = Boolean(backlog)
        && ('binding' in backlog || 'review' in backlog);
    const exactBacklogAbsence = backlog !== null && Object.keys(backlog).length === 0;
    const validPendingBacklogReview = !hasContinuation && backlogPendingReviewIsValid(backlog);
    const validContinuation = hasContinuation
        && !hasTopLevelReview
        && backlogContinuation.kind === 'display';
    const invalidBacklogReview = !exactBacklogAbsence
        && !validPendingBacklogReview
        && !validContinuation;
    const backlogCard = invalidBacklogReview
        ? backlogProjectionErrorMarkup()
        : (hasContinuation
            ? backlogFeedbackContinuationMarkup(backlogContinuation, backlogCorrection)
            : planningReviewCardMarkup('Backlog review', reviews.backlog, 'backlog'));
    const cards = [
        backlogCard,
        planningReviewCardMarkup('Roadmap review', reviews.roadmap, 'roadmap'),
        ...storyItems.map((item, index) => {
            const pbiId = item?.binding?.instance_key?.startsWith('backlog_item:')
                ? item.binding.instance_key.slice('backlog_item:'.length)
                : (item?.binding?.instance_key || null);
            const cardTitle = pbiId ? `Story review for ${pbiId}` : `Story review ${index + 1}`;
            return planningReviewCardMarkup(cardTitle, item, 'story', index);
        }),
        context?.sprintStatus?.kind === 'ready'
            ? ''
            : planningReviewCardMarkup('Sprint plan review', reviews.sprintPlan, 'sprint', 0),
    ].filter(Boolean);

    const stories = Array.isArray(context?.storyDependencies?.stories)
        ? context.storyDependencies.stories
        : (Array.isArray(lifecycleState?.storyDependencies?.stories)
            ? lifecycleState.storyDependencies.stories
            : []);
    const dependencies = context?.storyDependencies ?? lifecycleState?.storyDependencies ?? {};
    const candidates = Array.isArray(context?.sprintCandidates?.items)
        ? context.sprintCandidates.items
        : (Array.isArray(lifecycleState?.sprintCandidates?.items)
            ? lifecycleState.sprintCandidates.items
            : null);

    const dependencyAction = (Array.isArray(actions) ? actions : []).find(
        (action) => action?.request_kind === 'apply_story_dependencies',
    );

    const readinessSection = storyReadinessMarkup(stories, context);
    const dependencySection = storyDependencyReviewMarkup(dependencyAction, stories, dependencies);
    const candidateSection = sprintCandidatePoolMarkup(candidates);
    const sprintSection = sprintStatusMarkup(
        context?.sprintStatus,
        position,
        actions,
        context,
    );

    const availableDeliveryActions = (Array.isArray(actions) ? actions : []).filter((action) => (
        Boolean(DELIVERY_ACTION_CONFIG[action?.request_kind])
        && !((hasContinuation || invalidBacklogReview) && action?.request_kind === 'record_backlog_draft')
        && !(
            typeof context?.sprintStatus?.kind === 'string'
            && context.sprintStatus.kind !== 'absent'
            && action?.request_kind === 'record_sprint_plan'
        )
    ));
    const actionMarkup = availableDeliveryActions.map((action, index) =>
        deliveryGenerationActionMarkup(action, position, reviews, index, context),
    );

    const sections = [
        sprintSection,
        cards.length ? `<div class="grid gap-4">${cards.join('')}</div>` : '',
        readinessSection,
        dependencySection,
        candidateSection,
        actionMarkup.length ? actionMarkup.join('') : '',
    ].filter(Boolean);

    if (sections.length) {
        return `<div class="space-y-4">
            ${sections.join('')}
        </div>`;
    }

    const decisions = Array.isArray(position?.decisions) ? position.decisions : [];
    const deliveryDecision = decisions.find((decision) => {
        const stage = decisionStage(decision);
        return ['Backlog', 'Roadmap', 'Stories', 'Sprint', 'Execution', 'Review'].includes(stage);
    });
    if (!deliveryDecision) {
        return '<p class="text-sm text-slate-600">Delivery begins after the Specification is accepted.</p>';
    }
    return `<p class="text-sm leading-6 text-slate-700"><strong>${escapeWorkflowText(decisionStage(deliveryDecision))}:</strong> ${escapeWorkflowText(stageReason(deliveryDecision))}</p>`;
}

function setText(id, value) {
    const element = document.getElementById(id);
    if (element) element.textContent = value;
}

function setMarkup(id, markup) {
    const element = document.getElementById(id);
    if (element) element.innerHTML = markup;
}

function setProjectError(message) {
    const error = document.getElementById('project-error');
    if (!error) return;
    error.textContent = message;
    error.classList.toggle('hidden', !message);
}

function setInterviewStatus(scope, message) {
    const status = document.getElementById(`${scope}-response-status`);
    if (!status) return;
    status.textContent = message;
    status.hidden = !message;
}

function renderDashboard() {
    const project = lifecycleState.project ?? {};
    setText('project-page-title', project.name || `Project ${selectedProjectId}`);
    document.title = `${project.name || 'Project'} | AgileForge`;
    setText('project-description', project.description || 'No description');
    setText(
        'project-goal-summary',
        lifecycleState.goal?.active?.statement
            ?? lifecycleState.goal?.candidate?.statement
            ?? lifecycleState.goal?.outcome?.statement
            ?? 'Not set',
    );
    setText(
        'project-repository-summary',
        lifecycleState.repository?.repository?.worktree_path ?? 'Not attached',
    );
    setMarkup(
        'lifecycle-stage-strip',
        workflowPositionMarkup(
            lifecycleState.position,
            lifecycleState.actions,
            lifecycleState,
        ),
    );
    setMarkup(
        'vision-panel',
        visionPanelMarkup(lifecycleState.vision, lifecycleState.actions, {
            project: lifecycleState.project,
            repository: lifecycleState.repository?.repository,
        }),
    );
    setMarkup('goal-panel', productGoalPanelMarkup(lifecycleState.goal, lifecycleState.actions));
    setMarkup(
        'specification-panel',
        specificationPanelMarkup(
            lifecycleState.specification,
            lifecycleState.actions,
            lifecycleState.position,
        ),
    );
    setMarkup(
        'repository-panel',
        repositoryPanelMarkup(lifecycleState.repository, lifecycleState.actions),
    );
    setMarkup(
        'delivery-panel',
        deliveryPanelMarkup(
            lifecycleState.position,
            lifecycleState.planningReviews,
            lifecycleState.actions,
            lifecycleState,
        ),
    );
    reapplyActiveSpecificationMutation();
    reapplyActiveBacklogCorrectionMutation();
}

function validationIssueMessage(issue) {
    if (!issue || typeof issue.msg !== 'string') return null;
    const location = (Array.isArray(issue.loc) ? issue.loc : [])
        .filter((part) => part !== 'body')
        .map((part) => humanizeKey(part))
        .join(' > ');
    const message = issue.msg.trim().replace(/[.]+$/, '');
    return `${location ? `${location}: ` : ''}${message}.`;
}

function responseErrorMessage(payload, fallback) {
    const detail = payload?.detail;
    if (Array.isArray(detail)) {
        const validation = detail.map(validationIssueMessage).filter(Boolean).join(' ');
        if (validation) return validation;
    }
    const nestedErrors = detail?.errors;
    return detail?.error?.message
        ?? detail?.message
        ?? (Array.isArray(nestedErrors) ? nestedErrors[0]?.message : null)
        ?? payload?.message
        ?? fallback;
}

async function requestJson(path, options = {}) {
    const response = await fetch(path, options);
    const text = await response.text();
    let payload = {};
    if (text) {
        try {
            payload = JSON.parse(text);
        } catch (_error) {
            throw new Error('The dashboard received an unreadable response.');
        }
    }
    if (!response.ok) {
        const error = new Error(
            responseErrorMessage(payload, 'The requested action failed.'),
        );
        error.status = response.status;
        error.code = payload?.detail?.error?.code
            ?? payload?.detail?.errors?.[0]?.code
            ?? payload?.code
            ?? null;
        throw error;
    }
    return payload;
}

async function requestPlanningReview(url, options) {
    try {
        return await requestJson(url, options);
    } catch (error) {
        if (error.status === 409 && error.code === 'PLANNING_REVIEW_NOT_AVAILABLE') {
            return { data: {} };
        }
        throw error;
    }
}

async function requestSprintStatus(url, options) {
    try {
        const response = await requestJson(url, options);
        return { kind: 'candidate', data: response?.data };
    } catch (error) {
        if (error.name === 'AbortError') throw error;
        if (error.status === 404 && error.code === 'SPRINT_NOT_FOUND') {
            return { kind: 'absent' };
        }
        return { kind: 'error', message: error.message };
    }
}

async function loadDashboard() {
    const sequence = ++dashboardLoadSequence;
    const storyMutationAtStart = activeStoryMutation === null
        ? null
        : {
            token: activeStoryMutation.token,
            phase: activeStoryMutation.phase,
        };
    const dependencyMutationAtStart = activeDependencyMutation === null
        ? null
        : {
            token: activeDependencyMutation.token,
            phase: activeDependencyMutation.phase,
        };
    const backlogCorrectionMutationAtStart = activeBacklogCorrectionMutation === null
        ? null
        : {
            token: activeBacklogCorrectionMutation.token,
            phase: activeBacklogCorrectionMutation.phase,
        };
    activeDashboardLoadController?.abort();
    const controller = new AbortController();
    activeDashboardLoadController = controller;
    const base = `/api/projects/${selectedProjectId}`;
    try {
        const options = { signal: controller.signal };
        const [
            project,
            position,
            vision,
            goal,
            specification,
            repository,
            backlogReview,
            roadmapReview,
            storyReviews,
            sprintPlanReview,
            storyPending,
            storyDependencies,
            sprintCandidates,
            sprintStatusResponse,
        ] = await Promise.all([
            requestJson(base, options),
            requestJson(`${base}/position`, options),
            requestJson(`${base}/vision/status`, options),
            requestJson(`${base}/goals/status`, options),
            requestJson(`${base}/specifications/review`, options),
            requestJson(`${base}/repository`, options),
            requestPlanningReview(`${base}/backlog/review`, options),
            requestPlanningReview(`${base}/roadmap/review`, options),
            requestPlanningReview(`${base}/story/reviews`, options),
            requestPlanningReview(`${base}/sprint/plan/review`, options),
            requestJson(`${base}/story/pending`, options),
            requestJson(`${base}/story/dependencies`, options),
            requestJson(`${base}/sprint/candidates`, options),
            requestSprintStatus(`${base}/sprint/status`, options),
        ]);
        if (sequence !== dashboardLoadSequence || controller.signal.aborted) return false;
        const sprintPlanReviewData = sprintPlanReview.data ?? {};
        const sprintCandidatesData = sprintCandidates?.data ?? {};
        const positionData = position.data ?? {};
        const positionActions = position.actions ?? [];
        const validatedSprintStatus = sprintStatusResponse.kind === 'candidate'
            ? await validateSprintStatusProjection(
                sprintStatusResponse.data,
                selectedProjectId,
            )
            : null;
        const sprintAuthorityAdvertised = positionActions.some((action) => (
            ['start_sprint', 'complete_task'].includes(action?.request_kind)
        ));
        const sprintStatusAbsent = sprintStatusResponse.kind === 'absent'
            && !sprintAuthorityAdvertised;
        await Promise.all([
            validateSprintOwnerProjection(
                sprintCandidatesData?.sprint_owner,
                sprintCandidatesData?.project_id === selectedProjectId
                    ? selectedProjectId
                    : null,
            ),
            validateSprintOwnerProjection(
                sprintPlanReviewData?.review?.candidate?.sprint_owner,
                sprintPlanReviewData?.review?.project_id === selectedProjectId
                    ? selectedProjectId
                    : null,
            ),
        ]);
        if (sequence !== dashboardLoadSequence || controller.signal.aborted) return false;
        lifecycleState = {
            project: project.data ?? {},
            position: positionData,
            actions: positionActions,
            vision: vision.data ?? {},
            goal: goal.data ?? {},
            specification: specification.data ?? {},
            repository: repository.data ?? {},
            planningReviews: {
                backlog: backlogReview.data ?? {},
                roadmap: roadmapReview.data ?? {},
                stories: storyReviews.data ?? { items: [] },
                sprintPlan: sprintPlanReviewData,
            },
            storyPending: storyPending.data ?? {},
            storyDependencies: storyDependencies?.data ?? {},
            sprintCandidates: sprintCandidatesData,
            sprintStatus: sprintStatusAbsent
                ? { kind: 'absent' }
                : (validatedSprintStatus
                    ? { kind: 'ready', data: validatedSprintStatus }
                    : {
                        kind: 'error',
                        message: sprintStatusResponse.message
                            ?? 'Sprint status projection is incomplete.',
                    }),
        };
        const backlogFocusMutation = reconcileBacklogCorrectionMutation(
            backlogCorrectionMutationAtStart,
        );
        if (
            storyMutationAtStart?.phase === 'awaiting_authority'
            && activeStoryMutation?.token === storyMutationAtStart.token
            && activeStoryMutation.phase === storyMutationAtStart.phase
        ) {
            activeStoryMutation = null;
        }
        if (
            dependencyMutationAtStart?.phase === 'awaiting_authority'
            && activeDependencyMutation?.token === dependencyMutationAtStart.token
            && activeDependencyMutation.phase === dependencyMutationAtStart.phase
        ) {
            activeDependencyMutation = null;
        }
        setProjectError('');
        renderDashboard();
        consumeBacklogCorrectionFocus(backlogFocusMutation);
        consumeBacklogFeedbackFocus();
        return true;
    } catch (error) {
        if (sequence !== dashboardLoadSequence || controller.signal.aborted) return false;
        if (error.status === 409) {
            lifecycleState = {
                ...lifecycleState,
                planningReviews: {
                    backlog: {},
                    roadmap: {},
                    stories: { items: [] },
                    sprintPlan: {},
                },
            };
            renderDashboard();
            setProjectError(error.message);
        }
        throw error;
    } finally {
        if (activeDashboardLoadController === controller) {
            activeDashboardLoadController = null;
        }
    }
}

async function postAction(action, fields = {}, options = {}) {
    if (!action) throw new Error('This action is not available in the current lifecycle state.');
    const headers = { 'Content-Type': 'application/json' };
    if (options.expectedCandidate) {
        headers['X-AgileForge-Expected-Candidate'] = options.expectedCandidate;
    }
    if (options.expectedDecision) {
        headers['X-AgileForge-Expected-Decision'] = options.expectedDecision;
    }
    if (options.expectedInstance) {
        headers['X-AgileForge-Expected-Instance'] = options.expectedInstance;
    }
    return requestJson(`/api/projects/${selectedProjectId}/${action.endpoint}`, {
        method: 'POST',
        headers,
        body: JSON.stringify(semanticMutationPayload(fields)),
    });
}

function setDialogError(message) {
    const error = document.getElementById('human-action-error');
    if (!error) return;
    error.textContent = message;
    error.classList.toggle('hidden', !message);
}

function closeHumanDialog() {
    document.getElementById('human-action-dialog')?.close();
    pendingHumanAction = null;
    setDialogError('');
}

function captureAction(action) {
    if (!action?.request_kind || !action?.endpoint) return null;
    return { ...action };
}

function deliveryActionContainer(control) {
    return control?.closest?.('[data-delivery-generation-action]') ?? control;
}

function deliveryActionBindingElement(control) {
    const container = deliveryActionContainer(control);
    return container?.dataset?.deliveryActionNode ? container : control;
}

function deliveryActionElementMatches(element, action, requestKind) {
    const dataset = element?.dataset ?? {};
    const renderedRequestKind = dataset.deliveryGenerationAction
        ?? dataset.directAction;
    const hasInstance = dataset.deliveryActionHasInstance === 'true'
        || (
            dataset.deliveryActionHasInstance === undefined
            && typeof dataset.deliveryActionInstance === 'string'
        );
    const renderedInstance = hasInstance ? dataset.deliveryActionInstance : null;
    const renderedTransport = dataset.deliveryActionTransport ?? '';
    return renderedRequestKind === requestKind
        && dataset.deliveryActionNode === action?.node_id
        && renderedInstance === (action?.instance_key ?? null)
        && dataset.deliveryActionEndpoint === action?.endpoint
        && renderedTransport === (action?.transport ?? '');
}

function captureDeliveryActionBinding(state, control, requestKind) {
    const bindingElement = deliveryActionBindingElement(control);
    const matches = (Array.isArray(state?.actions) ? state.actions : []).filter(
        (action) => deliveryActionElementMatches(
            bindingElement,
            action,
            requestKind,
        ),
    );
    return matches.length === 1 ? captureAction(matches[0]) : null;
}

function captureSprintStartControlBinding(state, control) {
    const expected = sprintStartBinding(
        state?.sprintStatus?.data,
        state?.position,
        state?.actions,
    );
    const rendered = captureDeliveryActionBinding(state, control, 'start_sprint');
    return expected && rendered && deliveryActionsMatch(expected.action, rendered)
        ? expected
        : null;
}

function currentDeliveryActionContainers(action, requestKind) {
    const candidates = Array.from(document.querySelectorAll?.(
        `[data-delivery-generation-action="${requestKind}"]`,
    ) ?? []);
    const exact = candidates.find(
        (candidate) => deliveryActionElementMatches(candidate, action, requestKind),
    );
    return exact ? [exact] : [];
}

function deliveryActionsMatch(left, right) {
    return left?.node_id === right?.node_id
        && (left?.instance_key ?? null) === (right?.instance_key ?? null)
        && left?.request_kind === right?.request_kind
        && left?.endpoint === right?.endpoint
        && (left?.transport ?? '') === (right?.transport ?? '');
}

function sprintStartConfirmed(state, binding) {
    const status = state?.sprintStatus;
    const data = status?.kind === 'ready' ? status.data : null;
    const sprint = data?.sprint;
    const plan = data?.accepted_plan;
    const start = data?.start;
    const startStillAdvertised = (Array.isArray(state?.actions) ? state.actions : [])
        .some((action) => action?.request_kind === 'start_sprint');
    const startDecisionStillPresent = (
        Array.isArray(state?.position?.decisions) ? state.position.decisions : []
    ).some((decision) => (
        decision?.request_kind === 'start_sprint'
        && decision.reason_code === 'SPRINT_READY_TO_START'
    ));
    return Boolean(
        sprint?.sprint_id === binding.sprintId
        && sprint.status === 'active'
        && plan?.sprint_id === binding.sprintId
        && plan.status === 'active'
        && plan.sprint_plan_artifact_id === binding.sprintPlanArtifactId
        && plan.sprint_plan_artifact_decision_id
            === binding.sprintPlanArtifactDecisionId
        && plan.plan_fingerprint === binding.planFingerprint
        && plan.candidate_set_fingerprint === binding.candidateSetFingerprint
        && plan.task_content_fingerprint === binding.taskContentFingerprint
        && start?.sprint_id === binding.sprintId
        && start.sprint_plan_artifact_id === binding.sprintPlanArtifactId
        && start.sprint_plan_artifact_decision_id
            === binding.sprintPlanArtifactDecisionId
        && start.plan_fingerprint === binding.planFingerprint
        && start.candidate_set_fingerprint === binding.candidateSetFingerprint
        && start.task_content_fingerprint === binding.taskContentFingerprint
        && !startStillAdvertised
        && !startDecisionStillPresent
    );
}

function captureBacklogCorrectionBinding(state, control) {
    const continuation = backlogFeedbackContinuationProjection(state);
    const correction = backlogCorrectionActionBinding(state, continuation);
    const binding = captureDeliveryActionBinding(state, control, 'record_backlog_draft');
    if (continuation.kind !== 'display' || correction.kind !== 'ready'
        || !binding || !deliveryActionsMatch(binding, correction.action)) {
        return null;
    }
    return {
        action: binding,
        backlogArtifactId: continuation.candidate.backlog_artifact_id,
        decisionFingerprint: continuation.decision.decision_fingerprint,
    };
}

function reapplyActiveBacklogCorrectionMutation() {
    const mutation = activeBacklogCorrectionMutation;
    if (!mutation) return;
    currentDeliveryActionContainers(mutation.action, 'record_backlog_draft').forEach((container) => {
        const control = container.querySelector?.('[data-direct-action="record_backlog_draft"]')
            ?? container;
        setDeliveryActionBusy(control, true, 'record_backlog_draft', true);
    });
}

function backlogPendingReview(state) {
    const backlog = reviewObject(state?.planningReviews?.backlog);
    return backlogPendingReviewIsValid(backlog) ? backlog : null;
}

function advertisedBacklogCorrectionAction(action) {
    return action?.node_id === 'backlog.generate'
        || action?.endpoint === 'backlog/generate'
        || action?.request_kind === 'record_backlog_draft';
}

function currentBacklogDecisions(state) {
    const decisions = state?.position?.decisions;
    return Array.isArray(decisions) ? decisions : null;
}

function hasCurrentFeedbackDecision(state, decisionFingerprint) {
    const decisions = currentBacklogDecisions(state);
    return decisions === null || decisions.some((decision) => (
        decision?.decision_fingerprint === decisionFingerprint
        || isBacklogFeedbackContinuationDecision(decision)
    ));
}

function hasAdvertisedBacklogCorrectionAction(state) {
    return !Array.isArray(state?.actions)
        || state.actions.some(advertisedBacklogCorrectionAction);
}

function validNonFeedbackBacklogState(state, mutation) {
    const backlog = reviewObject(state?.planningReviews?.backlog);
    return backlog !== null
        && Object.keys(backlog).length === 0
        && !hasCurrentFeedbackDecision(state, mutation.decisionFingerprint)
        && !hasAdvertisedBacklogCorrectionAction(state);
}

function exactBacklogPendingReferences(decision, candidate, specification, productGoal) {
    const references = Array.isArray(decision?.fact_references)
        ? decision.fact_references
        : null;
    const expected = {
        backlog: [candidate?.backlog_artifact_id, candidate?.artifact_fingerprint],
        specification: [specification?.spec_version_id, specification?.spec_hash],
        product_goal: [productGoal?.product_goal_artifact_id, productGoal?.product_goal_fingerprint],
    };
    if (!references || references.length !== Object.keys(expected).length
        || Object.values(expected).some(([id, fingerprint]) => !isConcreteReviewIdentity(id, fingerprint))) {
        return false;
    }
    return Object.entries(expected).every(([factType, [factId, fingerprint]]) => {
        const matches = references.filter((reference) => reference?.fact_type === factType);
        return matches.length === 1
            && isCanonicalFactReferenceId(matches[0].fact_id)
            && matches[0].fact_id === String(factId)
            && matches[0].fingerprint === fingerprint;
    });
}

function completeCorrectedBacklogCandidate(candidate, mutation) {
    return candidate !== null
        && Number.isInteger(candidate.backlog_artifact_id)
        && candidate.backlog_artifact_id > 0
        && typeof candidate.artifact_fingerprint === 'string'
        && Boolean(candidate.artifact_fingerprint.trim())
        && Number.isInteger(candidate.version_number)
        && candidate.version_number > 0
        && candidate.supersedes_backlog_artifact_id === mutation.backlogArtifactId
        && Array.isArray(candidate.backlog_items)
        && candidate.backlog_items.every((item) => reviewObject(item) !== null)
        && typeof candidate.is_complete === 'boolean'
        && Array.isArray(candidate.clarifying_questions);
}

function correctedPendingBacklogState(state, mutation) {
    const backlog = backlogPendingReview(state);
    const backlogReviewDecisions = currentBacklogDecisions(state)?.filter((decision) => (
        decision?.node_id === 'backlog.review'
        && decision.instance_key === null
        && decision.request_kind === 'decide_backlog'
    ));
    const pendingDecision = backlogReviewDecisions?.filter((decision) => (
        decision?.node_id === 'backlog.review'
        && decision.instance_key === null
        && decision.request_kind === 'decide_backlog'
        && decision.category === 'waiting'
        && decision.recommendation_kind === 'required'
        && decision.reason_code === 'BACKLOG_REVIEW_REQUIRED'
        && decision.decision_fingerprint === backlog?.binding?.decision_fingerprint
    ));
    const review = reviewObject(backlog?.review);
    const candidate = reviewObject(review?.candidate);
    const lineage = reviewObject(review?.lineage);
    const specification = reviewObject(lineage?.specification);
    const productGoal = reviewObject(lineage?.product_goal);
    return backlog !== null
        && Object.keys(backlog).length === 2
        && 'binding' in backlog
        && 'review' in backlog
        && review?.phase === 'backlog'
        && reviewObject(review?.review) !== null
        && review.review.state === 'pending'
        && completeCorrectedBacklogCandidate(candidate, mutation)
        && isConcreteReviewIdentity(specification?.spec_version_id, specification?.spec_hash)
        && isConcreteReviewIdentity(
            productGoal?.product_goal_artifact_id,
            productGoal?.product_goal_fingerprint,
        )
        && backlogReviewDecisions?.length === 1
        && pendingDecision?.length === 1
        && exactBacklogPendingReferences(
            pendingDecision[0],
            candidate,
            specification,
            productGoal,
        )
        && !hasCurrentFeedbackDecision(state, mutation.decisionFingerprint)
        && !hasAdvertisedBacklogCorrectionAction(state);
}

function reconcileBacklogCorrectionMutation(mutationAtStart) {
    if (!mutationAtStart
        || activeBacklogCorrectionMutation?.token !== mutationAtStart.token
        || activeBacklogCorrectionMutation.phase !== mutationAtStart.phase
        || mutationAtStart.phase === 'submitting') {
        return null;
    }
    const mutation = activeBacklogCorrectionMutation;
    const continuation = backlogFeedbackContinuationProjection(lifecycleState);
    const correction = backlogCorrectionActionBinding(lifecycleState, continuation);
    const correctedPending = correctedPendingBacklogState(lifecycleState, mutation);
    const completeContinuation = continuation.kind === 'display'
        && (continuation.mode === 'active'
            ? correction.kind === 'unavailable' && correction.reason === 'active'
            : correction.kind === 'ready');
    const qualifyingBacklogState = mutation.phase === 'recovering_failure'
        ? (completeContinuation || correctedPending || validNonFeedbackBacklogState(lifecycleState, mutation))
        : (correctedPending || validNonFeedbackBacklogState(lifecycleState, mutation));
    const focusMutation = qualifyingBacklogState && mutation.focusIntent
        ? { ...mutation }
        : null;
    if (qualifyingBacklogState) activeBacklogCorrectionMutation = null;
    return focusMutation;
}

function backlogCorrectionFocusSelector(state) {
    const backlog = reviewObject(state?.planningReviews?.backlog);
    if (backlogPendingReviewIsValid(backlog)) return '[data-planning-review-card="backlog"]';
    if (!backlog || (('binding' in backlog || 'review' in backlog) && !backlogPendingReviewIsValid(backlog))) {
        return '[data-backlog-feedback-projection-error="true"]';
    }
    const continuation = backlogFeedbackContinuationProjection(state);
    const correction = backlogCorrectionActionBinding(state, continuation);
    if (continuation.kind !== 'display' || correction.kind === 'error') {
        return '[data-backlog-feedback-projection-error="true"]';
    }
    if (continuation.mode === 'active') return '[data-backlog-feedback-continuation="true"]';
    if (correction.kind === 'ready') return '[data-backlog-correction-action="true"]:not([disabled])';
    return null;
}

function focusBacklogCorrectionTarget() {
    const selector = backlogCorrectionFocusSelector(lifecycleState);
    const target = selector ? document.querySelector(selector) : null;
    const fallback = selector === '[data-backlog-correction-action="true"]:not([disabled])'
        ? document.querySelector('[data-backlog-feedback-continuation="true"]')
        : null;
    (target ?? fallback)?.focus?.();
}

function consumeBacklogCorrectionFocus(mutation = activeBacklogCorrectionMutation) {
    if (!mutation?.focusIntent) return;
    focusBacklogCorrectionTarget();
    if (activeBacklogCorrectionMutation?.token === mutation.token) {
        activeBacklogCorrectionMutation = {
            ...activeBacklogCorrectionMutation,
            focusIntent: false,
        };
    }
}

function consumeBacklogFeedbackFocus() {
    if (!backlogFeedbackFocusIntent) return;
    focusBacklogCorrectionTarget();
    backlogFeedbackFocusIntent = false;
}

function reviewCandidateFingerprint(state, scope) {
    return {
        goal: state?.goal?.candidate?.fingerprint,
        specification: state?.specification?.candidate?.candidate_fingerprint,
        vision: state?.vision?.candidate?.review_fingerprint,
    }[scope] ?? null;
}

function captureReviewBinding(state, scope, decision) {
    const requestKind = {
        goal: 'decide_product_goal_review',
        specification: 'decide_specification',
        vision: 'decide_vision_review',
    }[scope];
    const action = captureAction(findAction(state?.actions, requestKind));
    const expectedCandidate = reviewCandidateFingerprint(state, scope);
    if (!action || typeof expectedCandidate !== 'string' || !expectedCandidate) return null;
    return {
        kind: 'review',
        scope,
        decision,
        action,
        expectedCandidate,
    };
}

function reviewSubmission(binding, rationale) {
    return {
        action: binding.action,
        expectedCandidate: binding.expectedCandidate,
        fields: {
            decision: binding.decision,
            rationale: rationale || 'Accepted in the dashboard.',
        },
    };
}

function capturePlanningReview(scope, index, decision) {
    const reviewState = lifecycleState.planningReviews ?? {};
    const selected = scope === 'story'
        ? reviewState.stories?.items?.[index]
        : {
            backlog: reviewState.backlog,
            roadmap: reviewState.roadmap,
            sprint: reviewState.sprintPlan,
        }[scope];
    return planningReviewBinding(selected, scope, decision);
}

function planningReviewBinding(selected, scope, decision) {
    const binding = selected?.binding;
    if (!binding?.decision_fingerprint) return null;
    if (scope === 'story' && decision === 'accepted' && !isStoryReviewAcceptable(selected?.review)) {
        return null;
    }
    let titleScope = scope === 'sprint' ? 'Sprint plan' : scope;
    let titleSuffix = '';
    if (scope === 'story') {
        const instanceKey = binding.instance_key ?? '';
        const pbiId = instanceKey.startsWith('backlog_item:')
            ? instanceKey.slice('backlog_item:'.length)
            : (instanceKey || null);
        const req = selected?.review?.lineage?.backlog_item?.requirement;
        if (pbiId) {
            titleScope = `Story review for ${pbiId}`;
            if (req) titleSuffix = `: ${req}`;
        }
    }
    const verb = decision === 'accepted' ? 'Accept' : decision === 'feedback' ? 'Request changes for' : 'Reject';
    const titlePrefix = scope === 'story' && binding.instance_key ? `${verb} ` : `${verb} this `;
    return {
        kind: 'planning-review',
        scope,
        decision,
        binding: { ...binding },
        endpoint: {
            backlog: 'backlog/decide',
            roadmap: 'roadmap/decide',
            story: 'story/decide',
            sprint: 'sprint/decide',
        }[scope],
        title: `${titlePrefix}${titleScope}${titleSuffix}`,
        description: 'Confirm the exact evidence shown above. A changed review must be loaded again.',
        label: 'Rationale',
        required: decision !== 'accepted',
        submitLabel: decision === 'accepted' ? 'Accept' : 'Submit',
    };
}

function openHumanDialog(config) {
    pendingHumanAction = config;
    setText('human-action-kicker', config.kicker ?? 'Human decision');
    setText('human-action-title', config.title);
    setText('human-action-description', config.description);
    const rationaleGroup = document.getElementById('human-action-rationale-group');
    const rationale = document.getElementById('human-action-rationale');
    const rationaleLabel = document.getElementById('human-action-rationale-label');
    const pathGroup = document.getElementById('human-action-path-group');
    const path = document.getElementById('human-action-path');
    const isPath = config.field === 'path';
    const hideRationale = isPath || config.field === 'none' || config.hideRationale === true;
    if (rationaleGroup && rationale && rationaleLabel) {
        rationaleGroup.classList.toggle('hidden', hideRationale);
        rationale.required = !hideRationale && config.required !== false;
        rationale.value = '';
        rationaleLabel.textContent = config.label ?? 'Rationale';
    }
    if (pathGroup && path) {
        pathGroup.classList.toggle('hidden', !isPath);
        path.required = isPath;
        path.value = config.initialPath ?? '';
    }
    setText('human-action-submit', config.submitLabel ?? 'Confirm');
    setDialogError('');
    document.getElementById('human-action-dialog')?.showModal();
    window.setTimeout(() => (isPath ? path : hideRationale ? document.getElementById('human-action-submit') : rationale)?.focus(), 0);
}

function reviewDialogCopy(scope, decision) {
    const subject = {
        goal: 'Product Goal',
        specification: 'Specification',
        vision: 'Project Vision',
    }[scope];
    if (decision === 'accepted') {
        return {
            title: `Accept ${subject}`,
            description: `Confirm that this exact ${subject} candidate should become current.`,
            required: false,
            submitLabel: 'Accept',
        };
    }
    if (decision === 'feedback') {
        return {
            title: `Give ${subject} feedback`,
            description: 'Record the specific change needed before another review.',
            required: true,
            submitLabel: 'Send feedback',
        };
    }
    return {
        title: `Reject ${subject}`,
        description: `Record why this exact ${subject} candidate cannot proceed.`,
        required: true,
        submitLabel: 'Reject',
    };
}

async function readPositionActions() {
    const payload = await requestJson(`/api/projects/${selectedProjectId}/position`);
    return {
        position: payload.data ?? {},
        actions: payload.actions ?? [],
    };
}

async function submitHumanAction() {
    if (!pendingHumanAction) return;
    const rationale = document.getElementById('human-action-rationale')?.value.trim() ?? '';
    const path = document.getElementById('human-action-path')?.value.trim() ?? '';
    const pending = pendingHumanAction;

    if (pending.required !== false && pending.field !== 'path' && !rationale) {
        throw new Error('Enter a rationale before continuing.');
    }
    if (pending.field === 'path' && !path) {
        throw new Error('Enter a local repository path.');
    }

    if (pending.kind === 'review') {
        const submission = reviewSubmission(pending, rationale);
        await postAction(submission.action, submission.fields, {
            expectedCandidate: submission.expectedCandidate,
        });
    } else if (pending.kind === 'planning-review') {
        document.querySelectorAll('[data-planning-review]').forEach((button) => {
            button.disabled = true;
        });
        try {
            await postAction(
                { endpoint: pending.endpoint },
                {
                    decision: pending.decision,
                    rationale: rationale || 'Accepted in the dashboard.',
                },
                {
                    expectedDecision: pending.binding.decision_fingerprint,
                    expectedInstance: pending.binding.instance_key,
                },
            );
            if (pending.scope === 'backlog' && pending.decision === 'feedback') {
                backlogFeedbackFocusIntent = true;
            }
        } catch (error) {
            closeHumanDialog();
            try {
                await loadDashboard();
            } catch (_loadError) {
                // Old controls remain disabled when refresh also fails.
            }
            setProjectError(`This review changed. Review the current evidence again. ${error.message}`);
            return;
        }
    } else if (pending.kind === 'delivery-generation') {
        closeHumanDialog();
        await runDirectAction(
            pending.requestKind,
            pending.button,
            null,
            pending.fields ?? {},
        );
        return;
    } else if (pending.kind === 'sprint-start') {
        await runSprintStart(pending.binding, pending.button);
        return;
    } else if (pending.kind === 'goal-outcome') {
        const requestKind = pending.outcome === 'fulfilled'
            ? 'fulfill_product_goal'
            : 'abandon_product_goal';
        await postAction(findAction(lifecycleState.actions, requestKind), { rationale });
    } else if (pending.kind === 'repository') {
        await postAction({ endpoint: 'repository' }, { path });
    } else if (pending.kind === 'vision-revision') {
        await postAction(findAction(lifecycleState.actions, 'begin_vision_revision'), { reason: rationale });
    }
    closeHumanDialog();
    await loadDashboard();
}

async function runBacklogCorrection(binding, button) {
    if (activeBacklogCorrectionMutation) return false;
    const token = crypto.randomUUID();
    let mutationCompleted = false;
    activeBacklogCorrectionMutation = {
        token,
        phase: 'submitting',
        action: binding.action,
        backlogArtifactId: binding.backlogArtifactId,
        decisionFingerprint: binding.decisionFingerprint,
        focusIntent: true,
    };
    setDeliveryActionBusy(button, true, 'record_backlog_draft', true);
    setProjectError('');
    try {
        await postAction(binding.action);
        mutationCompleted = true;
        if (activeBacklogCorrectionMutation?.token === token
            && activeBacklogCorrectionMutation.phase === 'submitting') {
            activeBacklogCorrectionMutation = {
                ...activeBacklogCorrectionMutation,
                phase: 'awaiting_authority',
            };
            renderDashboard();
        }
        const refreshed = await loadDashboard();
        if (refreshed !== true) {
            throw new Error(
                'The Backlog correction was accepted, but the current project projection could not be reloaded. Controls remain locked until a successful refresh.',
            );
        }
        if (activeBacklogCorrectionMutation?.token === token) {
            throw new Error(
                'The Backlog correction was accepted, but the current project projection did not confirm the result. Controls remain locked until a successful refresh.',
            );
        }
    } catch (error) {
        if (!mutationCompleted
            && activeBacklogCorrectionMutation?.token === token
            && activeBacklogCorrectionMutation.phase === 'submitting') {
            activeBacklogCorrectionMutation = {
                ...activeBacklogCorrectionMutation,
                phase: 'recovering_failure',
            };
            renderDashboard();
            try {
                await loadDashboard();
            } catch (_loadError) {
                // Keep correction controls locked until a current projection arrives.
            }
        }
        const localMessage = error.message;
        const currentControls = currentDeliveryActionContainers(
            binding.action,
            'record_backlog_draft',
        );
        setProjectError(localMessage);
        (currentControls.length ? currentControls : [button]).forEach(
            (control) => setDeliveryActionStatus(control, localMessage),
        );
    } finally {
        if (activeBacklogCorrectionMutation?.token === token) {
            reapplyActiveBacklogCorrectionMutation();
        }
    }
    return true;
}

function sprintStartBindingsMatch(left, right) {
    return Boolean(
        left && right
        && left.decisionFingerprint === right.decisionFingerprint
        && left.sprintId === right.sprintId
        && left.sprintPlanArtifactId === right.sprintPlanArtifactId
        && left.sprintPlanArtifactDecisionId === right.sprintPlanArtifactDecisionId
        && left.planFingerprint === right.planFingerprint
        && left.candidateSetFingerprint === right.candidateSetFingerprint
        && left.taskContentFingerprint === right.taskContentFingerprint
    );
}

function completeSprintStartReconciliation(token) {
    if (activeSprintMutation?.token === token) activeSprintMutation = null;
    sprintStartRetry = null;
    closeHumanDialog();
    renderDashboard();
}

async function runSprintStart(binding, button) {
    if (activeSprintMutation) return false;
    const token = sprintStartBindingsMatch(sprintStartRetry?.binding, binding)
        ? sprintStartRetry.token
        : `dashboard-${crypto.randomUUID()}`;
    sprintStartRetry = null;
    let mutationCompleted = false;
    activeSprintMutation = { token, binding, phase: 'submitting' };
    button.disabled = true;
    button.setAttribute('aria-disabled', 'true');
    button.setAttribute('aria-busy', 'true');
    const label = button.querySelector?.('[data-sprint-start-label="true"]');
    if (label) label.textContent = 'Starting Sprint...';
    setProjectError('');
    try {
        await postAction(
            binding.action,
            { idempotency_key: token },
            { expectedDecision: binding.decisionFingerprint },
        );
        mutationCompleted = true;
        if (activeSprintMutation?.token === token) {
            activeSprintMutation = { ...activeSprintMutation, phase: 'awaiting_authority' };
        }
        const refreshed = await loadDashboard();
        if (refreshed !== true || !sprintStartConfirmed(lifecycleState, binding)) {
            throw new Error(
                'Sprint start was accepted, but the authoritative status did not confirm the same Sprint as active. Controls remain locked until a successful refresh.',
            );
        }
        completeSprintStartReconciliation(token);
        return true;
    } catch (error) {
        if (!mutationCompleted && activeSprintMutation?.token === token) {
            activeSprintMutation = { ...activeSprintMutation, phase: 'reconciling' };
            let refreshed = false;
            try {
                refreshed = await loadDashboard() === true;
            } catch (_loadError) {
                // Preserve the uncertainty lock until authority can be reloaded.
            }
            if (refreshed && sprintStartConfirmed(lifecycleState, binding)) {
                completeSprintStartReconciliation(token);
                return true;
            }
            const current = refreshed
                ? sprintStartBinding(
                    lifecycleState?.sprintStatus?.data,
                    lifecycleState?.position,
                    lifecycleState?.actions,
                )
                : null;
            if (current) {
                if (sprintStartBindingsMatch(current, binding)) {
                    sprintStartRetry = { token, binding };
                }
                if (activeSprintMutation?.token === token) activeSprintMutation = null;
                renderDashboard();
                throw new Error(
                    `Sprint start was not confirmed. Review the current Sprint and retry. ${error.message}`,
                );
            }
        }
        throw error;
    }
}

async function runDirectAction(requestKind, button, fallbackEndpoint = null, fields = {}) {
    if (button.disabled) return false;
    if (requestKind === 'record_backlog_draft' && activeBacklogCorrectionMutation) return false;
    const backlogCorrection = requestKind === 'record_backlog_draft'
        ? captureBacklogCorrectionBinding(lifecycleState, button)
        : null;
    if (backlogCorrection) return runBacklogCorrection(backlogCorrection, button);
    const isSpecificationStructuring = requestKind === 'structure_specification';
    if (isSpecificationStructuring && activeSpecificationMutation) return false;
    const isDeliveryGeneration = Boolean(DELIVERY_ACTION_CONFIG[requestKind]);
    const setBusy = (targetButton, busy) => {
        if (isSpecificationStructuring) {
            setSpecificationStructuringBusy(targetButton, busy);
        } else if (isDeliveryGeneration) {
            setDeliveryActionBusy(targetButton, busy, requestKind);
        } else {
            setSpecificationContinuationBusy(targetButton, busy);
        }
    };
    let specificationBinding = null;
    let specificationMutationToken = null;
    let deliveryBinding = null;
    let deliveryReconciled = false;
    let mutationCompleted = false;
    setBusy(button, true);
    setProjectError('');
    try {
        if (isSpecificationStructuring) {
            const binding = captureSpecificationStructuringBinding(lifecycleState);
            if (!binding) {
                throw new Error(
                    'This Specification action changed. Refresh and choose from the current source state.',
                );
            }
            specificationBinding = binding;
            specificationMutationToken = crypto.randomUUID();
            activeSpecificationMutation = {
                token: specificationMutationToken,
                kind: 'structuring',
            };
            await postAction(
                binding.action,
                {},
                { expectedDecision: binding.expectedDecision },
            );
            mutationCompleted = true;
        } else if (isDeliveryGeneration) {
            const binding = captureDeliveryActionBinding(
                lifecycleState,
                button,
                requestKind,
            );
            if (!binding) {
                throw new Error(
                    'This delivery action changed. Refresh and choose a current action.',
                );
            }
            deliveryBinding = binding;
            const deliveryFields = { ...fields };
            if (requestKind === 'record_sprint_plan') {
                Object.assign(
                    deliveryFields,
                    sprintCapacityFields(lifecycleState, fields.max_story_points),
                );
                const candidateIds = sprintGenerationCandidateIds(lifecycleState);
                if (candidateIds === null) {
                    throw new Error(
                        'Sprint candidate projection changed. Reload and choose the current candidates.',
                    );
                }
                deliveryFields.selected_story_ids = candidateIds;
            }
            if (binding.instance_key !== null && binding.instance_key !== undefined) {
                deliveryFields.instance_key = binding.instance_key;
            }
            let deliveryOptions = {};
            if (requestKind === 'record_story_draft') {
                const details = deliveryGenerationActionDetails(
                    binding,
                    lifecycleState.position,
                    lifecycleState.planningReviews,
                    lifecycleState,
                );
                if (!details) {
                    throw new Error(
                        'This Story action changed. Reload and choose a current action.',
                    );
                }
                if (details.correctionBinding) {
                    deliveryFields.accepted_story_artifact_id = (
                        details.correctionBinding.acceptedStoryArtifactId
                    );
                    deliveryFields.accepted_story_artifact_fingerprint = (
                        details.correctionBinding.acceptedStoryArtifactFingerprint
                    );
                    deliveryOptions = {
                        expectedDecision: details.correctionBinding.expectedDecision,
                    };
                }
            }
            await postAction(binding, deliveryFields, deliveryOptions);
            mutationCompleted = true;
        } else {
            const action = findAction(lifecycleState.actions, requestKind)
                ?? (fallbackEndpoint ? { endpoint: fallbackEndpoint } : null);
            await postAction(action);
        }
        const refreshed = await loadDashboard();
        if (isDeliveryGeneration) {
            deliveryReconciled = refreshed === true;
            if (!deliveryReconciled) {
                throw new Error(
                    'The dashboard reload was superseded before current delivery actions were confirmed.',
                );
            }
        }
    } catch (error) {
        if (isSpecificationStructuring) {
            let refreshed = false;
            if (!mutationCompleted) {
                try {
                    refreshed = await loadDashboard();
                } catch (_loadError) {
                    // Keep the captured action visible when reconciliation also fails.
                }
            }
            const currentButton = document.querySelector?.(
                '[data-direct-action="structure_specification"]',
            ) ?? button;
            const localMessage = mutationCompleted
                ? `Specification structuring completed, but the dashboard could not reload. ${error.message}`
                : specificationStructuringFailureMessage(
                    specificationBinding,
                    error,
                    refreshed,
                );
            setProjectError(error.message);
            setSpecificationStructuringStatus(currentButton, localMessage);
        } else if (isDeliveryGeneration) {
            if (!mutationCompleted) {
                try {
                    deliveryReconciled = await loadDashboard() === true;
                } catch (_loadError) {
                    // Keep the captured action visible when reconciliation also fails.
                }
            }
            const localMessage = mutationCompleted
                ? `Delivery generation completed, but the dashboard could not reload. ${error.message}`
                : (requestKind === 'record_sprint_plan'
                    && error.code === 'SPRINT_CAPACITY_REQUIRED'
                    ? 'Enter a positive Maximum story points value before generating a Sprint plan.'
                    : error.message);
            const currentControls = currentDeliveryActionContainers(
                deliveryBinding,
                requestKind,
            );
            setProjectError(localMessage);
            (currentControls.length ? currentControls : [button]).forEach(
                (control) => setDeliveryActionStatus(control, localMessage),
            );
        } else {
            setProjectError(error.message);
            return true;
        }
    } finally {
        if (isSpecificationStructuring) {
            if (activeSpecificationMutation?.token === specificationMutationToken) {
                activeSpecificationMutation = null;
            }
            const currentButton = document.querySelector?.(
                '[data-direct-action="structure_specification"]',
            ) ?? button;
            setSpecificationStructuringBusy(currentButton, false);
        } else if (isDeliveryGeneration && !deliveryReconciled) {
            setDeliveryActionBusy(button, false, requestKind, true);
        } else {
            setBusy(button, false);
        }
    }
    return true;
}

function setDeliveryActionStatus(control, message) {
    const action = deliveryActionContainer(control);
    const status = action?.querySelector?.('[data-delivery-action-status="true"]');
    if (!status) return;
    status.textContent = message;
    status.hidden = !message;
}

function setDeliveryActionBusy(control, busy, requestKind, keepDisabled = false) {
    const action = deliveryActionContainer(control);
    const controls = action?.querySelectorAll?.(
        'button, input, textarea, select',
    ) ?? [control];
    Array.from(controls).forEach((item) => {
        if (item) item.disabled = busy || keepDisabled;
    });
    if (action?.dataset) {
        if (busy) {
            action.dataset.submitting = 'true';
        } else {
            delete action.dataset.submitting;
        }
    }
    const config = DELIVERY_ACTION_CONFIG[requestKind];
    const label = action?.querySelector?.('[data-delivery-action-label="true"]')
        ?? control?.querySelector?.('[data-delivery-action-label="true"]');
    if (busy) {
        control?.setAttribute?.('aria-busy', 'true');
        let busyText = config?.busyLabel ?? 'Generating...';
        if (requestKind === 'record_backlog_draft'
            && (label?.textContent ?? '').startsWith('Regenerate Backlog from feedback')) {
            busyText = 'Regenerating Backlog from feedback...';
        }
        if (requestKind === 'record_story_draft') {
            const instance = action?.dataset?.deliveryActionInstance
                ?? control?.dataset?.deliveryActionInstance;
            const pbiId = instance?.startsWith('backlog_item:')
                ? instance.slice('backlog_item:'.length)
                : (instance || null);
            if (pbiId) {
                const idleText = label?.textContent ?? '';
                const isRevision = idleText.startsWith('Revise');
                const isCorrection = idleText.startsWith('Correct');
                const verb = isRevision ? 'Revising' : isCorrection ? 'Correcting' : 'Generating';
                busyText = `${verb} Stories for ${pbiId}...`;
            }
        }
        if (label) {
            label.dataset.idleLabel = label.textContent;
            label.textContent = busyText;
        }
        setDeliveryActionStatus(
            control,
            busyText,
        );
        return;
    }
    control?.removeAttribute?.('aria-busy');
    if (label?.dataset?.idleLabel) {
        label.textContent = label.dataset.idleLabel;
        delete label.dataset.idleLabel;
    }
}

function specificationStructuringFailureMessage(binding, error, refreshed) {
    if (error?.status !== 409 || error?.code !== 'SPECIFICATION_PRODUCER_FAILED') {
        const nextStep = refreshed
            ? 'The dashboard was refreshed. Verify the current candidate before retrying.'
            : 'Refresh the dashboard and verify the current candidate before retrying.';
        return `Specification structuring outcome is uncertain. ${error.message} ${nextStep}`;
    }
    const currentState = binding?.mode === 'same-source-feedback'
        ? 'The prior candidate and Feedback remain current.'
        : 'The registered source remains current.';
    return `Specification structuring failed. ${error.message} No new candidate was produced. ${currentState}`;
}

function setSpecificationStructuringStatus(control, message) {
    const action = control?.closest?.(
        '[data-specification-structuring-action="true"]',
    );
    const status = action?.querySelector?.(
        '[data-specification-structuring-status="true"]',
    );
    if (!status) return;
    status.textContent = message;
    status.hidden = !message;
}

function setSpecificationStructuringBusy(control, busy) {
    setSpecificationContinuationBusy(control, busy);
    setSpecificationRevisionRegistrationBusy(control, busy);
    const label = control?.querySelector?.(
        '[data-specification-structuring-label="true"]',
    );
    if (busy) {
        control?.setAttribute?.('aria-busy', 'true');
        if (label) {
            label.dataset.idleLabel = label.textContent;
            label.textContent = 'Structuring Specification...';
        }
        setSpecificationStructuringStatus(
            control,
            'Structuring Specification...',
        );
        return;
    }
    control?.removeAttribute?.('aria-busy');
    if (label?.dataset?.idleLabel) {
        label.textContent = label.dataset.idleLabel;
        delete label.dataset.idleLabel;
    }
}

function setSpecificationContinuationBusy(control, busy) {
    const continuation = control?.closest?.(
        '[data-specification-feedback-continuation="true"]',
    );
    const controls = continuation?.querySelectorAll?.(
        'button, input, textarea, select',
    ) ?? [control];
    Array.from(controls).forEach((item) => {
        if (item) item.disabled = busy;
    });
}

function setSpecificationRevisionRegistrationBusy(control, busy) {
    const sourceState = control?.closest?.(
        '[data-specification-source-state="true"]',
    );
    const revision = sourceState?.querySelector?.(
        '[data-specification-revision-registration="true"]',
    );
    if (!revision) return;
    const controls = revision.querySelectorAll('button, input, textarea, select');
    Array.from(controls).forEach((item) => {
        if (item) item.disabled = busy;
    });
    if (busy) {
        revision.removeAttribute('open');
        revision.setAttribute('aria-disabled', 'true');
        revision.setAttribute('inert', '');
        return;
    }
    revision.removeAttribute('aria-disabled');
    revision.removeAttribute('inert');
}

function setSpecificationSourceRegistrationBusy(form, busy) {
    const panel = form?.closest?.('#specification-panel')
        ?? document.getElementById('specification-panel');
    const controls = panel?.querySelectorAll?.(
        '[data-specification-source-form="true"] button, [data-specification-source-form="true"] input, [data-specification-source-form="true"] textarea, [data-specification-source-form="true"] select, [data-direct-action="structure_specification"]',
    ) ?? [];
    Array.from(controls).forEach((control) => {
        control.disabled = busy;
        control.toggleAttribute('aria-disabled', busy);
    });
}

function reapplyActiveSpecificationMutation() {
    const mutation = activeSpecificationMutation;
    if (!mutation) return;
    if (mutation.kind === 'structuring') {
        const button = document.querySelector(
            '[data-direct-action="structure_specification"]',
        );
        if (button) setSpecificationStructuringBusy(button, true);
        return;
    }
    document.querySelectorAll('[data-specification-source-form="true"]').forEach(
        (form) => setSpecificationSourceRegistrationBusy(form, true),
    );
}

function installInteractions() {
    document.addEventListener('submit', async (event) => {
        const form = event.target;
        if (form?.id === 'human-action-form') {
            event.preventDefault();
            const submit = document.getElementById('human-action-submit');
            if (submit) submit.disabled = true;
            try {
                await submitHumanAction();
            } catch (error) {
                setDialogError(error.message);
            } finally {
                if (submit) submit.disabled = false;
            }
            return;
        }
        if (form?.dataset?.specificationSourceForm === 'true') {
            event.preventDefault();
            if (form.dataset.submitting === 'true' || activeSpecificationMutation) return;
            const binding = captureSpecificationSourceRegistrationBinding(
                lifecycleState,
            );
            if (!binding) {
                setProjectError(
                    'This Specification source choice changed. Refresh and choose from the current state.',
                );
                return;
            }
            const sourcePath = form.querySelector('[name="source_path"]')?.value ?? '';
            const preparationCapability = form.querySelector('[name="preparation_capability"]')?.value ?? '';
            const adrPaths = form.querySelector('[name="adr_paths"]')?.value ?? '';
            const submission = specificationSourceSubmission(
                [binding.action],
                sourcePath,
                preparationCapability,
                adrPaths,
            );
            if (!submission.fields.source_path) {
                setProjectError('Enter a repository-relative Specification source path.');
                return;
            }
            if (!submission.fields.preparation_capability) {
                setProjectError('Select the preparation capability that produced this source.');
                return;
            }
            const submit = form.querySelector('button[type="submit"]');
            form.dataset.submitting = 'true';
            const specificationMutationToken = crypto.randomUUID();
            activeSpecificationMutation = {
                token: specificationMutationToken,
                kind: 'source-registration',
            };
            setSpecificationSourceRegistrationBusy(form, true);
            setProjectError('');
            try {
                await postAction(
                    submission.action,
                    submission.fields,
                    { expectedDecision: binding.expectedDecision },
                );
                await loadDashboard();
            } catch (error) {
                setProjectError(error.message);
            } finally {
                if (activeSpecificationMutation?.token === specificationMutationToken) {
                    activeSpecificationMutation = null;
                }
                delete form.dataset.submitting;
                const currentForm = document.querySelector(
                    'form[data-specification-source-form="true"]',
                ) ?? form;
                setSpecificationSourceRegistrationBusy(currentForm, false);
                if (submit && currentForm === form) submit.disabled = false;
            }
            return;
        }
        const deliveryRequestKind = form?.dataset?.deliveryGenerationForm;
        if (DELIVERY_ACTION_CONFIG[deliveryRequestKind]) {
            event.preventDefault();
            if (form.dataset.submitting === 'true') return;
            const submit = form.querySelector('button[type="submit"]');
            if (!submit) return;
            const fields = {};
            if (deliveryRequestKind === 'record_sprint_plan') {
                const teamName = form.querySelector('[name="team_name"]');
                const maxStoryPoints = form.querySelector('[name="max_story_points"]');
                try {
                    Object.assign(fields, sprintTeamOverrideFields(teamName?.value));
                } catch (error) {
                    setProjectError(error.message);
                    return;
                }
                try {
                    Object.assign(
                        fields,
                        sprintCapacityFields(lifecycleState, maxStoryPoints?.value),
                    );
                } catch (error) {
                    setProjectError('Enter a positive whole-number Maximum story points value before generating a Sprint plan.');
                    syncSprintCapacityButton(form);
                    return;
                }
            }
            await runDirectAction(
                deliveryRequestKind,
                submit,
                null,
                fields,
            );
            return;
        }
        const scope = form?.dataset?.interviewScope;
        if (!scope) return;
        event.preventDefault();
        if (form.dataset.submitting === 'true') return;
        const textarea = document.getElementById(`${scope}-response`);
        const text = textarea?.value.trim() ?? '';
        if (!text) return;
        const requestKind = scope === 'vision'
            ? 'record_vision_interview_turn'
            : 'record_product_goal_interview_turn';
        const submit = form.querySelector('button[type="submit"]');
        const submitLabel = submit?.querySelector?.('[data-interview-submit-label]');
        const idleLabel = submitLabel?.textContent ?? 'Send response';
        form.dataset.submitting = 'true';
        if (submit) submit.disabled = true;
        submit?.setAttribute?.('aria-busy', 'true');
        if (submitLabel) submitLabel.textContent = 'Sending...';
        if (textarea) textarea.disabled = true;
        setProjectError('');
        setInterviewStatus(scope, '');
        try {
            await postAction(findAction(lifecycleState.actions, requestKind), { text });
            await loadDashboard();
        } catch (error) {
            setProjectError(error.message);
            setInterviewStatus(scope, `Response was not sent. ${error.message}`);
        } finally {
            delete form.dataset.submitting;
            if (submit) submit.disabled = false;
            submit?.removeAttribute?.('aria-busy');
            if (submitLabel) submitLabel.textContent = idleLabel;
            if (textarea) textarea.disabled = false;
        }
    });

    document.addEventListener('input', (event) => {
        const form = event.target?.closest?.('[data-delivery-generation-form="record_sprint_plan"]');
        if (form) syncSprintCapacityButton(form);
    });

    document.addEventListener('click', async (event) => {
        const button = event.target.closest('button');
        if (!button) return;
        if (button.dataset.reviewScope) {
            const copy = reviewDialogCopy(button.dataset.reviewScope, button.dataset.reviewDecision);
            const binding = captureReviewBinding(
                lifecycleState,
                button.dataset.reviewScope,
                button.dataset.reviewDecision,
            );
            if (!binding) {
                setProjectError('This review changed. Refresh and review the current candidate.');
                return;
            }
            openHumanDialog({
                ...copy,
                ...binding,
            });
            return;
        }
        if (button.dataset.planningReview) {
            const binding = capturePlanningReview(
                button.dataset.planningReview,
                Number.parseInt(button.dataset.reviewIndex ?? '0', 10),
                button.dataset.reviewDecision,
            );
            if (!binding) {
                setProjectError('This review changed. Refresh and review the current evidence.');
                return;
            }
            openHumanDialog(binding);
            return;
        }
        if (button.dataset.goalOutcome) {
            const fulfilled = button.dataset.goalOutcome === 'fulfilled';
            openHumanDialog({
                kind: 'goal-outcome',
                outcome: button.dataset.goalOutcome,
                title: fulfilled ? 'Fulfill Product Goal' : 'Abandon Product Goal',
                description: fulfilled
                    ? 'Confirm that the observable Goal outcome has been achieved.'
                    : 'Confirm that work on this Goal should stop.',
                label: 'Outcome rationale',
                submitLabel: fulfilled ? 'Fulfill Goal' : 'Abandon Goal',
                required: true,
            });
            return;
        }
        if (button.dataset.repositoryAction === 'attach') {
            openHumanDialog({
                kind: 'repository',
                field: 'path',
                title: lifecycleState.repository?.repository ? 'Replace repository' : 'Attach repository',
                description: 'Use the local Git worktree that provides context for this Project.',
                initialPath: lifecycleState.repository?.repository?.worktree_path ?? '',
                submitLabel: lifecycleState.repository?.repository ? 'Replace' : 'Attach',
            });
            return;
        }
        if (button.dataset.repositoryAction === 'refresh') {
            runDirectAction('refresh_repository_binding', button, 'repository/refresh');
            return;
        }
        if (button.dataset.directAction === 'record_story_draft') {
            const binding = captureDeliveryActionBinding(
                lifecycleState,
                button,
                'record_story_draft',
            );
            if (!binding) {
                setProjectError('This delivery action changed. Refresh and choose a current action.');
                return;
            }
            const details = deliveryGenerationActionDetails(
                binding,
                lifecycleState.position,
                lifecycleState.planningReviews,
                lifecycleState,
            );
            if (!details || !details.requirement) {
                setProjectError('Requirement summary unavailable for this Story action. Refresh the project state.');
                return;
            }
            openHumanDialog({
                kind: 'delivery-generation',
                requestKind: 'record_story_draft',
                button,
                binding,
                title: `${details.intentVerb} Stories for ${details.pbiId}`,
                description: `Confirm Story ${details.intentLabel.toLowerCase()} for ${details.pbiId}: ${details.requirement}`,
                submitLabel: `${details.intentVerb} Stories`,
                field: 'none',
                required: false,
                hideRationale: true,
            });
            return;
        }
        if (button.dataset.directAction === 'start_sprint') {
            const binding = captureSprintStartControlBinding(lifecycleState, button);
            if (!binding) {
                setProjectError(
                    'This Sprint start action changed. Reload and review the current accepted Sprint.',
                );
                return;
            }
            openHumanDialog({
                kind: 'sprint-start',
                binding,
                button,
                title: `Start Sprint #${binding.sprintId}`,
                description: 'Confirm starting the exact accepted Sprint plan shown on this page.',
                submitLabel: 'Start Sprint',
                field: 'none',
                required: false,
                hideRationale: true,
            });
            return;
        }
        if (button.dataset.directAction) {
            runDirectAction(button.dataset.directAction, button);
            return;
        }
        if (button.dataset.storyStructuralReconcileId || button.dataset.storySelectionIntent) {
            if (activeStoryMutation) return;
            const storyId = Number.parseInt(
                button.dataset.storyStructuralReconcileId || button.dataset.storySelectionId,
                10,
            );
            if (!Number.isInteger(storyId) || storyId <= 0) return;
            const intent = button.dataset.storySelectionIntent || null;
            const fingerprint = button.dataset.storySelectionFingerprint || null;
            if (intent && !isSha256Fingerprint(fingerprint)) {
                setProjectError('Story Sprint-selection state is unavailable. Reload the current project projection.');
                return;
            }
            const controls = Array.from(document.querySelectorAll('[data-story-selection-intent], [data-story-structural-reconcile-id]'));
            const controlStates = captureStoryControlStates(controls);
            const label = button.querySelector('[data-story-selection-label="true"], [data-story-reconcile-label="true"]');
            const idleLabel = label?.textContent ?? '';
            const payload = intent
                ? sprintSelectionMutationPayload(storyId, intent, fingerprint)
                : structuralEligibilityMutationPayload(storyId);
            const token = payload.idempotency_key;
            controls.forEach((control) => {
                control.disabled = true;
                control.setAttribute('aria-busy', 'true');
            });
            activeStoryMutation = {
                token,
                phase: 'submitting',
                payload,
                storyId,
                intent,
                fingerprint,
            };
            let refreshed = false;
            let mutationCompleted = false;
            setProjectError('');
            try {
                if (intent) {
                    if (label) label.textContent = 'Saving selection...';
                    await postStorySelectionMutation(
                        selectedProjectId,
                        storyId,
                        intent,
                        fingerprint,
                        payload,
                    );
                    mutationCompleted = true;
                } else {
                    if (label) label.textContent = 'Running structural checks...';
                    const response = await requestJson(`/api/projects/${selectedProjectId}/story/structural-eligibility/reconcile`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload),
                    });
                    if (response?.ok === false) {
                        const failure = response.errors?.[0];
                        throw new Error(failure?.message || 'Structural eligibility reconciliation was rejected.');
                    }
                    mutationCompleted = true;
                }
                if (
                    activeStoryMutation?.token === token
                    && activeStoryMutation.phase === 'submitting'
                ) {
                    activeStoryMutation = {
                        ...activeStoryMutation,
                        phase: 'awaiting_authority',
                    };
                    renderDashboard();
                }
                refreshed = await loadDashboard() === true;
                if (!refreshed) {
                    throw new Error('The update was accepted, but the current project projection could not be reloaded. Controls remain locked until a successful refresh.');
                }
                focusStoryReadiness(storyId);
            } catch (error) {
                setProjectError(error.message);
            } finally {
                if (!mutationCompleted && activeStoryMutation?.token === token) {
                    activeStoryMutation = null;
                    restoreStoryControlStates(controlStates);
                    if (label) label.textContent = idleLabel;
                }
            }
            return;
        }
        if (button.dataset.applyDependencies) {
            if (activeDependencyMutation) return;
            const label = button.querySelector('[data-delivery-action-label="true"]');
            const idleLabel = label?.textContent ?? 'Confirm dependencies';
            const status = button.closest('[data-dependency-review-section]')?.querySelector('[data-delivery-action-status="true"]');
            let mutationCompleted = false;
            let refreshed = false;
            setProjectError('');
            const {
                scopeIds,
                scopeEdges,
                scopeFingerprint,
                isWellFormed,
            } = selectedScopeDependencies(
                lifecycleState.storyDependencies?.stories,
                lifecycleState.storyDependencies,
            );
            if (!isWellFormed || scopeIds.length === 0) {
                setProjectError('Current selected Story scope is unavailable.');
                return;
            }
            const payload = semanticMutationPayload({
                selected_story_ids: scopeIds,
                selected_scope_fingerprint: scopeFingerprint,
                reviewed_edges: scopeEdges.map(({
                    dependent_story_id,
                    prerequisite_story_id,
                    reason,
                }) => ({
                    dependent_story_id,
                    prerequisite_story_id,
                    reason,
                })),
            });
            const token = payload.idempotency_key;
            const controlState = captureStoryControlStates([button]);
            button.disabled = true;
            button.setAttribute('aria-disabled', 'true');
            button.setAttribute('aria-busy', 'true');
            if (label) label.textContent = 'Submitting...';
            if (status) {
                status.textContent = 'Dependency review is being submitted; controls remain locked.';
                status.hidden = false;
            }
            activeDependencyMutation = {
                token,
                phase: 'submitting',
                payload,
                button,
            };
            try {
                await postStoryDependencyMutation(
                    selectedProjectId,
                    scopeIds,
                    scopeEdges,
                    scopeFingerprint,
                    payload,
                );
                mutationCompleted = true;
                if (
                    activeDependencyMutation?.token === token
                    && activeDependencyMutation.phase === 'submitting'
                ) {
                    activeDependencyMutation = {
                        ...activeDependencyMutation,
                        phase: 'awaiting_authority',
                    };
                    renderDashboard();
                }
                refreshed = await loadDashboard() === true;
                if (!refreshed) {
                    throw new Error('The dependency review was accepted, but the current project projection could not be reloaded. Controls remain locked until a successful refresh.');
                }
            } catch (error) {
                setProjectError(error.message);
                if (status) {
                    status.textContent = error.message;
                    status.hidden = false;
                }
            } finally {
                if (shouldUnlockDependencyMutation(mutationCompleted, refreshed)) {
                    restoreStoryControlStates(controlState);
                    if (label) label.textContent = idleLabel;
                    if (activeDependencyMutation?.token === token) {
                        activeDependencyMutation = null;
                        renderDashboard();
                    }
                }
            }
            return;
        }
        if (button.dataset.visionRevision) {
            openHumanDialog({
                kind: 'vision-revision',
                title: 'Revise Project Vision',
                description: 'Record why the accepted Vision needs a new revision.',
                label: 'Revision reason',
                submitLabel: 'Begin revision',
                required: true,
            });
        }
    });

    document.getElementById('human-action-cancel')?.addEventListener('click', closeHumanDialog);
    document.getElementById('human-action-close')?.addEventListener('click', closeHumanDialog);
    document.getElementById('human-action-dialog')?.addEventListener('close', () => {
        pendingHumanAction = null;
        setDialogError('');
    });
    document.getElementById('refresh-project')?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
            await loadDashboard();
        } catch (error) {
            setProjectError(error.message);
        } finally {
            button.disabled = false;
        }
    });
}

window.addEventListener('DOMContentLoaded', async () => {
    const idValue = new URLSearchParams(window.location.search).get('id');
    selectedProjectId = Number.parseInt(idValue || '', 10);
    if (!Number.isInteger(selectedProjectId)) {
        window.location.href = '/dashboard';
        return;
    }
    installInteractions();
    try {
        await loadDashboard();
    } catch (error) {
        setProjectError(error.message);
    }
});

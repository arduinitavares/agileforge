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
    'begin_vision_revision',
    'decide_product_goal_review',
    'decide_specification',
    'decide_vision_review',
    'fulfill_product_goal',
    'generate_vision_bootstrap',
    'record_product_goal_interview_turn',
    'record_vision_interview_turn',
    'register_specification_source',
    'structure_specification',
]);

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

function isActionableDecision(decision, actions) {
    return DASHBOARD_CONTROL_REQUEST_KINDS.has(decision?.request_kind)
        && findAction(actions, decision?.request_kind) !== null;
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

function stageStatus(decision, actions) {
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

function stageReason(decision, actions) {
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
        if (!current || decisionRank(decision.category) > decisionRank(current.category)) {
            byStage.set(stage, decision);
        }
    });
    cardActions.forEach((action) => {
        const stage = REQUEST_STAGE[action.request_kind];
        if (stage && !byStage.has(stage)) {
            byStage.set(stage, { category: 'available', request_kind: action.request_kind });
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
            status: stageStatus(decision, cardActions),
            reason: decision ? stageReason(decision, cardActions) : null,
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

    return cards;
}

function workflowPositionMarkup(position, actions = [], projections = {}) {
    const cards = lifecycleCardProjection(position, actions, projections);

    return `<ol class="grid min-w-0 grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">${cards.map((card) => {
        return `<li class="min-w-0 rounded-lg border px-3 py-2 ${card.tone}">
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
    registration,
    position,
) {
    if (projection?.review?.state !== 'feedback') return registration;
    const binding = captureSpecificationStructuringBinding({
        actions,
        position,
        specification: projection,
    });
    if (!binding) return registration;
    const rationale = projection.review.rationale
        ? `<p class="mt-2 text-sm leading-6 text-amber-900"><strong>Feedback:</strong> ${escapeWorkflowText(projection.review.rationale)}</p>`
        : '';
    const revisedSource = registration
        ? `<div class="mt-5 border-t border-amber-200 pt-5">
            <p class="mb-3 text-sm font-semibold text-slate-800">Register a revised source</p>
            <p class="mb-4 text-sm leading-6 text-slate-600">Choose this path only when the external Specification source itself changed.</p>
            ${registration}
        </div>`
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
        ${revisedSource}
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
    const registration = specificationSourceRegistrationMarkup(actions);
    if (!candidate) {
        const structureBinding = captureSpecificationStructuringBinding({
            actions,
            position,
            specification: projection,
        });
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
            return [
                current,
                `<section data-specification-feedback-continuation="true" class="max-w-4xl space-y-5 rounded-lg border border-amber-300 bg-amber-50 p-5">
                    ${structure}
                    ${registration ? `<div class="border-t border-amber-200 pt-5">${registration}</div>` : ''}
                </section>`,
            ].filter(Boolean).join('');
        }
        return [current, registration, structure].filter(Boolean).join('')
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
        : specificationFeedbackContinuationMarkup(
            projection,
            actions,
            registration,
            position,
        );
    return `<div class="max-w-4xl">${decisionCopy}<pre class="whitespace-pre-wrap break-words rounded-lg border border-slate-300 bg-white p-4 font-mono text-sm leading-6">${escapeWorkflowText(candidate.rendered_markdown ?? '')}</pre></div>
        ${controls}${reentry}`;
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

function storyItemMarkup(value) {
    const story = reviewObject(value);
    if (!story) return '';
    return `<section class="space-y-3 rounded-md border border-slate-200 p-3">
        <div><p class="text-xs font-semibold uppercase text-slate-500">Story</p><p class="mt-1 font-semibold">${escapeWorkflowText(reviewValue(story.story_title ?? story.title))}</p></div>
        <p class="text-sm leading-6">${escapeWorkflowText(reviewValue(story.statement))}</p>
        <p class="text-sm"><strong>Persona:</strong> ${escapeWorkflowText(reviewValue(story.persona))}</p>
        ${reviewListMarkup('Acceptance criteria', story.acceptance_criteria)}
        ${specificationEvidenceMarkup(story.specification_evidence)}
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

function sprintReviewMarkup(candidate) {
    const stories = reviewItems(candidate.selected_stories);
    if (!stories) return '';
    return `<div class="space-y-4">
        <p class="text-sm"><strong>Team:</strong> ${escapeWorkflowText(reviewValue(candidate.team_name))}</p>
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
    if (review.phase === 'sprint_plan') return sprintReviewMarkup(candidate);
    return '';
}

function planningReviewCardMarkup(label, selected, scope, index = 0) {
    if (!selected?.review || !selected?.binding) return '';
    const content = planningReviewContentMarkup(selected.review);
    if (!content) return '';
    return `<article class="rounded-lg border border-slate-300 bg-white p-4" data-planning-review-card="${escapeWorkflowText(scope)}">
        <h3 class="text-sm font-semibold">${escapeWorkflowText(label)}</h3>
        <div class="mt-3 space-y-4">${content}</div>
        <div class="mt-4 flex flex-wrap gap-2">
            <button type="button" data-planning-review="${escapeWorkflowText(scope)}" data-review-index="${index}" data-review-decision="accepted" class="${BUTTON_PRIMARY}">Accept</button>
            <button type="button" data-planning-review="${escapeWorkflowText(scope)}" data-review-index="${index}" data-review-decision="feedback" class="${BUTTON_SECONDARY}">Request changes</button>
            <button type="button" data-planning-review="${escapeWorkflowText(scope)}" data-review-index="${index}" data-review-decision="rejected" class="${BUTTON_DANGER}">Reject</button>
        </div>
    </article>`;
}

function deliveryPanelMarkup(position, reviews = {}) {
    const storyItems = Array.isArray(reviews.stories?.items) ? reviews.stories.items : [];
    const cards = [
        planningReviewCardMarkup('Backlog review', reviews.backlog, 'backlog'),
        planningReviewCardMarkup('Roadmap review', reviews.roadmap, 'roadmap'),
        ...storyItems.map((item, index) => planningReviewCardMarkup(`Story review ${index + 1}`, item, 'story', index)),
        planningReviewCardMarkup('Sprint plan review', reviews.sprintPlan, 'sprint', 0),
    ].filter(Boolean);
    if (cards.length) return `<div class="grid gap-4">${cards.join('')}</div>`;
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
        deliveryPanelMarkup(lifecycleState.position, lifecycleState.planningReviews),
    );
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

async function loadDashboard() {
    const sequence = ++dashboardLoadSequence;
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
        ]);
        if (sequence !== dashboardLoadSequence || controller.signal.aborted) return false;
        lifecycleState = {
            project: project.data ?? {},
            position: position.data ?? {},
            actions: position.actions ?? [],
            vision: vision.data ?? {},
            goal: goal.data ?? {},
            specification: specification.data ?? {},
            repository: repository.data ?? {},
            planningReviews: {
                backlog: backlogReview.data ?? {},
                roadmap: roadmapReview.data ?? {},
                stories: storyReviews.data ?? { items: [] },
                sprintPlan: sprintPlanReview.data ?? {},
            },
        };
        setProjectError('');
        renderDashboard();
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
        title: `${decision === 'accepted' ? 'Accept' : decision === 'feedback' ? 'Request changes for' : 'Reject'} this ${scope === 'sprint' ? 'Sprint plan' : scope}`,
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
    if (rationaleGroup && rationale && rationaleLabel) {
        rationaleGroup.classList.toggle('hidden', config.field === 'path');
        rationale.required = config.field !== 'path' && config.required !== false;
        rationale.value = '';
        rationaleLabel.textContent = config.label ?? 'Rationale';
    }
    if (pathGroup && path) {
        pathGroup.classList.toggle('hidden', config.field !== 'path');
        path.required = config.field === 'path';
        path.value = config.initialPath ?? '';
    }
    setText('human-action-submit', config.submitLabel ?? 'Confirm');
    setDialogError('');
    document.getElementById('human-action-dialog')?.showModal();
    window.setTimeout(() => (config.field === 'path' ? path : rationale)?.focus(), 0);
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

async function runDirectAction(requestKind, button, fallbackEndpoint = null) {
    if (button.disabled) return false;
    const isSpecificationStructuring = requestKind === 'structure_specification';
    const setBusy = isSpecificationStructuring
        ? setSpecificationStructuringBusy
        : setSpecificationContinuationBusy;
    let specificationBinding = null;
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
            await postAction(
                binding.action,
                {},
                { expectedDecision: binding.expectedDecision },
            );
            mutationCompleted = true;
        } else {
            const action = findAction(lifecycleState.actions, requestKind)
                ?? (fallbackEndpoint ? { endpoint: fallbackEndpoint } : null);
            await postAction(action);
        }
        await loadDashboard();
    } catch (error) {
        if (!isSpecificationStructuring) {
            setProjectError(error.message);
            return true;
        }
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
    } finally {
        setBusy(button, false);
    }
    return true;
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
            if (form.dataset.submitting === 'true') return;
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
            setSpecificationContinuationBusy(form, true);
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
                delete form.dataset.submitting;
                setSpecificationContinuationBusy(form, false);
                if (submit) submit.disabled = false;
            }
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

    document.addEventListener('click', (event) => {
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
        if (button.dataset.directAction) {
            runDirectAction(button.dataset.directAction, button);
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

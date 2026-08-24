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
    'structure_specification',
    'validate_story',
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
        description: 'Generate the Sprint plan from the approved Stories.',
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

function isActionableDecision(decision, actions) {
    return DASHBOARD_CONTROL_REQUEST_KINDS.has(decision?.request_kind)
        && findDecisionAction(actions, decision) !== null;
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
        description = 'Generate replacement User Story drafts for this accepted requirement.';
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
    };
}

function deliveryGenerationActionMarkup(action, position = {}, reviews = {}, index = 0, context = {}) {
    const details = deliveryGenerationActionDetails(action, position, reviews, context);
    if (!details) return '';
    const bindingAttributes = deliveryActionBindingAttributes(action);
    const content = `<p class="mb-3 text-sm leading-6 text-slate-600">${escapeWorkflowText(details.description)}</p>`;
    if (action.request_kind === 'record_sprint_plan') {
        return `<form data-delivery-generation-action="${escapeWorkflowText(action.request_kind)}"
            data-delivery-generation-form="${escapeWorkflowText(action.request_kind)}" ${bindingAttributes}
            class="space-y-4 rounded-lg border border-slate-200 p-4">
            ${content}
            <div class="max-w-xl">
                <label for="delivery-team-name-${index}" class="text-sm font-semibold">Team name</label>
                <input id="delivery-team-name-${index}" name="team_name" type="text" required autocomplete="organization"
                    class="mt-1.5 w-full rounded-lg border-slate-300 text-sm focus:border-accent focus:ring-accent" />
                <p class="mt-1 text-xs leading-5 text-slate-500">Choose the team that will own this Sprint plan.</p>
            </div>
            <button type="submit" class="${BUTTON_PRIMARY}">
                <span class="material-symbols-outlined" aria-hidden="true">${details.icon}</span>
                <span data-delivery-action-label="true">${escapeWorkflowText(details.label)}</span>
            </button>
            <p data-delivery-action-status="true" hidden role="status" aria-live="polite" aria-atomic="true"
                class="text-sm leading-6 text-slate-700"></p>
        </form>`;
    }
    const isMissingRequirement = action.request_kind === 'record_story_draft' && !details.requirement;
    const disabledAttr = isMissingRequirement ? ' disabled title="Requirement summary unavailable"' : '';
    return `<div data-delivery-generation-action="${escapeWorkflowText(action.request_kind)}" ${bindingAttributes} class="mt-4">
        ${content}
        <button type="button" data-direct-action="${escapeWorkflowText(action.request_kind)}"${disabledAttr} class="${BUTTON_PRIMARY}">
            <span class="material-symbols-outlined" aria-hidden="true">${details.icon}</span>
            <span data-delivery-action-label="true">${escapeWorkflowText(details.label)}</span>
        </button>
        <p data-delivery-action-status="true" hidden role="status" aria-live="polite" aria-atomic="true"
            class="mt-3 text-sm leading-6 text-slate-700"></p>
    </div>`;
}

function storyReadinessMarkup(stories, context = {}) {
    if (!Array.isArray(stories) || stories.length === 0) return '';
    const pendingItems = Array.isArray(context?.storyPending?.items)
        ? context.storyPending.items
        : (Array.isArray(lifecycleState?.storyPending?.items)
            ? lifecycleState.storyPending.items
            : []);
    const storyRows = stories.map((story) => {
        const pbiId = story.backlog_item_id || '';
        const pending = pbiId ? pendingItems.find((item) => item?.backlog_item_id === pbiId) : null;
        const requirement = pending?.requirement || '';
        const storyIdText = story.source_story_item_id || `Story #${story.story_id}`;
        const blockers = Array.isArray(story.readiness_blockers) ? story.readiness_blockers : [];
        const failures = Array.isArray(story.validation_failures) ? story.validation_failures : [];
        const isValidationFailed = story.validation_status === 'failed' || failures.length > 0;
        const isUnvalidated = !isValidationFailed && (story.validation_status === 'unvalidated' || blockers.includes('STORY_VALIDATION_REQUIRED') || !story.content_accepted);

        let validationBadge = '';
        let actionButtonMarkup = '';
        let diagnosticsMarkup = '';

        if (isValidationFailed) {
            validationBadge = '<span class="inline-flex items-center rounded-full bg-red-50 px-2 py-0.5 text-xs font-semibold text-red-800 border border-red-200">Validation Failed</span>';
            actionButtonMarkup = `<button type="button" data-story-validate-id="${story.story_id}" class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:cursor-wait disabled:opacity-60">
                <span class="material-symbols-outlined text-sm" aria-hidden="true">refresh</span>
                <span data-story-validate-label="true">Revalidate</span>
            </button>`;
            if (failures.length > 0) {
                const failureItems = failures.map((f) => {
                    const code = escapeWorkflowText(f?.code || f?.rule_name || 'Structural Failure');
                    const msg = escapeWorkflowText(f?.message || (typeof f === 'string' ? f : JSON.stringify(f)));
                    return `<div>• <strong>${code}</strong>: ${msg}</div>`;
                }).join('');
                diagnosticsMarkup = `<div class="mt-1 rounded bg-red-50 p-2 text-xs text-red-700 border border-red-200 space-y-0.5" data-story-validation-diagnostics="true">${failureItems}</div>`;
            }
        } else if (isUnvalidated) {
            validationBadge = '<span class="inline-flex items-center rounded-full bg-amber-50 px-2 py-0.5 text-xs font-semibold text-amber-800 border border-amber-200">Unvalidated</span>';
            actionButtonMarkup = `<button type="button" data-story-validate-id="${story.story_id}" class="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-800 disabled:cursor-wait disabled:opacity-60">
                <span class="material-symbols-outlined text-sm" aria-hidden="true">task_alt</span>
                <span data-story-validate-label="true">Validate Story</span>
            </button>`;
        } else {
            validationBadge = '<span class="inline-flex items-center rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-semibold text-emerald-800 border border-emerald-200">Validated</span>';
        }

        const dependencyBlockers = blockers.filter((b) => b !== 'STORY_VALIDATION_REQUIRED');
        let blockerBadge = '';
        if (dependencyBlockers.length > 0) {
            blockerBadge = `<span class="inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-900 border border-amber-300">Blocked: ${escapeWorkflowText(dependencyBlockers.join(', '))}</span>`;
        }

        const badgesMarkup = [validationBadge, blockerBadge].filter(Boolean).join(' ');

        return `<div class="py-3 first:pt-0 last:pb-0 flex flex-col justify-between gap-2" data-story-readiness-row="${story.story_id}">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                <div class="min-w-0 space-y-1">
                    <div class="flex items-center gap-2 flex-wrap">
                        <span class="font-semibold text-sm text-slate-900">${escapeWorkflowText(storyIdText)}</span>
                        ${pbiId ? `<span class="text-xs text-slate-500 font-mono">(${escapeWorkflowText(pbiId)})</span>` : ''}
                        ${badgesMarkup}
                    </div>
                    ${requirement ? `<p class="text-xs text-slate-600 line-clamp-1">${escapeWorkflowText(requirement)}</p>` : ''}
                    <div class="flex items-center gap-3 text-xs text-slate-500">
                        <span>Rank: ${escapeWorkflowText(story.rank || '-')}</span>
                        <span>Points: ${escapeWorkflowText(story.story_points ?? '-')}</span>
                    </div>
                </div>
                ${actionButtonMarkup ? `<div class="shrink-0 flex items-center">${actionButtonMarkup}</div>` : ''}
            </div>
            ${diagnosticsMarkup}
        </div>`;
    });

    return `<div class="rounded-lg border border-slate-200 bg-white p-4 space-y-3" data-story-readiness-section="true">
        <div class="flex items-center justify-between border-b border-slate-100 pb-2">
            <h3 class="text-sm font-bold text-ink">Story readiness</h3>
            <span class="text-xs text-slate-500">${stories.length} accepted ${stories.length === 1 ? 'story' : 'stories'}</span>
        </div>
        <div class="divide-y divide-slate-100">
            ${storyRows.join('')}
        </div>
    </div>`;
}

function validateCandidateProjection(candidates) {
    if (!Array.isArray(candidates) || candidates.length === 0) {
        return {
            isValid: false,
            candidateStories: [],
            candidateIds: [],
        };
    }
    const seenIds = new Set();
    const validStories = [];
    const validIds = [];

    for (const s of candidates) {
        if (!s || typeof s !== 'object') {
            return {
                isValid: false,
                candidateStories: [],
                candidateIds: [],
            };
        }
        if (
            typeof s.story_id !== 'number' ||
            !Number.isInteger(s.story_id) ||
            s.story_id <= 0 ||
            s.sprint_candidate !== true
        ) {
            return {
                isValid: false,
                candidateStories: [],
                candidateIds: [],
            };
        }
        if (seenIds.has(s.story_id)) {
            return {
                isValid: false,
                candidateStories: [],
                candidateIds: [],
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
    };
}

function canonicalCandidateDependencies(candidates, dependencies) {
    const projection = validateCandidateProjection(candidates);
    if (!projection.isValid) {
        return {
            candidateStories: [],
            candidateIds: [],
            candidateEdges: [],
            isWellFormed: false,
        };
    }

    const { candidateStories, candidateIds } = projection;
    const candidateSet = new Set(candidateIds);
    const rawEdges = Array.isArray(dependencies?.edges) ? dependencies.edges : [];
    const candidateEdges = rawEdges
        .filter((e) => e && typeof e === 'object' && candidateSet.has(e.dependent_story_id) && candidateSet.has(e.prerequisite_story_id))
        .map((e) => ({
            dependent_story_id: e.dependent_story_id,
            prerequisite_story_id: e.prerequisite_story_id,
            reason: e.reason || 'Operator reviewed dependency',
        }));
    return {
        candidateStories,
        candidateIds,
        candidateEdges,
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
        for (const s of dependencies.stories) {
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

function storyDependencyReviewMarkup(action, candidates, dependencies) {
    if (!action || action.request_kind !== 'apply_story_dependencies') return '';
    const { candidateStories, candidateEdges, isWellFormed } = canonicalCandidateDependencies(
        candidates,
        dependencies,
    );
    const storyMap = buildStoryLookupMap(candidateStories, dependencies);
    const storySummary = isWellFormed
        ? (candidateStories.map((s) => storyDisplayLabel(s)).join(', ') || 'None')
        : 'Unavailable (canonical candidate projection missing)';
    const edgeSummary = isWellFormed
        ? (candidateEdges.length > 0
            ? candidateEdges.map((e) => {
                const dep = storyMap.get(e.dependent_story_id);
                const prereq = storyMap.get(e.prerequisite_story_id);
                const depLabel = storyDisplayLabel(dep) || `Story #${e.dependent_story_id}`;
                const prereqLabel = storyDisplayLabel(prereq) || `Story #${e.prerequisite_story_id}`;
                const reason = e.reason ? ` - ${e.reason}` : '';
                return `${depLabel} -> ${prereqLabel}${reason}`;
            }).join('; ')
            : 'None (independent stories)')
        : 'Unavailable (canonical candidate projection missing)';
    const bindingAttributes = deliveryActionBindingAttributes(action);
    const disabledAttr = isWellFormed ? '' : 'disabled aria-disabled="true"';

    return `<div class="rounded-lg border border-amber-200 bg-amber-50/50 p-4 space-y-3" data-dependency-review-section="true" ${bindingAttributes}>
        <div class="flex items-center gap-2">
            <span class="material-symbols-outlined text-amber-700" aria-hidden="true">account_tree</span>
            <h3 class="text-sm font-bold text-amber-900">Dependency review required</h3>
        </div>
        <p class="text-xs leading-5 text-amber-800">Review and confirm execution dependencies among validated candidate stories before Sprint planning.</p>
        <div class="text-xs text-slate-700 bg-white rounded border border-slate-200 p-2.5 space-y-1">
            <p><strong>Candidate stories:</strong> ${escapeWorkflowText(storySummary)}</p>
            <p><strong>Dependency edges:</strong> ${escapeWorkflowText(edgeSummary)}</p>
        </div>
        <button type="button" data-apply-dependencies="true" ${disabledAttr} class="${BUTTON_PRIMARY}">
            <span class="material-symbols-outlined" aria-hidden="true">verified</span>
            <span data-delivery-action-label="true">Confirm dependencies</span>
        </button>
        <p data-delivery-action-status="true" hidden role="status" aria-live="polite" aria-atomic="true"
            class="text-sm leading-6 text-slate-700"></p>
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

function deliveryPanelMarkup(position, reviews = {}, actions = [], context = {}) {
    const storyItems = Array.isArray(reviews.stories?.items) ? reviews.stories.items : [];
    const cards = [
        planningReviewCardMarkup('Backlog review', reviews.backlog, 'backlog'),
        planningReviewCardMarkup('Roadmap review', reviews.roadmap, 'roadmap'),
        ...storyItems.map((item, index) => {
            const pbiId = item?.binding?.instance_key?.startsWith('backlog_item:')
                ? item.binding.instance_key.slice('backlog_item:'.length)
                : (item?.binding?.instance_key || null);
            const cardTitle = pbiId ? `Story review for ${pbiId}` : `Story review ${index + 1}`;
            return planningReviewCardMarkup(cardTitle, item, 'story', index);
        }),
        planningReviewCardMarkup('Sprint plan review', reviews.sprintPlan, 'sprint', 0),
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
    const dependencySection = storyDependencyReviewMarkup(dependencyAction, candidates, dependencies);
    const candidateSection = sprintCandidatePoolMarkup(candidates);

    const availableDeliveryActions = (Array.isArray(actions) ? actions : []).filter((action) =>
        Boolean(DELIVERY_ACTION_CONFIG[action?.request_kind]),
    );
    const actionMarkup = availableDeliveryActions.map((action, index) =>
        deliveryGenerationActionMarkup(action, position, reviews, index, context),
    );

    const sections = [
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
            storyPending,
            storyDependencies,
            sprintCandidates,
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
            storyPending: storyPending.data ?? {},
            storyDependencies: storyDependencies?.data ?? {},
            sprintCandidates: sprintCandidates?.data ?? {},
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

function currentDeliveryActionContainers(action, requestKind) {
    const candidates = Array.from(document.querySelectorAll?.(
        `[data-delivery-generation-action="${requestKind}"]`,
    ) ?? []);
    const exact = candidates.find(
        (candidate) => deliveryActionElementMatches(candidate, action, requestKind),
    );
    return exact ? [exact] : [];
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

async function runDirectAction(requestKind, button, fallbackEndpoint = null, fields = {}) {
    if (button.disabled) return false;
    const isSpecificationStructuring = requestKind === 'structure_specification';
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
            if (binding.instance_key !== null && binding.instance_key !== undefined) {
                deliveryFields.instance_key = binding.instance_key;
            }
            await postAction(binding, deliveryFields);
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
                : error.message;
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
        if (isDeliveryGeneration && !deliveryReconciled) {
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
        const deliveryRequestKind = form?.dataset?.deliveryGenerationForm;
        if (DELIVERY_ACTION_CONFIG[deliveryRequestKind]) {
            event.preventDefault();
            if (form.dataset.submitting === 'true') return;
            const submit = form.querySelector('button[type="submit"]');
            if (!submit) return;
            const fields = {};
            if (deliveryRequestKind === 'record_sprint_plan') {
                const teamName = form.querySelector('[name="team_name"]');
                const value = teamName?.value.trim() ?? '';
                if (!value) {
                    teamName?.reportValidity?.();
                    return;
                }
                fields.team_name = value;
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
        if (button.dataset.directAction) {
            runDirectAction(button.dataset.directAction, button);
            return;
        }
        if (button.dataset.storyValidateId) {
            const storyId = Number.parseInt(button.dataset.storyValidateId, 10);
            if (!Number.isInteger(storyId)) return;
            const label = button.querySelector('[data-story-validate-label="true"]');
            const idleLabel = label?.textContent ?? 'Validate Story';
            button.disabled = true;
            button.setAttribute('aria-busy', 'true');
            if (label) label.textContent = 'Validating...';
            setProjectError('');
            try {
                const response = await requestJson(`/api/projects/${selectedProjectId}/story/validate`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(semanticMutationPayload({
                        story_id: storyId,
                        mode: 'structural',
                    })),
                });
                await loadDashboard();
                if (response?.data?.success && !response.data.ready_for_sprint) {
                    const failures = response.data.structural_failures || [];
                    const diag = failures.map((f) => `${f.rule_name || f.code || 'Failure'}: ${f.message}`).join(' ');
                    setProjectError(`Story structural validation failed. ${diag}`);
                }
            } catch (error) {
                setProjectError(error.message);
            } finally {
                button.disabled = false;
                button.removeAttribute('aria-busy');
                if (label) label.textContent = idleLabel;
            }
            return;
        }
        if (button.dataset.applyDependencies) {
            const label = button.querySelector('[data-delivery-action-label="true"]');
            const idleLabel = label?.textContent ?? 'Confirm dependencies';
            const status = button.closest('[data-dependency-review-section]')?.querySelector('[data-delivery-action-status="true"]');
            button.disabled = true;
            button.setAttribute('aria-busy', 'true');
            if (label) label.textContent = 'Confirming...';
            if (status) {
                status.textContent = 'Applying dependency review...';
                status.hidden = false;
            }
            setProjectError('');
            try {
                if (!Array.isArray(lifecycleState.sprintCandidates?.items)) {
                    throw new Error('Canonical candidate projection is unavailable.');
                }
                const candidates = lifecycleState.sprintCandidates.items;
                const { candidateIds, candidateEdges, isWellFormed } = canonicalCandidateDependencies(
                    candidates,
                    lifecycleState.storyDependencies,
                );
                if (!isWellFormed || candidateIds.length === 0) {
                    throw new Error('Canonical candidate projection is unavailable.');
                }
                await requestJson(`/api/projects/${selectedProjectId}/story/dependencies/apply`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(semanticMutationPayload({
                        selected_story_ids: candidateIds,
                        reviewed_edges: candidateEdges,
                    })),
                });
                await loadDashboard();
            } catch (error) {
                setProjectError(error.message);
                if (status) {
                    status.textContent = error.message;
                    status.hidden = false;
                }
            } finally {
                button.disabled = false;
                button.removeAttribute('aria-busy');
                if (label) label.textContent = idleLabel;
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

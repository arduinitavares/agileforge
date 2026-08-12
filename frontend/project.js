const STAGES = [
    'Vision',
    'Product Goal',
    'Specification',
    'Authority',
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
    compile_authority: 'Authority',
    complete_task: 'Execution',
    decide_authority: 'Authority',
    decide_backlog: 'Backlog',
    decide_product_goal_review: 'Product Goal',
    decide_roadmap: 'Roadmap',
    decide_specification: 'Specification',
    decide_sprint_plan: 'Sprint',
    decide_story: 'Stories',
    fulfill_product_goal: 'Product Goal',
    generate_vision_bootstrap: 'Vision',
    record_authority_feedback: 'Authority',
    record_backlog_draft: 'Backlog',
    record_post_sprint_triage: 'Review',
    record_product_goal_interview_turn: 'Product Goal',
    record_roadmap_draft: 'Roadmap',
    author_specification: 'Specification',
    record_sprint_plan: 'Sprint',
    record_story_draft: 'Stories',
    record_vision_interview_turn: 'Vision',
    repair_authority: 'Authority',
    repair_story_readiness: 'Stories',
    review_sprint: 'Review',
    start_sprint: 'Sprint',
};

const CHILD_STAGE = {
    authority: 'Authority',
    backlog: 'Backlog',
    execution: 'Execution',
    product_goal: 'Product Goal',
    vision: 'Vision',
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
    authority: {},
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

function stageStatus(category) {
    return {
        available: 'Ready',
        waiting: 'In progress',
        blocked: 'Waiting',
        invalid: 'Needs attention',
    }[category] ?? 'Upcoming';
}

function stageTone(category) {
    return {
        available: 'border-emerald-300 bg-emerald-50 text-emerald-900',
        waiting: 'border-sky-300 bg-sky-50 text-sky-900',
        blocked: 'border-slate-300 bg-white text-slate-700',
        invalid: 'border-red-300 bg-red-50 text-red-800',
    }[category] ?? 'border-slate-300 bg-white text-slate-600';
}

function stageReason(decision) {
    const blockers = Array.isArray(decision?.blockers) ? decision.blockers : [];
    const blocker = blockers.find((item) => typeof item?.message === 'string');
    if (blocker) return blocker.message;
    return {
        available: 'Ready for your input.',
        waiting: 'A human decision is pending.',
        blocked: 'Finish the previous stage first.',
        invalid: 'Resolve the current lifecycle conflict.',
    }[decision?.category] ?? 'This stage follows the current work.';
}

function workflowPositionMarkup(position, actions = []) {
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
    (Array.isArray(actions) ? actions : []).forEach((action) => {
        const stage = REQUEST_STAGE[action.request_kind];
        if (stage && !byStage.has(stage)) {
            byStage.set(stage, { category: 'available', request_kind: action.request_kind });
        }
    });

    const activeIndexes = STAGES
        .map((stage, index) => (byStage.has(stage) ? index : null))
        .filter((index) => index !== null);
    const firstActive = activeIndexes.length > 0 ? Math.min(...activeIndexes) : -1;

    return `<ol class="grid min-w-0 grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">${STAGES.map((stage, index) => {
        const decision = byStage.get(stage);
        if (!decision && firstActive >= 0 && index < firstActive) {
            return `<li class="min-w-0 rounded-lg border border-emerald-200 bg-white px-3 py-2">
                <p class="break-words text-xs font-semibold">${escapeWorkflowText(stage)}</p>
                <p class="mt-1 text-xs text-emerald-700">Complete</p>
            </li>`;
        }
        const category = decision?.category;
        return `<li class="min-w-0 rounded-lg border px-3 py-2 ${stageTone(category)}">
            <p class="break-words text-xs font-semibold">${escapeWorkflowText(stage)}</p>
            <p class="mt-1 text-xs font-medium">${stageStatus(category)}</p>
            ${decision ? `<p class="mt-1 break-words text-xs leading-4 opacity-80">${escapeWorkflowText(stageReason(decision))}</p>` : ''}
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

function specificationPanelMarkup(projection, actions = []) {
    const candidate = projection?.candidate;
    if (!candidate) {
        const authorAction = findAction(actions, 'author_specification');
        if (authorAction) {
            return `<p class="mb-4 text-sm leading-6 text-slate-600">Author a Specification from the accepted Vision, Product Goal, and host-prepared evidence.</p>
                <button type="button" data-direct-action="author_specification" class="${BUTTON_PRIMARY}"><span class="material-symbols-outlined" aria-hidden="true">description</span><span>Author Specification</span></button>`;
        }
        return '<p class="text-sm text-slate-600">Specification authoring is waiting for the current lifecycle state.</p>';
    }
    const review = projection?.review;
    const reviewAction = findAction(actions, 'decide_specification');
    const decisionCopy = review?.state && review.state !== 'pending'
        ? `<p class="mb-4 text-sm font-semibold text-slate-700">Review: ${escapeWorkflowText(humanizeKey(review.state))}</p>`
        : '<p class="mb-4 text-sm font-semibold text-slate-700">Exact Specification candidate</p>';
    return `<div class="max-w-4xl">${decisionCopy}<pre class="whitespace-pre-wrap break-words rounded-lg border border-slate-300 bg-white p-4 font-mono text-sm leading-6">${escapeWorkflowText(candidate.rendered_markdown ?? '')}</pre></div>
        ${review?.state === 'pending' ? reviewControlsMarkup('specification', reviewAction) : ''}`;
}

function findingMarkup(finding) {
    const message = typeof finding === 'string' ? finding : finding?.message;
    if (!message) return '';
    return `<li class="flex min-w-0 gap-2 text-sm leading-6 text-slate-700">
        <span class="material-symbols-outlined mt-0.5 shrink-0 text-amber-700" aria-hidden="true">warning</span>
        <span class="min-w-0 break-words">${escapeWorkflowText(message)}</span>
    </li>`;
}

function authorityPacketMarkup(authority, findings) {
    const artifact = authority?.artifact && typeof authority.artifact === 'object'
        ? authority.artifact
        : { invariants: Array.isArray(authority?.invariants) ? authority.invariants : [] };
    const provenance = {
        authority_id: authority?.authority_id ?? null,
        spec_version_id: authority?.spec_version_id ?? null,
        status: authority?.status ?? null,
        compiler_version: authority?.compiler_version ?? null,
        prompt_hash: authority?.prompt_hash ?? null,
        compiled_at: authority?.compiled_at ?? null,
    };
    const allFindings = [...(Array.isArray(findings) ? findings : [])];
    (Array.isArray(authority?.findings) ? authority.findings : []).forEach((finding) => {
        const message = typeof finding === 'string' ? finding : finding?.message;
        if (!allFindings.some((item) => (typeof item === 'string' ? item : item?.message) === message)) {
            allFindings.push(finding);
        }
    });
    return `<div class="grid min-w-0 gap-6">
        <div class="min-w-0">
            <h3 class="text-sm font-semibold">Compilation provenance</h3>
            <pre class="mt-3 whitespace-pre-wrap break-anywhere rounded-lg border border-slate-300 bg-white p-4 font-mono text-xs leading-5">${escapeWorkflowText(JSON.stringify(provenance, null, 2))}</pre>
        </div>
        <div class="min-w-0">
            <h3 class="text-sm font-semibold">Complete compiled Authority artifact</h3>
            <pre data-authority-artifact="true" class="mt-3 whitespace-pre-wrap break-anywhere rounded-lg border border-slate-300 bg-white p-4 font-mono text-xs leading-5">${escapeWorkflowText(JSON.stringify(artifact, null, 2))}</pre>
        </div>
        <div class="min-w-0">
            <h3 class="text-sm font-semibold">Findings</h3>
            ${allFindings.length > 0
                ? `<ul class="mt-3 space-y-2">${allFindings.map(findingMarkup).join('')}</ul>`
                : '<p class="mt-2 text-sm text-slate-500">No review findings.</p>'}
        </div>
    </div>`;
}

function authorityPanelMarkup(projection, actions = []) {
    const feedbackAction = findAction(actions, 'record_authority_feedback');
    if (feedbackAction) {
        return `<p class="mb-4 text-sm leading-6 text-slate-600">The Authority was rejected. Record the feedback needed for a corrected review.</p>
            <button type="button" data-authority-feedback="true" class="${BUTTON_PRIMARY}"><span class="material-symbols-outlined" aria-hidden="true">rate_review</span><span>Record feedback</span></button>`;
    }

    const repairAction = findAction(actions, 'repair_authority');
    if (repairAction) {
        return `<p class="mb-4 text-sm leading-6 text-slate-600">Feedback is recorded. Recompile the Authority for review.</p>
            <button type="button" data-direct-action="repair_authority" class="${BUTTON_PRIMARY}"><span class="material-symbols-outlined" aria-hidden="true">build</span><span>Recompile</span></button>`;
    }

    const pending = projection?.pending_authority;
    if (pending) {
        const reviewAction = findAction(actions, 'decide_authority');
        return `<p class="mb-4 text-sm font-semibold">Exact Authority review packet</p>
            ${authorityPacketMarkup(pending, projection?.findings)}
            ${reviewControlsMarkup('authority', reviewAction)}`;
    }

    const accepted = projection?.accepted_authority;
    if (accepted) {
        return `<p class="mb-4 text-sm font-semibold text-emerald-700">Authority accepted</p>${authorityPacketMarkup(accepted, accepted.findings)}`;
    }

    const compileAction = findAction(actions, 'compile_authority');
    if (compileAction) {
        return `<p class="mb-4 text-sm leading-6 text-slate-600">Compile the accepted Specification into reviewable rules.</p>
            <button type="button" data-direct-action="compile_authority" class="${BUTTON_PRIMARY}"><span class="material-symbols-outlined" aria-hidden="true">build</span><span>Compile</span></button>`;
    }

    return '<p class="text-sm text-slate-600">Authority follows an accepted Specification.</p>';
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

function deliveryPanelMarkup(position) {
    const decisions = Array.isArray(position?.decisions) ? position.decisions : [];
    const deliveryDecision = decisions.find((decision) => {
        const stage = decisionStage(decision);
        return ['Backlog', 'Roadmap', 'Stories', 'Sprint', 'Execution', 'Review'].includes(stage);
    });
    if (!deliveryDecision) {
        return '<p class="text-sm text-slate-600">Delivery begins after the product definition and Authority are accepted.</p>';
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
        workflowPositionMarkup(lifecycleState.position, lifecycleState.actions),
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
        specificationPanelMarkup(lifecycleState.specification, lifecycleState.actions),
    );
    setMarkup(
        'authority-panel',
        authorityPanelMarkup(lifecycleState.authority, lifecycleState.actions),
    );
    setMarkup(
        'repository-panel',
        repositoryPanelMarkup(lifecycleState.repository, lifecycleState.actions),
    );
    setMarkup('delivery-panel', deliveryPanelMarkup(lifecycleState.position));
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
        throw new Error(responseErrorMessage(payload, 'The requested action failed.'));
    }
    return payload;
}

async function loadDashboard() {
    const sequence = ++dashboardLoadSequence;
    activeDashboardLoadController?.abort();
    const controller = new AbortController();
    activeDashboardLoadController = controller;
    const base = `/api/projects/${selectedProjectId}`;
    try {
        const options = { signal: controller.signal };
        const [project, position, vision, goal, specification, authority, repository] = await Promise.all([
            requestJson(base, options),
            requestJson(`${base}/position`, options),
            requestJson(`${base}/vision/status`, options),
            requestJson(`${base}/goals/status`, options),
            requestJson(`${base}/specifications/review`, options),
            requestJson(`${base}/authority/review?include_spec=auto`, options),
            requestJson(`${base}/repository`, options),
        ]);
        if (sequence !== dashboardLoadSequence || controller.signal.aborted) return false;
        lifecycleState = {
            project: project.data ?? {},
            position: position.data ?? {},
            actions: position.actions ?? [],
            vision: vision.data ?? {},
            goal: goal.data ?? {},
            specification: specification.data ?? {},
            authority: authority.data ?? {},
            repository: repository.data ?? {},
        };
        setProjectError('');
        renderDashboard();
        return true;
    } catch (error) {
        if (sequence !== dashboardLoadSequence || controller.signal.aborted) return false;
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
        authority: state?.authority?.pending_authority?.authority_fingerprint,
        goal: state?.goal?.candidate?.fingerprint,
        specification: state?.specification?.candidate?.candidate_fingerprint,
        vision: state?.vision?.candidate?.review_fingerprint,
    }[scope] ?? null;
}

function captureReviewBinding(state, scope, decision) {
    const requestKind = {
        authority: 'decide_authority',
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
    const decision = binding.scope === 'authority' && binding.decision === 'feedback'
        ? 'rejected'
        : binding.decision;
    return {
        action: binding.action,
        expectedCandidate: binding.expectedCandidate,
        fields: {
            decision,
            rationale: rationale || 'Accepted in the dashboard.',
        },
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
        authority: 'Authority',
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
        if (pending.scope === 'authority' && pending.decision === 'feedback') {
            const recovery = await readPositionActions();
            const feedbackAction = captureAction(
                findAction(recovery.actions, 'record_authority_feedback'),
            );
            lifecycleState.position = recovery.position;
            lifecycleState.actions = recovery.actions;
            renderDashboard();
            try {
                await postAction(feedbackAction, { feedback: rationale });
            } catch (error) {
                closeHumanDialog();
                try {
                    await loadDashboard();
                } catch (_loadError) {
                    // The captured recovery action remains rendered from the position read.
                }
                setProjectError(`Authority was rejected, but feedback was not recorded. ${error.message}`);
                return;
            }
        }
    } else if (pending.kind === 'authority-feedback') {
        await postAction(pending.action, { feedback: rationale });
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
    button.disabled = true;
    setProjectError('');
    try {
        const action = findAction(lifecycleState.actions, requestKind)
            ?? (fallbackEndpoint ? { endpoint: fallbackEndpoint } : null);
        await postAction(action);
        await loadDashboard();
    } catch (error) {
        setProjectError(error.message);
    } finally {
        button.disabled = false;
    }
    return true;
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
        if (button.dataset.authorityFeedback) {
            const action = captureAction(
                findAction(lifecycleState.actions, 'record_authority_feedback'),
            );
            if (!action) {
                setProjectError('Authority feedback changed. Refresh and continue from the current state.');
                return;
            }
            openHumanDialog({
                kind: 'authority-feedback',
                action,
                title: 'Record Authority feedback',
                description: 'Record the specific correction needed before the Authority is recompiled.',
                label: 'Authority feedback',
                submitLabel: 'Record feedback',
                required: true,
            });
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

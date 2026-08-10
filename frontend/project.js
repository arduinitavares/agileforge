const STAGES = [
    'Vision',
    'Product Goal',
    'Discovery',
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
    record_authority_feedback: 'Authority',
    record_backlog_draft: 'Backlog',
    record_discovery_artifact: 'Discovery',
    record_post_sprint_triage: 'Review',
    record_product_goal_interview_turn: 'Product Goal',
    record_roadmap_draft: 'Roadmap',
    record_specification_candidate: 'Specification',
    record_sprint_plan: 'Sprint',
    record_story_draft: 'Stories',
    record_vision_interview_turn: 'Vision',
    reconcile_backlog: 'Backlog',
    repair_authority: 'Authority',
    repair_story_readiness: 'Stories',
    review_sprint: 'Review',
    start_sprint: 'Sprint',
};

const CHILD_STAGE = {
    authority: 'Authority',
    backlog: 'Backlog',
    execution: 'Execution',
    product_discovery: 'Discovery',
    product_goal: 'Product Goal',
    vision: 'Vision',
};

const BUTTON_PRIMARY = 'inline-flex min-h-10 items-center justify-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-800 disabled:cursor-wait disabled:opacity-60';
const BUTTON_SECONDARY = 'inline-flex min-h-10 items-center justify-center gap-2 rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-slate-800 hover:bg-slate-100 disabled:cursor-wait disabled:opacity-60';
const BUTTON_DANGER = 'inline-flex min-h-10 items-center justify-center gap-2 rounded-lg border border-red-300 bg-white px-4 py-2 text-sm font-semibold text-red-700 hover:bg-red-50 disabled:cursor-wait disabled:opacity-60';

let selectedProjectId = null;
let pendingHumanAction = null;
let lifecycleState = {
    project: {},
    position: {},
    actions: [],
    vision: {},
    goal: {},
    discovery: {},
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
            <span class="min-w-0 break-words">${escapeWorkflowText(question)}</span>
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
            ${questionListMarkup(questions)}
            ${transcriptMarkup(transcript, label)}
        </div>
        <form data-interview-scope="${scope}" class="min-w-0 self-start border-l-2 border-slate-200 pl-4">
            <label for="${scope}-response" class="text-sm font-semibold">Your response</label>
            <textarea id="${scope}-response" rows="6" required
                class="mt-2 w-full resize-y rounded-lg border-slate-300 text-sm leading-6 focus:border-accent focus:ring-accent"></textarea>
            <div class="mt-3 flex justify-end">
                <button type="submit" class="${BUTTON_PRIMARY}">
                    <span class="material-symbols-outlined" aria-hidden="true">send</span><span>Send response</span>
                </button>
            </div>
        </form>
    </div>`;
}

function visionPanelMarkup(projection, actions = []) {
    const candidate = projection?.candidate;
    const reviewState = projection?.review?.state;
    const reviewAction = findAction(actions, 'decide_vision_review');
    if (candidate && reviewState === 'pending') {
        return `${candidateMarkup(candidate, 'Vision')}${reviewControlsMarkup('vision', reviewAction)}`;
    }

    const respondAction = findAction(actions, 'record_vision_interview_turn');
    if (respondAction) {
        const questions = Array.isArray(projection?.latest_questions)
            && projection.latest_questions.length > 0
            ? projection.latest_questions
            : [
                'Who should benefit from this product first?',
                'What problem should be meaningfully different for them?',
                'What principles should guide product decisions?',
            ];
        const feedback = ['feedback', 'rejected'].includes(reviewState)
            ? `<p class="mb-5 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900"><strong>Review response:</strong> ${escapeWorkflowText(projection?.review?.rationale ?? 'Revise the Vision with the review in mind.')}</p>`
            : '';
        return `${feedback}<p class="mb-5 text-sm leading-6 text-slate-600">Shape the durable direction for this Project.</p>${interviewFormMarkup(
            'vision',
            questions,
            projection?.transcript ?? [],
            'Vision',
        )}`;
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

function discoveryPanelMarkup(projection) {
    const current = projection?.current;
    if (!current) {
        return '<p class="text-sm text-slate-600">Discovery has not been recorded.</p>';
    }
    return `<div class="max-w-4xl">
        <p class="mb-3 text-xs font-semibold uppercase text-accent">Current discovery</p>
        ${humanValueMarkup(current.canonical_content ?? {})}
    </div>`;
}

function specificationPanelMarkup(projection, actions = []) {
    const candidate = projection?.candidate;
    if (!candidate) {
        return '<p class="text-sm text-slate-600">A Specification candidate has not been recorded.</p>';
    }
    const review = projection?.review;
    const reviewAction = findAction(actions, 'decide_specification');
    const decisionCopy = review?.state && review.state !== 'pending'
        ? `<p class="mb-4 text-sm font-semibold text-slate-700">Review: ${escapeWorkflowText(humanizeKey(review.state))}</p>`
        : '<p class="mb-4 text-sm font-semibold text-slate-700">Exact Specification candidate</p>';
    return `<div class="max-w-4xl">${decisionCopy}${humanValueMarkup(candidate.canonical_content ?? {})}</div>
        ${review?.state === 'pending' ? reviewControlsMarkup('specification', reviewAction) : ''}`;
}

function invariantMarkup(invariant) {
    const parameters = invariant?.parameters && typeof invariant.parameters === 'object'
        ? Object.fromEntries(Object.entries(invariant.parameters).map(([key, value]) => [
            key,
            typeof value === 'string' ? value.replaceAll('_', ' ') : value,
        ]))
        : {};
    return `<li class="min-w-0 border-l-2 border-slate-300 pl-3">
        <p class="break-words text-sm font-semibold">${escapeWorkflowText(String(invariant?.type ?? 'Rule').replaceAll('_', ' '))}</p>
        ${Object.keys(parameters).length > 0 ? `<div class="mt-2">${humanValueMarkup(parameters)}</div>` : ''}
    </li>`;
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
    const invariants = Array.isArray(authority?.invariants) ? authority.invariants : [];
    const allFindings = [...(Array.isArray(findings) ? findings : [])];
    (Array.isArray(authority?.findings) ? authority.findings : []).forEach((finding) => {
        const message = typeof finding === 'string' ? finding : finding?.message;
        if (!allFindings.some((item) => (typeof item === 'string' ? item : item?.message) === message)) {
            allFindings.push(finding);
        }
    });
    return `<div class="grid min-w-0 gap-6 lg:grid-cols-2">
        <div class="min-w-0">
            <h3 class="text-sm font-semibold">Invariants</h3>
            ${invariants.length > 0
                ? `<ul class="mt-3 space-y-4">${invariants.map(invariantMarkup).join('')}</ul>`
                : '<p class="mt-2 text-sm text-slate-500">No invariants were compiled.</p>'}
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

    const repairAction = findAction(actions, 'repair_authority');
    if (repairAction) {
        return `<p class="mb-4 text-sm leading-6 text-slate-600">Feedback is recorded. Recompile the Authority for review.</p>
            <button type="button" data-direct-action="repair_authority" class="${BUTTON_PRIMARY}"><span class="material-symbols-outlined" aria-hidden="true">build</span><span>Recompile</span></button>`;
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
    setMarkup('vision-panel', visionPanelMarkup(lifecycleState.vision, lifecycleState.actions));
    setMarkup('goal-panel', productGoalPanelMarkup(lifecycleState.goal, lifecycleState.actions));
    setMarkup('discovery-panel', discoveryPanelMarkup(lifecycleState.discovery));
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

function responseErrorMessage(payload, fallback) {
    const detail = payload?.detail;
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
    const base = `/api/projects/${selectedProjectId}`;
    const [project, position, vision, goal, discovery, specification, authority, repository] = await Promise.all([
        requestJson(base),
        requestJson(`${base}/position`),
        requestJson(`${base}/vision/status`),
        requestJson(`${base}/goals/status`),
        requestJson(`${base}/discovery`),
        requestJson(`${base}/specifications/review`),
        requestJson(`${base}/authority/review?include_spec=auto`),
        requestJson(`${base}/repository`),
    ]);
    lifecycleState = {
        project: project.data ?? {},
        position: position.data ?? {},
        actions: position.actions ?? [],
        vision: vision.data ?? {},
        goal: goal.data ?? {},
        discovery: discovery.data ?? {},
        specification: specification.data ?? {},
        authority: authority.data ?? {},
        repository: repository.data ?? {},
    };
    setProjectError('');
    renderDashboard();
}

async function postAction(action, fields = {}) {
    if (!action) throw new Error('This action is not available in the current lifecycle state.');
    return requestJson(`/api/projects/${selectedProjectId}/${action.endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
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

async function refreshPositionActions() {
    const payload = await requestJson(`/api/projects/${selectedProjectId}/position`);
    lifecycleState.position = payload.data ?? {};
    lifecycleState.actions = payload.actions ?? [];
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
        const requestKinds = {
            authority: 'decide_authority',
            goal: 'decide_product_goal_review',
            specification: 'decide_specification',
            vision: 'decide_vision_review',
        };
        const action = findAction(lifecycleState.actions, requestKinds[pending.scope]);
        const decision = pending.scope === 'authority' && pending.decision === 'feedback'
            ? 'rejected'
            : pending.decision;
        await postAction(action, {
            decision,
            rationale: rationale || 'Accepted in the dashboard.',
        });
        if (pending.scope === 'authority' && pending.decision === 'feedback') {
            await refreshPositionActions();
            const feedbackAction = findAction(lifecycleState.actions, 'record_authority_feedback');
            await postAction(feedbackAction, { feedback: rationale });
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
        const textarea = document.getElementById(`${scope}-response`);
        const text = textarea?.value.trim() ?? '';
        if (!text) return;
        const requestKind = scope === 'vision'
            ? 'record_vision_interview_turn'
            : 'record_product_goal_interview_turn';
        const submit = form.querySelector('button[type="submit"]');
        if (submit) submit.disabled = true;
        setProjectError('');
        try {
            await postAction(findAction(lifecycleState.actions, requestKind), { text });
            await loadDashboard();
        } catch (error) {
            setProjectError(error.message);
        } finally {
            if (submit) submit.disabled = false;
        }
    });

    document.addEventListener('click', (event) => {
        const button = event.target.closest('button');
        if (!button) return;
        if (button.dataset.reviewScope) {
            const copy = reviewDialogCopy(button.dataset.reviewScope, button.dataset.reviewDecision);
            openHumanDialog({
                ...copy,
                kind: 'review',
                scope: button.dataset.reviewScope,
                decision: button.dataset.reviewDecision,
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

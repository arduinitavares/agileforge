const FALLBACK_WORKFLOW_STEPS = [
    { id: 'setup', label: 'Project Setup', states: ['SETUP_REQUIRED'] },
    { id: 'vision', label: 'Vision', states: ['VISION_INTERVIEW', 'VISION_REVIEW', 'VISION_PERSISTENCE'] },
    { id: 'backlog', label: 'Backlog', states: ['BACKLOG_INTERVIEW', 'BACKLOG_REVIEW', 'BACKLOG_PERSISTENCE'] },
    { id: 'roadmap', label: 'Roadmap', states: ['ROADMAP_INTERVIEW', 'ROADMAP_REVIEW', 'ROADMAP_PERSISTENCE'] },
    { id: 'story', label: 'Stories', states: ['STORY_INTERVIEW', 'STORY_REVIEW', 'STORY_PERSISTENCE'] },
    {
        id: 'sprint',
        label: 'Sprint',
        states: [
            'SPRINT_SETUP',
            'SPRINT_DRAFT',
            'SPRINT_PERSISTENCE',
            'SPRINT_VIEW',
            'SPRINT_LIST',
            'SPRINT_UPDATE_STORY',
            'SPRINT_MODIFY',
            'SPRINT_COMPLETE',
        ],
    },
];

const STEP_ICONS = {
    setup: 'settings',
    vision: 'visibility',
    backlog: 'format_list_bulleted',
    roadmap: 'timeline',
    story: 'description',
    sprint: 'bolt',
};

const PHASE_ORDER = ['setup', 'vision', 'backlog', 'roadmap', 'story', 'sprint'];
const PANEL_ORDER = ['overview', ...PHASE_ORDER];
const NEXT_PHASE = {
    setup: 'vision',
    vision: 'backlog',
    backlog: 'roadmap',
    roadmap: 'story',
    story: 'sprint',
    sprint: null,
};

const PHASE_TERMINAL_STATES = {
    setup: ['VISION_INTERVIEW', 'VISION_REVIEW', 'VISION_PERSISTENCE', 'BACKLOG_INTERVIEW', 'BACKLOG_REVIEW', 'BACKLOG_PERSISTENCE', 'ROADMAP_INTERVIEW', 'ROADMAP_REVIEW', 'ROADMAP_PERSISTENCE', 'STORY_INTERVIEW', 'STORY_REVIEW', 'STORY_PERSISTENCE', 'SPRINT_SETUP', 'SPRINT_DRAFT', 'SPRINT_PERSISTENCE', 'SPRINT_COMPLETE'],
    vision: ['VISION_PERSISTENCE'],
    backlog: ['BACKLOG_PERSISTENCE'],
    roadmap: ['ROADMAP_PERSISTENCE'],
    story: ['STORY_PERSISTENCE'],
    sprint: ['SPRINT_PERSISTENCE', 'SPRINT_COMPLETE'],
};

let dashboardConfig = null;
let selectedProjectId = null;
let activeFsmState = 'SETUP_REQUIRED';
let activePhaseId = 'setup';
let viewPhaseId = 'setup';
let currentProjectState = { setup_status: 'failed', setup_error: null };
let currentAuthorityReview = null;
let savedSprints = [];
let sprintRuntimeSummary = null;
let currentSprintId = null;
let currentSprintDetail = null;
let currentSprintClosePreview = null;
let sprintMode = null;
let showSprintPlanner = false;

let latestVisionIsComplete = false;
let visionAttemptCount = 0;

let latestBacklogIsComplete = false;
let backlogAttemptCount = 0;

let latestRoadmapIsComplete = false;
let roadmapAttemptCount = 0;

// Story Phase State
let storyRequirements = []; // Array of { requirement, status, attempt_count }
let activeStoryReq = null;
let activeStoryAttemptCount = 0;
let activeStoryIsComplete = false;
let activeStoryRetryAvailable = false;
let activeStoryRetryTargetAttemptId = null;
let activeStorySaveAvailable = false;
let activeStoryDraftKind = null;
let activeStoryLatestClassification = null;
let activeStoryResolutionAvailable = false;
let activeStoryResolutionCurrent = null;
let activeStoryResolutionRecommendation = null;

// Variables to hold raw JSON data for the copy feature
let currentVisionArtifactJSON = null;
let currentBacklogArtifactJSON = null;
let currentRoadmapArtifactJSON = null;
let currentStoryArtifactJSON = null;
let currentSprintArtifactJSON = null;
let currentSprintInputContextJSON = null;

let latestSprintIsComplete = false;
let sprintAttemptCount = 0;
let sprintCandidates = [];
let selectedSprintStoryIds = new Set();

const SPRINT_VELOCITY_LIMITS = {
    Low: 3,
    Medium: 5,
    High: 7,
};

window.addEventListener('DOMContentLoaded', async () => {
    // 1. Get Project ID from URL
    const urlParams = new URLSearchParams(window.location.search);
    const idParam = urlParams.get('id');

    if (!idParam) {
        alert("No project ID found in URL. Returning to dashboard.");
        window.location.href = '/dashboard';
        return;
    }
    selectedProjectId = parseInt(idParam, 10);

    // 2. Fetch config & set initial state
    await fetchDashboardConfig();
    document.getElementById('setup-panel').classList.remove('hidden');

    setPhaseState('SETUP_REQUIRED', 'setup');
    attachPhaseNavigation();

    // 3. Load initial project data & state
    await loadInitialProjectMetadata();
    await loadSavedSprints();
    await fetchProjectFSMState(selectedProjectId);
    await loadVisionHistory();
    await loadBacklogHistory();
    await loadRoadmapHistory();
    await loadStoryRequirements();
    attachSprintInputListeners();
    await loadSprintHistory();
});

async function fetchDashboardConfig() {
    try {
        const response = await fetch('/api/dashboard/config');
        const data = await response.json();
        dashboardConfig = data?.status === 'success' ? data.data : null;
    } catch (error) {
        console.error('Error fetching dashboard config:', error);
        dashboardConfig = null;
    }
}

async function loadInitialProjectMetadata() {
    try {
        const response = await fetch('/api/projects');
        const data = await response.json();
        if (data.status === 'success') {
            const project = data.data.find(p => p.id === selectedProjectId);
            const title = document.getElementById('project-page-title');
            const nameInput = document.getElementById('setup-project-name');
            if (project) {
                if (title) title.innerText = project.name;
                if (nameInput) nameInput.value = project.name;
            } else {
                if (title) title.innerText = `Project ${selectedProjectId}`;
                if (nameInput) nameInput.value = `Project ${selectedProjectId}`;
            }
        }
    } catch (e) {
        console.error("Failed to load generic metadata");
    }
}


function getWorkflowSteps() {
    const configSteps = dashboardConfig?.workflow_steps;
    if (Array.isArray(configSteps) && configSteps.length > 0) {
        return configSteps;
    }
    return FALLBACK_WORKFLOW_STEPS;
}

function normalizeStateKey(value) {
    if (typeof value !== 'string') return 'SETUP_REQUIRED';
    const normalized = value.trim().toUpperCase();
    return normalized || 'SETUP_REQUIRED';
}

function getPhaseIdForState(stateKey) {
    const step = getWorkflowSteps().find((item) => Array.isArray(item.states) && item.states.includes(stateKey));
    return step ? step.id : 'setup';
}

function phaseIndex(phaseId) {
    return PHASE_ORDER.indexOf(phaseId);
}

function capitalizePhase(phaseId) {
    if (phaseId === 'story') return 'Stories';
    return phaseId.charAt(0).toUpperCase() + phaseId.slice(1);
}

function isResolvedStoryStatus(status) {
    return status === 'Saved' || status === 'Merged';
}

function deriveStoryProjectionState(payload) {
    const currentDraft = payload?.current_draft || null;
    const retry = payload?.retry || {};
    const save = payload?.save || {};
    const resolution = payload?.resolution || {};

    return {
        isComplete: Boolean(currentDraft?.is_complete),
        retryAvailable: Boolean(retry.available),
        retryTargetAttemptId: typeof retry.target_attempt_id === 'string'
            ? retry.target_attempt_id
            : null,
        saveAvailable: Boolean(save.available),
        draftKind: typeof currentDraft?.kind === 'string' ? currentDraft.kind : null,
        resolutionAvailable: Boolean(resolution.available),
        resolutionCurrent: resolution.current && typeof resolution.current === 'object'
            ? resolution.current
            : null,
        resolutionRecommendation: resolution.recommendation && typeof resolution.recommendation === 'object'
            ? resolution.recommendation
            : null,
    };
}

function getSavedSprintById(sprintId) {
    return savedSprints.find((sprint) => sprint.id === Number(sprintId)) || null;
}

function getSprintMode(savedSprint) {
    const normalized = String(savedSprint?.status || 'Planned').toLowerCase();
    if (normalized === 'completed') return 'completed';
    if (normalized === 'active') return 'active';
    return 'planned';
}

function chooseLandingSprint() {
    const activeSprints = savedSprints
        .filter((sprint) => getSprintMode(sprint) === 'active')
        .sort((left, right) => {
            const leftStarted = new Date(left.started_at || left.updated_at || left.created_at || 0).getTime();
            const rightStarted = new Date(right.started_at || right.updated_at || right.created_at || 0).getTime();
            if (rightStarted !== leftStarted) return rightStarted - leftStarted;
            return new Date(right.created_at || 0).getTime() - new Date(left.created_at || 0).getTime();
        });
    if (activeSprints[0]) return activeSprints[0];

    const plannedSprints = savedSprints
        .filter((sprint) => getSprintMode(sprint) === 'planned')
        .sort((left, right) => {
            const leftCreated = new Date(left.created_at || 0).getTime();
            const rightCreated = new Date(right.created_at || 0).getTime();
            return rightCreated - leftCreated;
        });
    if (plannedSprints[0]) return plannedSprints[0];

    const completedSprints = savedSprints
        .filter((sprint) => getSprintMode(sprint) === 'completed')
        .sort((left, right) => {
            const leftCompleted = new Date(left.completed_at || left.updated_at || left.created_at || 0).getTime();
            const rightCompleted = new Date(right.completed_at || right.updated_at || right.created_at || 0).getTime();
            return rightCompleted - leftCompleted;
        });
    return completedSprints[0] || null;
}

function ensureCurrentSprintSelection() {
    const selectedSprint = currentSprintId ? getSavedSprintById(currentSprintId) : null;
    if (selectedSprint) return selectedSprint;

    const landingSprint = chooseLandingSprint();
    if (!landingSprint) {
        currentSprintId = null;
        sprintMode = null;
        return null;
    }

    currentSprintId = landingSprint.id;
    sprintMode = getSprintMode(landingSprint);
    return landingSprint;
}

function isPlanningCompleteState(stateKey) {
    return [
        'SPRINT_PERSISTENCE',
        'SPRINT_VIEW',
        'SPRINT_LIST',
        'SPRINT_UPDATE_STORY',
        'SPRINT_MODIFY',
        'SPRINT_COMPLETE',
    ].includes(stateKey);
}

function getNextIncompletePlanningPhase(stateKey) {
    if (currentProjectState.setup_status === 'failed') {
        return 'setup';
    }

    const mappedPhaseId = getPhaseIdForState(stateKey);
    if (mappedPhaseId === 'sprint' && isPlanningCompleteState(stateKey)) {
        return null;
    }

    if ((PHASE_TERMINAL_STATES[mappedPhaseId] || []).includes(stateKey)) {
        return NEXT_PHASE[mappedPhaseId] || null;
    }

    return mappedPhaseId;
}

function resolveProjectLanding(stateKey) {
    const nextPlanningPhase = getNextIncompletePlanningPhase(stateKey);
    if (nextPlanningPhase) {
        return { phaseId: nextPlanningPhase };
    }

    const landingSprint = chooseLandingSprint();
    if (landingSprint) {
        return {
            phaseId: 'sprint',
            currentSprintId: landingSprint.id,
            sprintMode: getSprintMode(landingSprint),
        };
    }

    return { phaseId: 'overview' };
}

function getCompletedPhaseCount() {
    const activeIndex = phaseIndex(activePhaseId);
    if (activeIndex < 0) return 0;

    let completedCount = activeIndex;
    if ((PHASE_TERMINAL_STATES[activePhaseId] || []).includes(activeFsmState)) {
        completedCount += 1;
    }

    return Math.max(0, Math.min(PHASE_ORDER.length, completedCount));
}

function shouldHideWorkflowFooter() {
    return viewPhaseId === 'overview' || (viewPhaseId === 'sprint' && currentSprintId && !showSprintPlanner);
}

function applyResolvedLanding(stateKey, landing) {
    if (landing.phaseId === 'overview') {
        currentSprintId = null;
        sprintMode = null;
        showSprintPlanner = false;
        setPhaseState(stateKey, 'overview');
        return;
    }

    if (landing.phaseId === 'sprint') {
        const savedSprint = landing.currentSprintId ? getSavedSprintById(landing.currentSprintId) : null;
        if (savedSprint) {
            currentSprintId = savedSprint.id;
            sprintMode = landing.sprintMode || getSprintMode(savedSprint);
            showSprintPlanner = false;
        } else {
            currentSprintId = null;
            sprintMode = null;
            showSprintPlanner = true;
        }
        setPhaseState(stateKey, 'sprint');
        return;
    }

    showSprintPlanner = false;
    setPhaseState(stateKey, landing.phaseId);
}

function updateRetryButton() {
    const retryBtn = document.getElementById('btn-retry-setup');
    if (!retryBtn) return;

    if (currentProjectState.setup_status === 'failed') {
        retryBtn.classList.remove('hidden');
    } else {
        retryBtn.classList.add('hidden');
    }
}

function updateSetupStatusBanner() {
    const banner = document.getElementById('setup-status-banner');
    if (!banner) return;

    banner.classList.remove('hidden');
    if (currentProjectState.setup_status === 'passed') {
        banner.className = 'text-sm rounded-lg border px-4 py-3 border-emerald-200 bg-emerald-50 text-emerald-700';
        banner.innerText = 'Setup passed. Specification linked and authority compiled.';
        updateSetupPanelCopy('Project Setup', 'Project setup is complete.');
        renderAuthorityReviewCard(true);
        return;
    }

    if (currentProjectState.setup_status === 'authority_pending_review') {
        banner.className = 'text-sm rounded-lg border px-4 py-3 border-sky-200 bg-sky-50 text-sky-700';
        banner.innerText = 'Pending Authority Review. Review the compiled authority before Vision is unlocked.';
        updateSetupPanelCopy('Pending Authority Review', 'Review the compiled authority and either accept or reject it.');
        renderAuthorityReviewCard(true);
        return;
    }

    if (currentProjectState.setup_status === 'authority_rejected') {
        banner.className = 'text-sm rounded-lg border px-4 py-3 border-rose-200 bg-rose-50 text-rose-700';
        banner.innerText = currentProjectState.setup_error || 'Authority Rejected. Update the specification or recompile authority before continuing.';
        updateSetupPanelCopy('Authority Rejected', 'Vision remains locked until authority is reviewed again and accepted.');
        renderAuthorityReviewCard(false);
        return;
    }

    banner.className = 'text-sm rounded-lg border px-4 py-3 border-amber-200 bg-amber-50 text-amber-700';
    banner.innerText = currentProjectState.setup_error || 'Setup is required before Vision.';
    updateSetupPanelCopy('Project Setup', 'Project setup requires a valid specification file path and successful authority compile before proceeding.');
    renderAuthorityReviewCard(false);
}

function updateSetupPanelCopy(title, description) {
    const titleEl = document.getElementById('setup-panel-title');
    const descriptionEl = document.getElementById('setup-panel-description');
    if (titleEl) titleEl.innerText = title;
    if (descriptionEl) descriptionEl.innerText = description;
}

let currentAuthorityReviewActiveTab = 'overview';
let authorityInvariantSearchQuery = '';
let authoritySupportFilter = 'all';

function safeArray(value) {
    return Array.isArray(value) ? value : [];
}

function authorityReviewState(review) {
    if (review?.post_accept === true) {
        return {
            label: 'Accepted',
            tone: 'accepted',
            acceptDisabled: true,
            decision: 'Authority accepted. Vision is unlocked.',
            reason: '',
        };
    }

    if (!Array.isArray(review?.pending_authority?.review_findings)) {
        return {
            label: 'Review Incomplete',
            tone: 'blocked',
            acceptDisabled: true,
            decision: 'Review packet is incomplete.',
            reason: 'Reload the authority review before deciding.',
        };
    }

    const findings = safeArray(review?.pending_authority?.review_findings);
    const blocking = findings.filter((finding) => finding?.severity === 'blocking');
    const nonOverrideable = blocking.filter((finding) => finding?.override_allowed === false);

    if (nonOverrideable.length > 0) {
        const first = nonOverrideable[0];
        return {
            label: 'Blocked',
            tone: 'blocked',
            acceptDisabled: true,
            decision: 'Authority cannot be accepted yet.',
            reason: `${first.code || 'Blocking finding'} must be resolved before acceptance.`,
        };
    }

    if (blocking.length > 0) {
        return {
            label: 'Override Required',
            tone: 'warning',
            acceptDisabled: false,
            decision: 'Authority has overrideable blocking findings.',
            reason: 'Accepting will request candidate-specific override rationale.',
        };
    }

    return {
        label: 'Accept Ready',
        tone: 'ready',
        acceptDisabled: false,
        decision: 'Authority is ready for human acceptance.',
        reason: 'Review the compiled artifacts, then accept or request refinement.',
    };
}

function setTextContent(id, value) {
    const element = document.getElementById(id);
    if (!element) return;
    element.textContent = value == null ? '' : String(value);
}

function createMetricElement(label, value) {
    const item = document.createElement('div');
    item.className = 'rounded-lg border border-slate-200 bg-white/80 p-2 dark:border-slate-800 dark:bg-slate-950/30';

    const labelEl = document.createElement('div');
    labelEl.className = 'text-[10px] font-black uppercase tracking-wide text-slate-500 dark:text-slate-400';
    labelEl.textContent = label;
    item.appendChild(labelEl);

    const valueEl = document.createElement('div');
    valueEl.className = 'mt-1 text-lg font-black text-slate-900 dark:text-slate-100';
    valueEl.textContent = String(value);
    item.appendChild(valueEl);

    return item;
}

function sourceStateLabel(spec) {
    if (spec?.content_truncated === true) return 'Source excerpt shown; full content is truncated.';
    if (spec?.content_included === true) return 'Full source included.';
    if (spec?.excerpt) return 'Source excerpt available.';
    return 'Source content unavailable in review packet.';
}

function applyStateBadgeTone(element, tone) {
    if (!element) return;
    const base = 'rounded-full px-2.5 py-1 text-[11px] font-black uppercase tracking-wide';
    const tones = {
        accepted: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-100',
        blocked: 'bg-rose-100 text-rose-800 dark:bg-rose-900/50 dark:text-rose-100',
        warning: 'bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-100',
        ready: 'bg-sky-100 text-sky-800 dark:bg-sky-900/50 dark:text-sky-100',
    };
    element.className = `${base} ${tones[tone] || tones.ready}`;
}

function renderAuthoritySummary(review) {
    const project = review?.project || {};
    const spec = review?.spec || {};
    const pending = review?.pending_authority || {};
    const artifact = pending.artifact || {};
    const state = authorityReviewState(review);
    const findings = safeArray(pending.review_findings);

    const title = review?.post_accept === true
        ? 'Accepted Authority Invariants'
        : 'Pending Authority Review';
    setTextContent('authority-card-title', title);
    setTextContent('authority-review-state-badge', state.label);
    applyStateBadgeTone(document.getElementById('authority-review-state-badge'), state.tone);

    const sourcePath = spec.resolved_path || 'linked specification';
    const authorityLabel = pending.authority_id || pending.pending_authority_id || 'pending';
    const summaryPrefix = review?.post_accept === true
        ? `${project.name || 'Project'} authority is accepted.`
        : `${project.name || 'Project'} authority ${authorityLabel} awaits review.`;
    setTextContent(
        'authority-review-summary',
        `${summaryPrefix} Coverage: ${spec.coverage_summary?.omission_assessment || 'unknown'}. Source: ${sourcePath}.`,
    );

    const metrics = document.getElementById('authority-review-metrics');
    if (metrics) {
        metrics.replaceChildren(
            createMetricElement('Blockers', findings.filter((finding) => finding?.severity === 'blocking').length),
            createMetricElement('Invariants', safeArray(artifact.invariants).length),
            createMetricElement('Gaps', safeArray(artifact.gaps).length),
            createMetricElement('Assumptions', safeArray(artifact.assumptions).length),
            createMetricElement('Excluded', safeArray(artifact.rejected_features).length),
            createMetricElement('Rules', safeArray(artifact.eligible_feature_rules).length),
        );
    }

    setTextContent('authority-spec-location', sourcePath);
    setTextContent('authority-spec-hash', spec.spec_hash || spec.disk_sha256 || 'No spec hash available');
    setTextContent('authority-fingerprint', pending.authority_fingerprint || 'No authority fingerprint available');
    setTextContent(
        'authority-compiled-at',
        [pending.compiled_at || 'Compile time unavailable', pending.compiler_version || 'compiler unknown'].join(' - '),
    );
    setTextContent('authority-source-state', sourceStateLabel(spec));
    setTextContent('authority-decision-status', state.decision);
    setTextContent('authority-blocked-reason', state.reason);

    const acceptButton = document.getElementById('btn-accept-authority');
    if (acceptButton) acceptButton.disabled = state.acceptDisabled;

    const actionsContainer = document.getElementById('authority-review-actions-container');
    const findingsEl = document.getElementById('authority-review-findings');
    if (review?.post_accept === true) {
        if (actionsContainer) actionsContainer.classList.add('hidden');
        if (findingsEl) findingsEl.classList.add('hidden');
    } else {
        if (actionsContainer) actionsContainer.classList.remove('hidden');
        if (findingsEl) {
            const blockingCount = findings.filter((finding) => finding?.severity === 'blocking').length;
            findingsEl.textContent = blockingCount
                ? `${blockingCount} blocking review finding(s) require resolution or candidate-specific overrides.`
                : '';
            findingsEl.classList.toggle('hidden', blockingCount === 0);
        }
    }
}

function switchAuthorityReviewTab(tabName) {
    currentAuthorityReviewActiveTab = tabName;
    const tabs = ['overview', 'invariants', 'spec', 'raw'];
    
    tabs.forEach(t => {
        const btn = document.getElementById(`tab-btn-${t}`);
        const content = document.getElementById(`tab-content-${t}`);
        if (!btn || !content) return;
        
        if (t === tabName) {
            btn.className = 'px-4 py-2 -mb-px text-xs font-bold border-b-2 border-sky-600 text-sky-600 dark:border-sky-400 dark:text-sky-300 transition-colors focus:outline-none';
            content.classList.remove('hidden');
        } else {
            btn.className = 'px-4 py-2 -mb-px text-xs font-bold border-b-2 border-transparent text-slate-500 hover:text-sky-600 dark:text-slate-400 dark:hover:text-sky-300 transition-colors focus:outline-none';
            content.classList.add('hidden');
        }
    });
}

function createInvariantCard(invariant) {
    const card = document.createElement('div');
    card.className = 'p-3 rounded-lg border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/50 shadow-sm space-y-2 hover:border-sky-200 dark:hover:border-sky-900 transition-all';
    
    const header = document.createElement('div');
    header.className = 'flex items-center justify-between gap-2 flex-wrap';
    
    const leftBadges = document.createElement('div');
    leftBadges.className = 'flex items-center gap-1.5';
    
    const idStr = invariant.id || '';
    let idColorClass = 'bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-200';
    if (idStr.startsWith('REQ.')) {
        idColorClass = 'bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-200';
    } else if (idStr.startsWith('DATA.')) {
        idColorClass = 'bg-purple-100 text-purple-800 dark:bg-purple-900/40 dark:text-purple-200';
    } else if (idStr.startsWith('CONSTRAINT.')) {
        idColorClass = 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200';
    } else if (idStr.startsWith('INTERFACE.')) {
        idColorClass = 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-200';
    }
    
    const idBadge = document.createElement('span');
    idBadge.className = `px-2 py-0.5 rounded text-[10px] font-black uppercase ${idColorClass}`;
    idBadge.textContent = idStr;
    leftBadges.appendChild(idBadge);
    
    const supportBadge = document.createElement('span');
    const supportStr = invariant.support || 'inferred';
    let supportColorClass = 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-400';
    if (supportStr === 'direct') {
        supportColorClass = 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200';
    }
    supportBadge.className = `px-1.5 py-0.5 rounded text-[9px] font-bold uppercase ${supportColorClass}`;
    supportBadge.textContent = supportStr;
    leftBadges.appendChild(supportBadge);
    
    header.appendChild(leftBadges);
    
    const rightRefs = document.createElement('div');
    rightRefs.className = 'flex flex-wrap gap-1';
    
    const refs = invariant.source_refs || [];
    refs.forEach(ref => {
        const refSpan = document.createElement('span');
        refSpan.className = 'px-1.5 py-0.5 rounded text-[9px] font-semibold bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400';
        refSpan.textContent = ref;
        rightRefs.appendChild(refSpan);
    });
    
    header.appendChild(rightRefs);
    card.appendChild(header);
    
    const textP = document.createElement('p');
    textP.className = 'text-xs text-slate-700 dark:text-slate-300 whitespace-pre-wrap leading-relaxed font-medium';
    textP.textContent = invariant.text || '';
    card.appendChild(textP);
    
    return card;
}

function createSimpleCard(item, badgeColorClass) {
    const card = document.createElement('div');
    card.className = 'p-2.5 rounded-lg border border-slate-200 dark:border-slate-800 bg-white/50 dark:bg-slate-900/30 text-xs shadow-sm hover:border-slate-300 dark:hover:border-slate-700 transition-all flex items-start gap-2';
    
    const badge = document.createElement('span');
    badge.className = `px-1.5 py-0.5 rounded text-[9px] font-black uppercase flex-shrink-0 ${badgeColorClass}`;
    badge.textContent = item.id || '';
    card.appendChild(badge);
    
    const contentDiv = document.createElement('div');
    contentDiv.className = 'text-slate-700 dark:text-slate-300 whitespace-pre-wrap font-medium';
    contentDiv.textContent = item.text || '';
    card.appendChild(contentDiv);
    
    return card;
}

function createEmptyState(text) {
    const empty = document.createElement('div');
    empty.className = 'text-xs text-slate-500 font-medium italic p-2';
    empty.textContent = text;
    return empty;
}

function createFindingCard(finding) {
    const card = document.createElement('div');
    card.className = 'p-2.5 rounded-lg border border-slate-200 dark:border-slate-800 bg-white/50 dark:bg-slate-900/30 text-xs shadow-sm space-y-2';

    const header = document.createElement('div');
    header.className = 'flex flex-wrap items-center gap-1.5';

    const codeBadge = document.createElement('span');
    codeBadge.className = 'px-1.5 py-0.5 rounded text-[9px] font-black uppercase bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300';
    codeBadge.textContent = finding?.code || 'Finding';
    header.appendChild(codeBadge);

    const severityBadge = document.createElement('span');
    severityBadge.className = 'px-1.5 py-0.5 rounded text-[9px] font-bold uppercase bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200';
    severityBadge.textContent = finding?.severity || 'info';
    header.appendChild(severityBadge);

    const overrideBadge = document.createElement('span');
    overrideBadge.className = finding?.override_allowed === false
        ? 'px-1.5 py-0.5 rounded text-[9px] font-bold uppercase bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-200'
        : 'px-1.5 py-0.5 rounded text-[9px] font-bold uppercase bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200';
    overrideBadge.textContent = finding?.override_allowed === false ? 'No override' : 'Override allowed';
    header.appendChild(overrideBadge);

    card.appendChild(header);

    const message = finding?.message || finding?.detail || finding?.text || '';
    if (message) {
        const messageEl = document.createElement('div');
        messageEl.className = 'text-slate-700 dark:text-slate-300 whitespace-pre-wrap font-medium';
        messageEl.textContent = message;
        card.appendChild(messageEl);
    }

    const candidateIds = safeArray(finding?.candidate_ids);
    if (candidateIds.length > 0) {
        const candidatesEl = document.createElement('div');
        candidatesEl.className = 'text-[11px] text-slate-500 dark:text-slate-400 font-semibold';
        candidatesEl.textContent = `Candidates: ${candidateIds.join(', ')}`;
        card.appendChild(candidatesEl);
    }

    return card;
}

function renderListSection(containerId, items, emptyText, createItem) {
    const container = document.getElementById(containerId);
    if (!container) return;

    const normalizedItems = safeArray(items);
    if (normalizedItems.length === 0) {
        container.replaceChildren(createEmptyState(emptyText));
        return;
    }

    container.replaceChildren(...normalizedItems.map((item) => createItem(item)));
}

function renderAuthorityOverview(review) {
    const pending = review?.pending_authority || {};
    const artifact = pending.artifact || {};

    renderListSection(
        'overview-findings-list',
        pending.review_findings,
        'No review findings.',
        createFindingCard,
    );
    renderListSection(
        'overview-gaps-list',
        artifact.gaps,
        'No identified gaps.',
        (gap) => createSimpleCard(gap, 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200'),
    );
    renderListSection(
        'overview-assumptions-list',
        artifact.assumptions,
        'No compiler assumptions.',
        (assumption) => createSimpleCard(assumption, 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-200'),
    );
    renderListSection(
        'overview-exclusions-list',
        artifact.rejected_features,
        'No excluded features.',
        (exclusion) => createSimpleCard(exclusion, 'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-200'),
    );
    renderListSection(
        'overview-eligible-rules-list',
        artifact.eligible_feature_rules,
        'No eligible feature rules.',
        (rule) => createSimpleCard(rule, 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200'),
    );
}

function renderAuthorityReviewCard(visible) {
    const card = document.getElementById('authority-review-card');
    if (!card) return;

    if (!visible) {
        card.classList.add('hidden');
        return;
    }

    card.classList.remove('hidden');
    const titleEl = document.getElementById('authority-card-title');
    const summary = document.getElementById('authority-review-summary');
    const findingsEl = document.getElementById('authority-review-findings');
    const actionsContainer = document.getElementById('authority-review-actions-container');
    const preview = document.getElementById('authority-review-preview');

    const invariantsList = document.getElementById('invariants-list');
    const gapsList = document.getElementById('gaps-list');
    const assumptionsList = document.getElementById('assumptions-list');
    const exclusionsList = document.getElementById('exclusions-list');
    const eligibleRulesList = document.getElementById('eligible-rules-list');
    const domainBadge = document.getElementById('authority-domain-badge');
    const themesContainer = document.getElementById('authority-themes-container');
    const specViewer = document.getElementById('spec-source-viewer');
    const truncationBanner = document.getElementById('spec-truncation-banner');

    if (!currentAuthorityReview) {
        if (titleEl) titleEl.innerText = 'Loading Authority Review';
        if (summary) summary.innerText = 'Fetching review data from backend...';
        if (findingsEl) findingsEl.innerText = '';
        if (invariantsList) invariantsList.innerHTML = '';
        if (gapsList) gapsList.innerHTML = '';
        if (assumptionsList) assumptionsList.innerHTML = '';
        if (exclusionsList) exclusionsList.innerHTML = '';
        if (eligibleRulesList) eligibleRulesList.innerHTML = '';
        if (domainBadge) domainBadge.textContent = '';
        if (themesContainer) themesContainer.innerHTML = '';
        if (specViewer) specViewer.textContent = '';
        if (truncationBanner) truncationBanner.classList.add('hidden');
        if (preview) preview.innerText = '';
        return;
    }

    const isPostAccept = currentAuthorityReview.post_accept === true;
    
    // Theme styling based on post-accept state
    if (isPostAccept) {
        card.className = "mt-4 rounded-lg border border-slate-200 bg-slate-50 p-4 text-sm text-slate-900 dark:border-slate-800 dark:bg-slate-900/20 dark:text-slate-100";
        if (titleEl) titleEl.innerText = 'Accepted Authority Invariants';
        if (findingsEl) findingsEl.classList.add('hidden');
        if (actionsContainer) actionsContainer.classList.add('hidden');
    } else {
        card.className = "mt-4 rounded-lg border border-sky-200 bg-sky-50 p-4 text-sm text-sky-900 dark:border-sky-800 dark:bg-sky-950/40 dark:text-sky-100";
        if (titleEl) titleEl.innerText = 'Pending Authority Review';
        if (findingsEl) findingsEl.classList.remove('hidden');
        if (actionsContainer) actionsContainer.classList.remove('hidden');
    }

    const project = currentAuthorityReview.project || {};
    const spec = currentAuthorityReview.spec || {};
    const pending = currentAuthorityReview.pending_authority || {};
    const authorityId = pending.authority_id || pending.pending_authority_id || 'pending';
    const omission = spec.coverage_summary?.omission_assessment || 'unknown';
    const path = spec.resolved_path || 'linked specification';

    if (summary) {
        if (isPostAccept) {
            summary.innerText = `${project.name || 'Project'} authority is complete and accepted. Coverage: ${omission}. Source: ${path}.`;
        } else {
            summary.innerText = `${project.name || 'Project'} authority ${authorityId} awaits review. Coverage: ${omission}. Source: ${path}.`;
        }
    }

    if (findingsEl && !isPostAccept) {
        const findings = pending.review_findings || [];
        const blockingCount = findings.filter((finding) => finding?.severity === 'blocking').length;
        findingsEl.innerText = blockingCount
            ? `${blockingCount} blocking review finding(s) require resolution or candidate-specific overrides.`
            : '';
    }

    // Render Invariants & Elements
    const artifact = pending.artifact || {};
    
    // Domain Badge
    if (domainBadge) {
        domainBadge.textContent = artifact.domain || 'N/A';
    }

    // Themes Badge Container
    if (themesContainer) {
        themesContainer.innerHTML = '';
        const themes = artifact.scope_themes || [];
        if (themes.length === 0) {
            const none = document.createElement('span');
            none.className = 'text-xs text-slate-500 font-medium italic';
            none.textContent = 'None';
            themesContainer.appendChild(none);
        } else {
            themes.forEach(theme => {
                const span = document.createElement('span');
                span.className = 'px-2 py-0.5 rounded-full bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300 font-semibold text-xs border border-slate-200 dark:border-slate-700';
                span.textContent = theme;
                themesContainer.appendChild(span);
            });
        }
    }

    // Invariants List
    if (invariantsList) {
        invariantsList.innerHTML = '';
        const invariants = artifact.invariants || [];
        if (invariants.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'text-xs text-slate-500 font-medium italic p-2';
            empty.textContent = 'No compiled invariants.';
            invariantsList.appendChild(empty);
        } else {
            invariants.forEach(inv => {
                invariantsList.appendChild(createInvariantCard(inv));
            });
        }
    }

    // Gaps List
    if (gapsList) {
        gapsList.innerHTML = '';
        const gaps = artifact.gaps || [];
        if (gaps.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'text-xs text-slate-500 font-medium italic p-2';
            empty.textContent = 'No identified gaps.';
            gapsList.appendChild(empty);
        } else {
            gaps.forEach(gap => {
                gapsList.appendChild(createSimpleCard(gap, 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200'));
            });
        }
    }

    // Assumptions List
    if (assumptionsList) {
        assumptionsList.innerHTML = '';
        const assumptions = artifact.assumptions || [];
        if (assumptions.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'text-xs text-slate-500 font-medium italic p-2';
            empty.textContent = 'No compiler assumptions.';
            assumptionsList.appendChild(empty);
        } else {
            assumptions.forEach(asm => {
                assumptionsList.appendChild(createSimpleCard(asm, 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-200'));
            });
        }
    }

    // Excluded Features (exclusions)
    if (exclusionsList) {
        exclusionsList.innerHTML = '';
        const exclusions = artifact.rejected_features || [];
        if (exclusions.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'text-xs text-slate-500 font-medium italic p-2';
            empty.textContent = 'No excluded features.';
            exclusionsList.appendChild(empty);
        } else {
            exclusions.forEach(excl => {
                exclusionsList.appendChild(createSimpleCard(excl, 'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-200'));
            });
        }
    }

    // Eligible Feature Rules
    if (eligibleRulesList) {
        eligibleRulesList.innerHTML = '';
        const eligibleRules = artifact.eligible_feature_rules || [];
        if (eligibleRules.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'text-xs text-slate-500 font-medium italic p-2';
            empty.textContent = 'No eligible feature rules.';
            eligibleRulesList.appendChild(empty);
        } else {
            eligibleRules.forEach(rule => {
                eligibleRulesList.appendChild(createSimpleCard(rule, 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200'));
            });
        }
    }

    // Render Spec Tab
    if (specViewer) {
        const specContent = spec.source_content || spec.excerpt || '';
        specViewer.textContent = specContent;
    }
    if (truncationBanner) {
        if (spec.content_truncated === true) {
            truncationBanner.classList.remove('hidden');
        } else {
            truncationBanner.classList.add('hidden');
        }
    }

    // Render Raw JSON Tab
    if (preview) {
        const previewPayload = {
            project,
            spec: {
                resolved_path: spec.resolved_path,
                spec_hash: spec.spec_hash,
                disk_sha256: spec.disk_sha256,
                content_included: spec.content_included,
                coverage_summary: spec.coverage_summary,
                coverage_diagnostics: spec.coverage_diagnostics,
            },
            pending_authority: pending,
        };
        preview.innerText = JSON.stringify(previewPayload, null, 2);
    }
    
    // Restore/Apply active tab view
    switchAuthorityReviewTab(currentAuthorityReviewActiveTab);
}

function setAuthorityReviewError(message) {
    const errorEl = document.getElementById('authority-review-error');
    if (!errorEl) return;

    if (!message) {
        errorEl.innerText = '';
        errorEl.classList.add('hidden');
        return;
    }

    errorEl.innerText = message;
    errorEl.classList.remove('hidden');
}

function authorityApiErrorMessage(data, fallback) {
    const detail = data?.detail || data;
    const errors = detail?.errors || [];
    if (Array.isArray(errors) && errors.length > 0) {
        const first = errors[0] || {};
        return first.message || first.code || fallback;
    }
    if (typeof detail === 'string') return detail;
    return fallback;
}

function authorityErrorCodes(data) {
    const detail = data?.detail || data;
    const errors = detail?.errors || [];
    if (!Array.isArray(errors)) return [];
    return errors.map((error) => error?.code).filter(Boolean);
}

function isStaleAuthorityError(data) {
    const staleCodes = new Set([
        'AUTHORITY_SOURCE_CHANGED',
        'STALE_AUTHORITY_VERSION',
        'STALE_ARTIFACT_FINGERPRINT',
        'STALE_CONTEXT_FINGERPRINT',
        'STALE_STATE',
    ]);
    return authorityErrorCodes(data).some((code) => staleCodes.has(code));
}

async function handleAuthorityDecisionFailure(data, fallback) {
    if (isStaleAuthorityError(data)) {
        await loadAuthorityReview();
        setAuthorityReviewError('Authority review changed. Reloaded the latest review; review again before deciding.');
        return;
    }
    setAuthorityReviewError(authorityApiErrorMessage(data, fallback));
}

async function loadAuthorityReview() {
    if (!selectedProjectId) return;
    setAuthorityReviewError('');
    renderAuthorityReviewCard(true);

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/authority/review`);
        const data = await response.json();
        if (!response.ok || data.status !== 'success') {
            throw data;
        }
        currentAuthorityReview = data.data;
        renderAuthorityReviewCard(true);
    } catch (error) {
        console.error('Authority review load error:', error);
        setAuthorityReviewError(authorityApiErrorMessage(error, 'Failed to load authority review.'));
    }
}

async function acceptAuthorityReview() {
    if (!selectedProjectId) return;
    const reviewToken = currentAuthorityReview?.guard_tokens?.review_token;
    if (!reviewToken) {
        setAuthorityReviewError('Reload the authority review before accepting.');
        await loadAuthorityReview();
        return;
    }

    const incompleteReviewOverrides = collectIncompleteReviewOverrides();
    if (incompleteReviewOverrides === null) return;

    const btn = document.getElementById('btn-accept-authority');
    const original = btn?.innerHTML;
    if (btn) {
        btn.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">progress_activity</span> Accepting...';
        btn.disabled = true;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/authority/accept`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                review_token: reviewToken,
                incomplete_review_overrides: incompleteReviewOverrides,
            }),
        });
        const data = await response.json();
        if (!response.ok || data.status !== 'success') {
            await handleAuthorityDecisionFailure(data, 'Failed to accept authority.');
            return;
        }
        currentAuthorityReview = null;
        await fetchProjectFSMState(selectedProjectId);
        await loadVisionHistory();
    } catch (error) {
        console.error('Authority accept error:', error);
        await handleAuthorityDecisionFailure(error, 'Network error while accepting authority.');
    } finally {
        if (btn) {
            btn.innerHTML = original || '<span class="material-symbols-outlined text-sm">verified</span> Accept Authority';
            btn.disabled = false;
        }
    }
}

function collectIncompleteReviewOverrides() {
    const findings = currentAuthorityReview?.pending_authority?.review_findings || [];
    const blocking = findings.filter((finding) => (
        finding?.severity === 'blocking'
        && finding?.code !== 'AUTHORITY_COVERAGE_INCOMPLETE'
    ));
    if (!blocking.length) return [];

    const overrides = [];
    for (const finding of blocking) {
        if (finding.override_allowed === false) {
            setAuthorityReviewError(`${finding.code} must be resolved before accepting authority.`);
            return null;
        }
        const candidateIds = Array.isArray(finding.candidate_ids) ? finding.candidate_ids : [];
        for (const candidateId of candidateIds) {
            const rationale = window.prompt(
                `Rationale for overriding ${finding.code} on ${candidateId}:`,
                '',
            );
            if (!rationale || !rationale.trim()) {
                setAuthorityReviewError('Candidate-specific override rationale is required.');
                return null;
            }
            overrides.push({
                candidate_id: String(candidateId),
                finding_code: String(finding.code),
                rationale: rationale.trim(),
            });
        }
    }
    return overrides;
}

async function rejectAuthorityReview() {
    if (!selectedProjectId) return;
    const reviewToken = currentAuthorityReview?.guard_tokens?.review_token;
    if (!reviewToken) {
        setAuthorityReviewError('Reload the authority review before rejecting.');
        await loadAuthorityReview();
        return;
    }

    const reasonInput = document.getElementById('authority-reject-reason');
    const reason = reasonInput?.value?.trim() || '';
    if (!reason) {
        setAuthorityReviewError('Reason is required to reject authority.');
        return;
    }

    const btn = document.getElementById('btn-reject-authority');
    const original = btn?.innerHTML;
    if (btn) {
        btn.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">progress_activity</span> Rejecting...';
        btn.disabled = true;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/authority/reject`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ review_token: reviewToken, reason }),
        });
        const data = await response.json();
        if (!response.ok || data.status !== 'success') {
            await handleAuthorityDecisionFailure(data, 'Failed to reject authority.');
            return;
        }
        currentAuthorityReview = null;
        await fetchProjectFSMState(selectedProjectId);
    } catch (error) {
        console.error('Authority reject error:', error);
        await handleAuthorityDecisionFailure(error, 'Network error while rejecting authority.');
    } finally {
        if (btn) {
            btn.innerHTML = original || '<span class="material-symbols-outlined text-sm">block</span> Reject Authority';
            btn.disabled = false;
        }
    }
}

function setPhaseState(fsmState, desiredViewPhase = null) {
    activeFsmState = normalizeStateKey(fsmState);
    activePhaseId = getPhaseIdForState(activeFsmState);
    viewPhaseId = desiredViewPhase || activePhaseId;

    updateStepperUI(activeFsmState);
    renderPhaseSection();
    updateNextButton();
}

function renderPhaseSection() {
    PANEL_ORDER.forEach((phaseId) => {
        const section = document.getElementById(`phase-section-${phaseId}`);
        if (!section) return;
        if (phaseId === viewPhaseId) section.classList.remove('hidden');
        else section.classList.add('hidden');
    });

    renderOverviewPanel();
    renderSprintSavedWorkspace();
    updateProjectNavUI();
    updateFooterVisibility();
}

function updateFooterVisibility() {
    const footer = document.getElementById('setup-selected-actions');
    if (!footer) return;

    if (shouldHideWorkflowFooter()) {
        footer.classList.add('hidden');
    } else {
        footer.classList.remove('hidden');
    }
}

function isPhaseReady(phaseId) {
    if (phaseId === 'setup') {
        return currentProjectState.setup_status === 'passed' && activeFsmState !== 'SETUP_REQUIRED';
    }

    const activeIndex = phaseIndex(activePhaseId);
    const targetIndex = phaseIndex(phaseId);
    if (activeIndex > targetIndex) {
        return true;
    }

    return (PHASE_TERMINAL_STATES[phaseId] || []).includes(activeFsmState);
}

function getNextButtonModel() {
    if (viewPhaseId === 'overview') {
        return {
            label: 'Project Overview',
            enabled: false,
            hint: 'Use Overview actions or open Sprint to continue.',
        };
    }

    const targetPhase = NEXT_PHASE[viewPhaseId] || null;
    if (!targetPhase) {
        return {
            label: 'Workflow Complete',
            enabled: false,
            hint: 'You reached the final phase.',
        };
    }

    const currentReady = isPhaseReady(viewPhaseId);
    if (!currentReady) {
        if (viewPhaseId === 'setup') {
            return {
                label: `Next: ${capitalizePhase(targetPhase)}`,
                enabled: false,
                hint: 'Setup must pass (valid file path + compiled authority).',
            };
        }
        const required = (PHASE_TERMINAL_STATES[viewPhaseId] || []).join(', ');
        return {
            label: `Next: ${capitalizePhase(targetPhase)}`,
            enabled: false,
            hint: required ? `Current phase must reach ${required}.` : 'Current phase is not ready yet.',
        };
    }

    return {
        label: `Next: ${capitalizePhase(targetPhase)}`,
        enabled: true,
        hint: `Navigate to ${capitalizePhase(targetPhase)} section.`,
    };
}

function updateNextButton() {
    const button = document.getElementById('btn-next-phase');
    const hint = document.getElementById('next-phase-hint');
    if (!button || !hint) return;

    const model = getNextButtonModel();
    // Inner text and HTML must be cleanly inserted with icon
    button.innerHTML = `${model.label} <span class="material-symbols-outlined text-sm">arrow_forward</span>`;
    button.disabled = !model.enabled;
    hint.innerText = model.hint;

    button.className = model.enabled
        ? 'inline-flex items-center gap-2 px-6 py-2.5 rounded-lg bg-primary hover:bg-primary/90 text-white font-bold transition-all shadow-sm'
        : 'inline-flex items-center gap-2 px-6 py-2.5 rounded-lg bg-primary/40 text-white font-bold cursor-not-allowed transition-all shadow-sm';
}

function handleNextPhase() {
    const model = getNextButtonModel();
    if (!model.enabled) return;

    const target = NEXT_PHASE[viewPhaseId];
    if (!target) return;

    if (target === 'sprint') {
        if (isPlanningCompleteState(activeFsmState) && ensureCurrentSprintSelection()) {
            showSprintPlanner = false;
            sprintMode = getSprintMode(getSavedSprintById(currentSprintId));
        } else {
            currentSprintId = null;
            sprintMode = null;
            showSprintPlanner = true;
        }
    } else {
        showSprintPlanner = false;
    }

    viewPhaseId = target;
    renderPhaseSection();
    updateNextButton();

    runAutoLoadForVisiblePhase();
}

function runAutoLoadForVisiblePhase() {
    if (viewPhaseId === 'backlog' && backlogAttemptCount === 0) {
        generateBacklogDraft();
    } else if (viewPhaseId === 'roadmap' && roadmapAttemptCount === 0) {
        generateRoadmapDraft();
    } else if (viewPhaseId === 'story') {
        loadStoryRequirements();
    } else if (viewPhaseId === 'sprint' && (!currentSprintId || showSprintPlanner)) {
        loadSprintCandidates();
    }
}

function isPhaseNavigable(phaseId) {
    if (phaseId === 'setup') return true;
    if (phaseId === 'sprint') {
        return (
            isPhaseReady('story')
            || phaseIndex(activePhaseId) >= phaseIndex('sprint')
            || savedSprints.length > 0
            || isPlanningCompleteState(activeFsmState)
        );
    }

    const targetIndex = phaseIndex(phaseId);
    return targetIndex >= 0 && (targetIndex <= phaseIndex(activePhaseId) || isPhaseReady(phaseId));
}

function navigateToOverview() {
    if (currentProjectState.setup_status === 'failed') return;
    if (!isPlanningCompleteState(activeFsmState) && savedSprints.length === 0) return;

    viewPhaseId = 'overview';
    renderPhaseSection();
    updateNextButton();
}

function handlePhaseNavigation(phaseId) {
    if (!isPhaseNavigable(phaseId)) return;

    if (phaseId === 'sprint') {
        const selectedSprint = ensureCurrentSprintSelection();
        if (selectedSprint) {
            sprintMode = getSprintMode(selectedSprint);
            showSprintPlanner = false;
        } else {
            currentSprintId = null;
            sprintMode = null;
            showSprintPlanner = true;
        }
    } else {
        showSprintPlanner = false;
    }

    viewPhaseId = phaseId;
    renderPhaseSection();
    updateNextButton();
    runAutoLoadForVisiblePhase();
}

function attachPhaseNavigation() {
    document.querySelectorAll('[data-step-id]').forEach((stepEl) => {
        stepEl.addEventListener('click', () => handlePhaseNavigation(stepEl.dataset.stepId));
        stepEl.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                handlePhaseNavigation(stepEl.dataset.stepId);
            }
        });
    });

    document.querySelectorAll('[data-project-nav="overview"]').forEach((button) => {
        button.addEventListener('click', navigateToOverview);
    });
}

function updateProjectNavUI() {
    const showOverviewNav = currentProjectState.setup_status !== 'failed'
        && (isPlanningCompleteState(activeFsmState) || savedSprints.length > 0);
    document.getElementById('desktop-overview-nav')?.classList.toggle('hidden', !showOverviewNav);
    document.getElementById('mobile-overview-nav')?.classList.toggle('hidden', !showOverviewNav);

    document.querySelectorAll('[data-project-nav="overview"]').forEach((button) => {
        const isActive = viewPhaseId === 'overview';
        button.className = isActive
            ? 'w-full inline-flex items-center gap-2 rounded-lg border border-sky-200 bg-sky-50 px-3 py-2 text-sm font-bold text-sky-700 transition-colors dark:border-sky-700 dark:bg-sky-900/30 dark:text-sky-300'
            : 'w-full inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm font-bold text-slate-600 transition-colors hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700';
    });
}

async function fetchProjectFSMState(projectId, options = {}) {
    const { preserveView = false } = options;
    try {
        const response = await fetch(`/api/projects/${projectId}/state`);
        const data = await response.json();

        if (data.status !== 'success') throw new Error('Failed to load state');

        const state = data.data || {};
        currentProjectState = {
            setup_status: state.setup_status || 'failed',
            setup_error: state.setup_error || null,
        };

        const stateKey = normalizeStateKey(state.fsm_state);
        const landing = resolveProjectLanding(stateKey);

        const specInput = document.getElementById('setup-spec-path');
        if (specInput) {
            specInput.value = state.setup_spec_file_path || '';
            // Only unlock edits if setup completely failed
            specInput.readOnly = currentProjectState.setup_status !== 'failed';
            if (!specInput.readOnly) {
                specInput.classList.remove('bg-white', 'dark:bg-slate-900', 'cursor-not-allowed', 'border-slate-300', 'dark:border-slate-600');
                specInput.classList.add('bg-amber-50', 'dark:bg-amber-900/40', 'border-amber-400');
            } else {
                specInput.classList.add('bg-white', 'dark:bg-slate-900', 'cursor-not-allowed', 'border-slate-300', 'dark:border-slate-600');
                specInput.classList.remove('bg-amber-50', 'dark:bg-amber-900/40', 'border-amber-400');
            }
        }

        if (currentProjectState.setup_status === 'failed') {
            currentSprintId = null;
            sprintMode = null;
            showSprintPlanner = false;
            currentAuthorityReview = null;
            setPhaseState('SETUP_REQUIRED', 'setup');
        } else if (currentProjectState.setup_status === 'authority_pending_review') {
            currentSprintId = null;
            sprintMode = null;
            showSprintPlanner = false;
            setPhaseState('SETUP_REQUIRED', 'setup');
            await loadAuthorityReview();
        } else if (currentProjectState.setup_status === 'authority_rejected') {
            currentSprintId = null;
            sprintMode = null;
            showSprintPlanner = false;
            currentAuthorityReview = null;
            setPhaseState('SETUP_REQUIRED', 'setup');
        } else if (preserveView) {
            const selectedSprint = ensureCurrentSprintSelection();
            if (selectedSprint) {
                sprintMode = getSprintMode(selectedSprint);
            }
            setPhaseState(stateKey, viewPhaseId);
        } else {
            applyResolvedLanding(stateKey, landing);
            setTimeout(runAutoLoadForVisiblePhase, 500);
        }

        if (currentProjectState.setup_status === 'passed') {
            await loadAuthorityReview();
        }

        updateSetupStatusBanner();
        updateRetryButton();
        updateNextButton();
    } catch (error) {
        console.error('Error fetching project state:', error);
        currentProjectState = { setup_status: 'failed', setup_error: 'Failed to load state.' };
        currentSprintId = null;
        sprintMode = null;
        showSprintPlanner = false;
        setPhaseState('SETUP_REQUIRED', 'setup');
        updateSetupStatusBanner();
    }
}

async function retryProjectSetup() {
    if (!selectedProjectId) return;

    const specInput = document.getElementById('setup-spec-path');
    const specFilePath = specInput?.value?.trim() || '';

    if (!specFilePath) {
        alert('Please provide a specification file path.');
        return;
    }

    const btn = document.getElementById('btn-retry-setup');
    const original = btn?.innerHTML;
    if (btn) {
        btn.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">refresh</span> Retrying...';
        btn.disabled = true;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/setup/retry`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ spec_file_path: specFilePath }),
        });

        const data = await response.json();
        if (data.status === 'success') {
            await fetchProjectFSMState(selectedProjectId);
            await loadVisionHistory();
        } else {
            alert(data.detail || 'Setup retry failed.');
        }
    } catch (error) {
        console.error('Setup retry error:', error);
        alert('Network error while retrying setup.');
    } finally {
        if (btn) {
            btn.innerHTML = original || '<span class="material-symbols-outlined text-sm">refresh</span> Try Setup Again';
            btn.disabled = false;
        }
    }
}

function updateStepperUI(fsmState) {
    const stateKey = normalizeStateKey(fsmState);
    const steps = getWorkflowSteps();
    const activeStepId = getPhaseIdForState(stateKey);
    let activeIndex = Math.max(0, steps.findIndex((step) => step.id === activeStepId));

    // If the active phase's FSM state is explicitly its terminal/persistence state,
    // the UI should mark it as Completed and move the Active badge to the next phase.
    if ((PHASE_TERMINAL_STATES[activeStepId] || []).includes(stateKey)) {
        activeIndex++;
    }

    steps.forEach((step, index) => {
        const stepEls = document.querySelectorAll(`[data-step-id="${step.id}"]`);
        if (stepEls.length === 0) return;

        stepEls.forEach((stepEl) => {
            const iconContainer = stepEl.querySelector('[data-role="icon"]');
            const labelSpan = stepEl.querySelector('[data-role="label"]');
            const statusSpan = stepEl.querySelector('[data-role="status"]');
            if (!iconContainer || !labelSpan || !statusSpan) return;

            const navigable = isPhaseNavigable(step.id);
            stepEl.tabIndex = navigable ? 0 : -1;
            stepEl.style.cursor = navigable ? 'pointer' : 'default';

            if (index < activeIndex) {
                stepEl.removeAttribute('aria-current');
                iconContainer.className = 'w-10 h-10 rounded-full bg-emerald-500 text-white flex items-center justify-center ring-4 ring-white dark:ring-background-dark shadow-md transition-all';
                iconContainer.innerHTML = '<span class="material-symbols-outlined text-xl">check</span>';
                labelSpan.className = 'text-xs font-bold text-emerald-600 dark:text-emerald-400 transition-colors';
                statusSpan.className = 'text-[10px] text-emerald-500 uppercase font-black transition-colors';
                statusSpan.innerText = 'Completed';
                return;
            }

            if (index === activeIndex) {
                stepEl.setAttribute('aria-current', 'step');
                const icon = STEP_ICONS[step.id] || 'play_circle';
                iconContainer.className = 'w-10 h-10 rounded-full bg-primary text-white flex items-center justify-center ring-4 ring-white dark:ring-background-dark shadow-md transition-all';
                iconContainer.innerHTML = `<span class="material-symbols-outlined text-xl">${icon}</span>`;
                labelSpan.className = 'text-xs font-bold text-primary transition-colors';
                statusSpan.className = 'text-[10px] text-primary/80 uppercase font-black transition-colors';
                statusSpan.innerText = 'Active';
                return;
            }

            stepEl.removeAttribute('aria-current');
            iconContainer.className = 'w-10 h-10 rounded-full bg-slate-200 dark:bg-slate-700 text-slate-500 dark:text-slate-400 flex items-center justify-center ring-4 ring-white dark:ring-background-dark transition-all';
            iconContainer.innerHTML = '<span class="material-symbols-outlined text-xl">lock</span>';
            labelSpan.className = 'text-xs font-medium text-slate-500 transition-colors';
            statusSpan.className = 'text-[10px] text-slate-400 uppercase font-black transition-colors';
            statusSpan.innerText = 'Locked';
        });
    });
}

function renderVisionArtifactHtml(artifact) {
    if (!artifact) return '<div class="text-xs text-slate-500">No vision run yet.</div>';

    const uc = artifact.updated_components || {};
    const stmt = artifact.product_vision_statement || 'No statement drafted.';
    const isComplete = Boolean(artifact.is_complete);
    const questions = Array.isArray(artifact.clarifying_questions) ? artifact.clarifying_questions : [];

    const statusColor = isComplete ? 'text-emerald-700 bg-emerald-50 border-emerald-200 dark:text-emerald-400 dark:bg-emerald-900/30 dark:border-emerald-800' : 'text-rose-700 bg-rose-50 border-rose-200 dark:text-rose-400 dark:bg-rose-900/30 dark:border-rose-800';
    const statusIcon = isComplete ? 'check_circle' : 'error';
    const statusText = isComplete ? 'Complete' : 'Incomplete - Needs Refinement';

    let html = `
        <div class="space-y-5">
            <!-- Status Pill -->
            <div class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border ${statusColor} text-xs font-bold">
                <span class="material-symbols-outlined text-[14px]">${statusIcon}</span>
                ${statusText}
            </div>

            <!-- Vision Statement -->
            <div class="space-y-1.5 border-l-2 border-primary/40 pl-3">
                <h6 class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Vision Statement drafted</h6>
                <div class="text-sm text-slate-800 dark:text-slate-200 leading-relaxed italic">
                    "${stmt}"
                </div>
            </div>

            <!-- Clarifying Questions -->
            ${!isComplete && questions.length > 0 ? `
            <div class="space-y-2">
                <h6 class="text-[10px] font-bold uppercase tracking-wider text-amber-600 dark:text-amber-500 flex items-center gap-1"><span class="material-symbols-outlined text-[14px]">help</span> Missing Context / Questions</h6>
                <ul class="text-xs text-slate-700 dark:text-slate-300 space-y-1.5 list-disc list-inside bg-amber-50 dark:bg-amber-900/20 p-3 rounded-lg border border-amber-200 dark:border-amber-800">
                    ${questions.map(q => `<li>${q}</li>`).join('')}
                </ul>
            </div>
            ` : ''}

            <!-- Granular Components -->
            <div class="space-y-3 pt-2 border-t border-slate-100 dark:border-slate-800">
                <h6 class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Components Dictionary</h6>
                <div class="grid grid-cols-1 gap-2.5">
    `;

    const labels = {
        project_name: 'Project Name',
        target_user: 'Target User',
        problem: 'Problem',
        product_category: 'Category',
        key_benefit: 'Key Benefit',
        competitors: 'Competitors',
        differentiator: 'Differentiator'
    };

    for (const [key, label] of Object.entries(labels)) {
        const val = uc[key];
        const isSet = val && val !== 'null' && val !== '/UNKNOWN' && String(val).trim() !== '';
        const badgeColor = isSet ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-400' : 'bg-rose-100 text-rose-700 dark:bg-rose-900/50 dark:text-rose-400';
        const badgeIcon = isSet ? 'check' : 'close';
        const displayVal = isSet ? val : 'Not defined yet...';

        html += `
            <div class="flex items-start gap-2.5 text-xs">
                <div class="mt-0.5 shrink-0 w-4 h-4 rounded-full flex items-center justify-center ${badgeColor}">
                    <span class="material-symbols-outlined text-[10px] font-bold">${badgeIcon}</span>
                </div>
                <div class="flex-1">
                    <span class="font-bold text-slate-600 dark:text-slate-400">${label}:</span> 
                    <span class="${isSet ? 'text-slate-800 dark:text-slate-200 font-medium' : 'text-slate-400 italic'}">${displayVal}</span>
                </div>
            </div>
        `;
    }

    html += `
                </div>
            </div>
        </div>
    `;

    return html;
}

function renderVisionAttemptPanels(inputContext, outputArtifact) {
    const inputEl = document.getElementById('vision-input-context');
    const outputEl = document.getElementById('vision-output-artifact');
    const copyBtn = document.getElementById('btn-copy-vision-output');

    if (inputEl) {
        inputEl.innerText = inputContext ? JSON.stringify(inputContext, null, 2) : 'No vision run yet.';
    }

    if (outputEl) {
        outputEl.innerHTML = renderVisionArtifactHtml(outputArtifact);
    }
    
    currentVisionArtifactJSON = outputArtifact || null;
    if (copyBtn) {
        if (currentVisionArtifactJSON) {
            copyBtn.classList.remove('hidden');
        } else {
            copyBtn.classList.add('hidden');
        }
    }
}

function renderVisionHistory(items) {
    const container = document.getElementById('vision-history-list');
    if (!container) return;

    container.innerHTML = '';

    if (!items || items.length === 0) {
        container.innerHTML = '<p class="text-xs text-slate-500">No attempts yet.</p>';
        return;
    }

    const reversed = [...items].reverse();
    reversed.forEach((item, index) => {
        const stamp = item.created_at || '-';
        const state = item.is_complete ? 'Complete' : 'Needs input';
        const color = item.is_complete ? 'text-emerald-600 bg-emerald-50 dark:bg-emerald-900/30 ring-emerald-200' : 'text-amber-600 bg-amber-50 dark:bg-amber-900/30 ring-amber-200';
        const trigger = item.trigger === 'auto_setup_transition' ? 'Auto setup' : 'Manual refine';

        const row = document.createElement('div');
        row.className = 'border border-slate-200 dark:border-slate-700 rounded-lg p-3 bg-slate-50 dark:bg-slate-800/60 transition-transform';
        row.innerHTML = `
            <div class="flex items-center justify-between">
                <span class="text-xs font-extrabold text-slate-700 dark:text-slate-300">Attempt ${items.length - index}</span>
                <span class="text-[10px] uppercase ${color} px-2 py-0.5 rounded-full ring-1 ring-inset font-bold">${state}</span>
            </div>
            <p class="text-[11px] font-semibold text-slate-500 mt-2">Trigger: <span class="text-slate-700 dark:text-slate-300 font-bold">${trigger}</span></p>
            <p class="text-[10px] text-slate-400 mt-1">${stamp}</p>
        `;
        container.appendChild(row);
    });
}

function updateVisionSaveButton() {
    const button = document.getElementById('btn-save-vision');
    const hint = document.getElementById('vision-save-hint');
    if (!button || !hint) return;

    const canSave = Boolean(selectedProjectId) && latestVisionIsComplete;
    button.disabled = !canSave;
    button.className = canSave
        ? 'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-bold transition-all shadow-sm'
        : 'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-primary/40 text-white font-bold cursor-not-allowed transition-all';

    hint.innerText = canSave
        ? 'Vision is complete. Proceed to save and advance to Backlog.'
        : 'Save is disabled until latest Vision output has is_complete=true.';
}

async function loadVisionHistory() {
    if (!selectedProjectId) {
        visionAttemptCount = 0;
        latestVisionIsComplete = false;
        renderVisionHistory([]);
        return;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/vision/history`);
        const data = await response.json();
        if (data.status !== 'success') {
            renderVisionHistory([]);
            return;
        }

        const items = Array.isArray(data.data?.items) ? data.data.items : [];
        visionAttemptCount = items.length;
        renderVisionHistory(items);

        if (items.length > 0) {
            const latest = items[items.length - 1];
            latestVisionIsComplete = Boolean(latest.is_complete);
            renderVisionAttemptPanels(latest.input_context || null, latest.output_artifact || null);
        } else {
            latestVisionIsComplete = false;
            renderVisionAttemptPanels(null, null);
        }

        updateVisionSaveButton();
    } catch (error) {
        console.error('Failed to load vision history:', error);
        visionAttemptCount = 0;
        latestVisionIsComplete = false;
        renderVisionHistory([]);
    }
}

async function generateVisionDraft() {
    if (!selectedProjectId) {
        alert('Select a project first.');
        return;
    }

    const input = document.getElementById('vision-user-input');
    const userInput = input?.value?.trim() || '';
    if (!userInput && visionAttemptCount === 0) {
        await loadVisionHistory();
    }
    if (visionAttemptCount > 0 && !userInput) {
        alert('Please provide feedback to refine Vision.');
        return;
    }

    const button = document.getElementById('btn-generate-vision');
    const original = button?.innerHTML;
    if (button) {
        button.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">cycle</span> Running...';
        button.disabled = true;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/vision/generate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_input: userInput }),
        });

        if (response.status >= 400) {
            const errorBody = await response.json();
            throw new Error(errorBody.detail || 'Vision generation failed');
        }

        const data = await response.json();
        if (data.status !== 'success') {
            throw new Error('Vision generation failed');
        }

        latestVisionIsComplete = Boolean(data.data?.is_complete);
        renderVisionAttemptPanels(data.data?.input_context || null, data.data?.output_artifact || null);
        setPhaseState(data.data?.fsm_state || 'VISION_INTERVIEW', 'vision');

        await loadVisionHistory();
    } catch (error) {
        console.error(error);
        alert(error.message || 'Vision generation failed.');
    } finally {
        if (button) {
            button.innerHTML = original || '<span class="material-symbols-outlined text-sm">cycle</span> Generate / Refine';
            button.disabled = false;
        }
        updateVisionSaveButton();
    }
}

async function saveVisionDraft() {
    if (!selectedProjectId) {
        alert('Select a project first.');
        return;
    }

    const button = document.getElementById('btn-save-vision');
    const original = button?.innerHTML;
    if (button) {
        button.innerHTML = '<span class="material-symbols-outlined text-sm">save</span> Saving...';
        button.disabled = true;
    }

    let success = false;
    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/vision/save`, {
            method: 'POST',
        });

        if (response.status === 409) {
            const body = await response.json();
            throw new Error(body.detail || 'Vision is not complete yet.');
        }
        if (response.status >= 400) {
            throw new Error('Failed to save vision.');
        }

        const data = await response.json();
        if (data.status !== 'success') {
            throw new Error('Failed to save vision.');
        }

        setPhaseState('VISION_PERSISTENCE', 'vision');
        latestVisionIsComplete = true;
        success = true;

        await fetchProjectFSMState(selectedProjectId, { preserveView: true });

        if (button) {
            button.innerHTML = '<span class="material-symbols-outlined text-sm">check_circle</span> Saved Successfully!';
            button.className = 'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-emerald-500 text-white font-bold transition-all shadow-md scale-105 ring-2 ring-emerald-200';
            setTimeout(() => {
                updateVisionSaveButton();
            }, 3000);
        }

        const nextBtn = document.getElementById('btn-next-phase');
        if (nextBtn) {
            nextBtn.classList.add('ring-4', 'ring-primary/40', 'scale-105');
            setTimeout(() => {
                nextBtn.classList.remove('ring-4', 'ring-primary/40', 'scale-105');
            }, 3000);
        }

    } catch (error) {
        console.error(error);
        alert(error.message || 'Failed to save vision.');
    } finally {
        if (!success) {
            if (button) button.innerHTML = original || '<span class="material-symbols-outlined text-sm">save</span> Save Vision';
            updateVisionSaveButton();
        }
    }
}

// --- BACKLOG LOGIC ---

function renderBacklogArtifactHtml(artifact) {
    if (!artifact) return '<div class="text-[11px] text-slate-500 text-center mt-10">No backlog generated yet.</div>';

    const items = Array.isArray(artifact.backlog_items) ? artifact.backlog_items : [];
    const isComplete = Boolean(artifact.is_complete);
    const questions = Array.isArray(artifact.clarifying_questions) ? artifact.clarifying_questions : [];

    const statusColor = isComplete ? 'text-emerald-700 bg-emerald-50 border-emerald-200 dark:text-emerald-400 dark:bg-emerald-900/30 dark:border-emerald-800' : 'text-rose-700 bg-rose-50 border-rose-200 dark:text-rose-400 dark:bg-rose-900/30 dark:border-rose-800';
    const statusIcon = isComplete ? 'check_circle' : 'error';
    const statusText = isComplete ? 'Complete' : 'Incomplete - Needs Refinement';

    let html = `
        <div class="space-y-5">
            <!-- Status Pill -->
            <div class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border ${statusColor} text-xs font-bold">
                <span class="material-symbols-outlined text-[14px]">${statusIcon}</span>
                ${statusText}
            </div>

            <!-- Clarifying Questions -->
            ${!isComplete && questions.length > 0 ? `
            <div class="space-y-2">
                <h6 class="text-[10px] font-bold uppercase tracking-wider text-amber-600 dark:text-amber-500 flex items-center gap-1"><span class="material-symbols-outlined text-[14px]">help</span> Missing Context / Questions</h6>
                <ul class="text-xs text-slate-700 dark:text-slate-300 space-y-1.5 list-disc list-inside bg-amber-50 dark:bg-amber-900/20 p-3 rounded-lg border border-amber-200 dark:border-amber-800">
                    ${questions.map(q => `<li>${q}</li>`).join('')}
                </ul>
            </div>
            ` : ''}

            <!-- Items list -->
            <div class="space-y-3 pt-2">
                <h6 class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Prioritized Items (${items.length})</h6>
                <div class="space-y-3">
    `;

    if (items.length === 0) {
        html += `<div class="text-[11px] italic text-slate-400">No items available.</div>`;
    }

    items.forEach((item) => {
        let sizeColor = 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400';
        if (item.estimated_effort === 'S') sizeColor = 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-400';
        if (item.estimated_effort === 'M') sizeColor = 'bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-400';
        if (item.estimated_effort === 'L') sizeColor = 'bg-orange-100 text-orange-700 dark:bg-orange-900/50 dark:text-orange-400';
        if (item.estimated_effort === 'XL') sizeColor = 'bg-rose-100 text-rose-700 dark:bg-rose-900/50 dark:text-rose-400';

        html += `
            <div class="border border-slate-200 dark:border-slate-700 rounded-lg p-3 bg-white dark:bg-slate-800 overflow-hidden relative">
                <div class="absolute left-0 top-0 bottom-0 w-1 bg-slate-300 dark:bg-slate-600"></div>
                <div class="flex items-start justify-between gap-3 ml-2">
                    <div class="flex-1">
                        <div class="flex items-center gap-2 mb-1">
                            <span class="text-[10px] font-black bg-slate-100 text-slate-500 dark:bg-slate-700 dark:text-slate-400 px-1.5 py-0.5 rounded">#${item.priority || '?'}</span>
                            <span class="text-[10px] font-bold uppercase text-indigo-500 dark:text-indigo-400">${item.value_driver || 'Unknown'}</span>
                        </div>
                        <h4 class="text-sm font-bold text-slate-800 dark:text-slate-200">${item.requirement || 'Untitled'}</h4>
                        <p class="text-[11px] text-slate-500 dark:text-slate-400 mt-1.5 leading-relaxed">${item.justification || ''}</p>
                    </div>
                    <div class="shrink-0 text-center">
                        <div class="text-[10px] uppercase font-bold text-slate-400 mb-0.5">Size</div>
                        <div class="px-2 py-1 rounded text-xs font-black ${sizeColor}">${item.estimated_effort || '?'}</div>
                    </div>
                </div>
            </div>
        `;
    });

    html += `
                </div>
            </div>
        </div>
    `;

    return html;
}

function renderBacklogAttemptPanels(inputContext, outputArtifact) {
    const inputEl = document.getElementById('backlog-input-context');
    const outputEl = document.getElementById('backlog-output-artifact');
    const copyBtn = document.getElementById('btn-copy-backlog-output');

    if (inputEl) {
        inputEl.innerText = inputContext ? JSON.stringify(inputContext, null, 2) : 'No backlog run yet.';
    }

    if (outputEl) {
        outputEl.innerHTML = renderBacklogArtifactHtml(outputArtifact);
    }
    
    currentBacklogArtifactJSON = outputArtifact || null;
    if (copyBtn) {
        if (currentBacklogArtifactJSON) {
            copyBtn.classList.remove('hidden');
        } else {
            copyBtn.classList.add('hidden');
        }
    }
}

function renderBacklogHistory(items) {
    const container = document.getElementById('backlog-history-list');
    if (!container) return;

    container.innerHTML = '';

    if (!items || items.length === 0) {
        container.innerHTML = '<p class="text-xs text-slate-500">No attempts yet.</p>';
        return;
    }

    const reversed = [...items].reverse();
    reversed.forEach((item, index) => {
        const stamp = item.created_at || '-';
        const state = item.is_complete ? 'Complete' : 'Needs input';
        const color = item.is_complete ? 'text-emerald-600 bg-emerald-50 dark:bg-emerald-900/30 ring-emerald-200' : 'text-amber-600 bg-amber-50 dark:bg-amber-900/30 ring-amber-200';
        const trigger = item.trigger === 'auto_transition' ? 'Auto setup' : 'Manual refine';

        const row = document.createElement('div');
        row.className = 'border border-slate-200 dark:border-slate-700 rounded-lg p-3 bg-slate-50 dark:bg-slate-800/60 transition-transform';
        row.innerHTML = `
            <div class="flex items-center justify-between">
                <span class="text-xs font-extrabold text-slate-700 dark:text-slate-300">Attempt ${items.length - index}</span>
                <span class="text-[10px] uppercase ${color} px-2 py-0.5 rounded-full ring-1 ring-inset font-bold">${state}</span>
            </div>
            <p class="text-[11px] font-semibold text-slate-500 mt-2">Trigger: <span class="text-slate-700 dark:text-slate-300 font-bold">${trigger}</span></p>
            <p class="text-[10px] text-slate-400 mt-1">${stamp}</p>
        `;
        container.appendChild(row);
    });
}

function updateBacklogSaveButton() {
    const button = document.getElementById('btn-save-backlog');
    const hint = document.getElementById('backlog-save-hint');
    if (!button || !hint) return;

    const canSave = Boolean(selectedProjectId) && latestBacklogIsComplete;
    button.disabled = !canSave;
    button.className = canSave
        ? 'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-bold transition-all shadow-sm'
        : 'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-primary/40 text-white font-bold cursor-not-allowed transition-all';

    hint.innerText = canSave
        ? 'Backlog is complete. Proceed to save and advance to Roadmap.'
        : 'Save is disabled until latest Backlog output has is_complete=true.';
}

async function loadBacklogHistory() {
    if (!selectedProjectId) {
        backlogAttemptCount = 0;
        latestBacklogIsComplete = false;
        renderBacklogHistory([]);
        return;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/backlog/history`);
        const data = await response.json();
        if (data.status !== 'success') {
            renderBacklogHistory([]);
            return;
        }

        const items = Array.isArray(data.data?.items) ? data.data.items : [];
        backlogAttemptCount = items.length;
        renderBacklogHistory(items);

        if (items.length > 0) {
            const latest = items[items.length - 1];
            latestBacklogIsComplete = Boolean(latest.is_complete);
            renderBacklogAttemptPanels(latest.input_context || null, latest.output_artifact || null);
        } else {
            latestBacklogIsComplete = false;
            renderBacklogAttemptPanels(null, null);
        }

        updateBacklogSaveButton();
    } catch (error) {
        console.error('Failed to load backlog history:', error);
        backlogAttemptCount = 0;
        latestBacklogIsComplete = false;
        renderBacklogHistory([]);
    }
}

async function generateBacklogDraft() {
    if (!selectedProjectId) {
        alert('Select a project first.');
        return;
    }

    const input = document.getElementById('backlog-user-input');
    const userInput = input?.value?.trim() || '';
    if (backlogAttemptCount > 0 && !userInput) {
        alert('Please provide feedback to refine Backlog.');
        return;
    }

    const button = document.getElementById('btn-generate-backlog');
    const original = button?.innerHTML;
    if (button) {
        button.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">cycle</span> Running...';
        button.disabled = true;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/backlog/generate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_input: userInput }),
        });

        if (response.status >= 400) {
            const errorBody = await response.json();
            throw new Error(errorBody.detail || 'Backlog generation failed');
        }

        const data = await response.json();
        if (data.status !== 'success') {
            throw new Error('Backlog generation failed');
        }

        latestBacklogIsComplete = Boolean(data.data?.is_complete);
        renderBacklogAttemptPanels(data.data?.input_context || null, data.data?.output_artifact || null);
        setPhaseState(data.data?.fsm_state || 'BACKLOG_INTERVIEW', 'backlog');

        await loadBacklogHistory();
    } catch (error) {
        console.error(error);
        alert(error.message || 'Backlog generation failed.');
    } finally {
        if (button) {
            button.innerHTML = original || '<span class="material-symbols-outlined text-sm">cycle</span> Generate / Refine';
            button.disabled = false;
        }
        updateBacklogSaveButton();
    }
}

async function saveBacklogDraft() {
    if (!selectedProjectId) {
        alert('Select a project first.');
        return;
    }

    const button = document.getElementById('btn-save-backlog');
    const original = button?.innerHTML;
    if (button) {
        button.innerHTML = '<span class="material-symbols-outlined text-sm">save</span> Saving...';
        button.disabled = true;
    }

    let success = false;
    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/backlog/save`, {
            method: 'POST',
        });

        if (response.status === 409) {
            const body = await response.json();
            throw new Error(body.detail || 'Backlog is not complete yet.');
        }
        if (response.status >= 400) {
            throw new Error('Failed to save backlog.');
        }

        const data = await response.json();
        if (data.status !== 'success') {
            throw new Error('Failed to save backlog.');
        }

        setPhaseState('BACKLOG_PERSISTENCE', 'backlog');
        latestBacklogIsComplete = true;
        success = true;

        await fetchProjectFSMState(selectedProjectId, { preserveView: true });

        if (button) {
            button.innerHTML = '<span class="material-symbols-outlined text-sm">check_circle</span> Saved Successfully!';
            button.className = 'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-emerald-500 text-white font-bold transition-all shadow-md scale-105 ring-2 ring-emerald-200';
            setTimeout(() => {
                updateBacklogSaveButton();
            }, 3000);
        }

        const nextBtn = document.getElementById('btn-next-phase');
        if (nextBtn) {
            nextBtn.classList.add('ring-4', 'ring-primary/40', 'scale-105');
            setTimeout(() => {
                nextBtn.classList.remove('ring-4', 'ring-primary/40', 'scale-105');
            }, 3000);
        }

    } catch (error) {
        console.error(error);
        alert(error.message || 'Failed to save backlog.');
    } finally {
        if (!success) {
            if (button) button.innerHTML = original || '<span class="material-symbols-outlined text-sm">save</span> Save Backlog';
            updateBacklogSaveButton();
        }
    }
}


// --- ROADMAP LOGIC ---

function renderRoadmapArtifactHtml(artifact) {
    if (!artifact) return '<div class="text-[11px] text-slate-500 text-center mt-10">No roadmap generated yet.</div>';

    const items = Array.isArray(artifact.roadmap_releases) ? artifact.roadmap_releases : [];
    const isComplete = Boolean(artifact.is_complete);
    const questions = Array.isArray(artifact.clarifying_questions) ? artifact.clarifying_questions : [];

    const statusColor = isComplete ? 'text-emerald-700 bg-emerald-50 border-emerald-200 dark:text-emerald-400 dark:bg-emerald-900/30 dark:border-emerald-800' : 'text-rose-700 bg-rose-50 border-rose-200 dark:text-rose-400 dark:bg-rose-900/30 dark:border-rose-800';
    const statusIcon = isComplete ? 'check_circle' : 'error';
    const statusText = isComplete ? 'Complete' : 'Incomplete - Needs Refinement';

    let html = `
        <div class="space-y-5">
            <!-- Status Pill -->
            <div class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border ${statusColor} text-xs font-bold">
                <span class="material-symbols-outlined text-[14px]">${statusIcon}</span>
                ${statusText}
            </div>

            <!-- Roadmap Summary -->
            <div class="space-y-1.5 border-l-2 border-purple-400/40 pl-3">
                <h6 class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Roadmap Overview</h6>
                <div class="text-sm text-slate-800 dark:text-slate-200 leading-relaxed italic">
                    "${artifact.roadmap_summary || 'No summary provided.'}"
                </div>
            </div>

            <!-- Clarifying Questions -->
            ${!isComplete && questions.length > 0 ? `
            <div class="space-y-2">
                <h6 class="text-[10px] font-bold uppercase tracking-wider text-amber-600 dark:text-amber-500 flex items-center gap-1"><span class="material-symbols-outlined text-[14px]">help</span> Missing Context / Questions</h6>
                <ul class="text-xs text-slate-700 dark:text-slate-300 space-y-1.5 list-disc list-inside bg-amber-50 dark:bg-amber-900/20 p-3 rounded-lg border border-amber-200 dark:border-amber-800">
                    ${questions.map(q => `<li>${q}</li>`).join('')}
                </ul>
            </div>
            ` : ''}

            <!-- Items list -->
            <div class="space-y-3 pt-2">
                <h6 class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Releases & Milestones (${items.length})</h6>
                <div class="space-y-4">
    `;

    if (items.length === 0) {
        html += `<div class="text-[11px] italic text-slate-400">No releases available.</div>`;
    }

    items.forEach((item, index) => {
        const assignedCount = Array.isArray(item.items) ? item.items.length : 0;

        html += `
            <div class="border border-slate-200 dark:border-slate-700 rounded-lg bg-white dark:bg-slate-800 overflow-hidden relative shadow-sm">
                <div class="bg-slate-50 dark:bg-slate-800/80 px-4 py-3 border-b border-slate-100 dark:border-slate-700 flex justify-between items-center">
                    <div class="flex items-center gap-2">
                        <span class="text-[10px] font-black bg-purple-100 text-purple-600 dark:bg-purple-900/40 dark:text-purple-400 px-2 py-0.5 rounded-full uppercase">Release ${index + 1}</span>
                        <h4 class="text-sm font-bold text-slate-800 dark:text-slate-200">${item.release_name || 'Untitled Release'}</h4>
                    </div>
                </div>
                <div class="p-4 space-y-3">
                    <div>
                        <div class="text-[10px] uppercase font-bold text-slate-400 mb-0.5">Focus Area</div>
                        <p class="text-[11px] text-slate-700 dark:text-slate-300 font-medium">${item.focus_area || 'None specified'}</p>
                    </div>
                    <div>
                        <div class="text-[10px] uppercase font-bold text-slate-400 mb-0.5">Theme</div>
                        <p class="text-[11px] text-slate-700 dark:text-slate-300 font-medium">${item.theme || 'None specified'}</p>
                    </div>
                    <div>
                        <div class="text-[10px] uppercase font-bold text-slate-400 mb-0.5">Reasoning</div>
                        <p class="text-[11px] text-slate-700 dark:text-slate-300 font-medium">${item.reasoning || 'None specified'}</p>
                    </div>
                    
                    <div class="pt-2">
                        <div class="flex items-center gap-2 mb-2">
                            <span class="text-[10px] uppercase font-bold text-slate-400">Assigned Backlog Items</span>
                            <span class="bg-slate-100 text-slate-500 text-[9px] font-black px-1.5 py-0.5 rounded-full">${assignedCount}</span>
                        </div>
                        <ul class="text-[11px] text-slate-600 dark:text-slate-400 list-disc list-inside space-y-1">
                            ${(item.items || []).map(bItem => `<li><span class="font-medium text-slate-700 dark:text-slate-300">${bItem}</span></li>`).join('')}
                        </ul>
                    </div>
                </div>
            </div>
        `;
    });

    html += `
                </div>
            </div>
        </div>
    `;

    return html;
}

function renderRoadmapAttemptPanels(inputContext, outputArtifact) {
    const inputEl = document.getElementById('roadmap-input-context');
    const outputEl = document.getElementById('roadmap-output-artifact');
    const copyBtn = document.getElementById('btn-copy-roadmap-output');

    if (inputEl) {
        inputEl.innerText = inputContext ? JSON.stringify(inputContext, null, 2) : 'No roadmap run yet.';
    }

    if (outputEl) {
        outputEl.innerHTML = renderRoadmapArtifactHtml(outputArtifact);
    }
    
    currentRoadmapArtifactJSON = outputArtifact || null;
    if (copyBtn) {
        if (currentRoadmapArtifactJSON) {
            copyBtn.classList.remove('hidden');
        } else {
            copyBtn.classList.add('hidden');
        }
    }
}

function renderRoadmapHistory(items) {
    const container = document.getElementById('roadmap-history-list');
    if (!container) return;

    container.innerHTML = '';

    if (!items || items.length === 0) {
        container.innerHTML = '<p class="text-xs text-slate-500">No attempts yet.</p>';
        return;
    }

    const reversed = [...items].reverse();
    reversed.forEach((item, index) => {
        const stamp = item.created_at || '-';
        const state = item.is_complete ? 'Complete' : 'Needs input';
        const color = item.is_complete ? 'text-emerald-600 bg-emerald-50 dark:bg-emerald-900/30 ring-emerald-200' : 'text-amber-600 bg-amber-50 dark:bg-amber-900/30 ring-amber-200';
        const trigger = item.trigger === 'auto_transition' ? 'Auto setup' : 'Manual refine';

        const row = document.createElement('div');
        row.className = 'border border-slate-200 dark:border-slate-700 rounded-lg p-3 bg-slate-50 dark:bg-slate-800/60 transition-transform';
        row.innerHTML = `
            <div class="flex items-center justify-between">
                <span class="text-xs font-extrabold text-slate-700 dark:text-slate-300">Attempt ${items.length - index}</span>
                <span class="text-[10px] uppercase ${color} px-2 py-0.5 rounded-full ring-1 ring-inset font-bold">${state}</span>
            </div>
            <p class="text-[11px] font-semibold text-slate-500 mt-2">Trigger: <span class="text-slate-700 dark:text-slate-300 font-bold">${trigger}</span></p>
            <p class="text-[10px] text-slate-400 mt-1">${stamp}</p>
        `;
        container.appendChild(row);
    });
}

function updateRoadmapSaveButton() {
    const button = document.getElementById('btn-save-roadmap');
    const hint = document.getElementById('roadmap-save-hint');
    if (!button || !hint) return;

    const canSave = Boolean(selectedProjectId) && latestRoadmapIsComplete;
    button.disabled = !canSave;
    button.className = canSave
        ? 'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-bold transition-all shadow-sm'
        : 'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-primary/40 text-white font-bold cursor-not-allowed transition-all';

    hint.innerText = canSave
        ? 'Roadmap is complete. Proceed to save and advance to Stories.'
        : 'Save is disabled until latest Roadmap output has is_complete=true.';
}

async function loadRoadmapHistory() {
    if (!selectedProjectId) {
        roadmapAttemptCount = 0;
        latestRoadmapIsComplete = false;
        renderRoadmapHistory([]);
        return;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/roadmap/history`);
        const data = await response.json();
        if (data.status !== 'success') {
            renderRoadmapHistory([]);
            return;
        }

        const items = Array.isArray(data.data?.items) ? data.data.items : [];
        roadmapAttemptCount = items.length;
        renderRoadmapHistory(items);

        if (items.length > 0) {
            const latest = items[items.length - 1];
            latestRoadmapIsComplete = Boolean(latest.is_complete);
            renderRoadmapAttemptPanels(latest.input_context || null, latest.output_artifact || null);
        } else {
            latestRoadmapIsComplete = false;
            renderRoadmapAttemptPanels(null, null);
        }

        updateRoadmapSaveButton();
    } catch (error) {
        console.error('Failed to load roadmap history:', error);
        roadmapAttemptCount = 0;
        latestRoadmapIsComplete = false;
        renderRoadmapHistory([]);
    }
}

async function generateRoadmapDraft() {
    if (!selectedProjectId) {
        alert('Select a project first.');
        return;
    }

    const input = document.getElementById('roadmap-user-input');
    const userInput = input?.value?.trim() || '';
    if (roadmapAttemptCount > 0 && !userInput) {
        alert('Please provide feedback to refine the Roadmap.');
        return;
    }

    const button = document.getElementById('btn-generate-roadmap');
    const original = button?.innerHTML;
    if (button) {
        button.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">cycle</span> Running...';
        button.disabled = true;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/roadmap/generate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_input: userInput }),
        });

        if (response.status >= 400) {
            const errorBody = await response.json();
            throw new Error(errorBody.detail || 'Roadmap generation failed');
        }

        const data = await response.json();
        if (data.status !== 'success') {
            throw new Error('Roadmap generation failed');
        }

        latestRoadmapIsComplete = Boolean(data.data?.is_complete);
        renderRoadmapAttemptPanels(data.data?.input_context || null, data.data?.output_artifact || null);
        setPhaseState(data.data?.fsm_state || 'ROADMAP_INTERVIEW', 'roadmap');

        await loadRoadmapHistory();
    } catch (error) {
        console.error(error);
        alert(error.message || 'Roadmap generation failed.');
    } finally {
        if (button) {
            button.innerHTML = original || '<span class="material-symbols-outlined text-sm">cycle</span> Generate / Refine';
            button.disabled = false;
        }
        updateRoadmapSaveButton();
    }
}

async function saveRoadmapDraft() {
    if (!selectedProjectId) {
        alert('Select a project first.');
        return;
    }

    const button = document.getElementById('btn-save-roadmap');
    const original = button?.innerHTML;
    if (button) {
        button.innerHTML = '<span class="material-symbols-outlined text-sm">save</span> Saving...';
        button.disabled = true;
    }

    let success = false;
    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/roadmap/save`, {
            method: 'POST',
        });

        if (response.status === 409) {
            const body = await response.json();
            throw new Error(body.detail || 'Roadmap is not complete yet.');
        }
        if (response.status >= 400) {
            throw new Error('Failed to save roadmap.');
        }

        const data = await response.json();
        if (data.status !== 'success') {
            throw new Error('Failed to save roadmap.');
        }

        setPhaseState('ROADMAP_PERSISTENCE', 'roadmap');
        latestRoadmapIsComplete = true;
        success = true;

        await fetchProjectFSMState(selectedProjectId, { preserveView: true });

        if (button) {
            button.innerHTML = '<span class="material-symbols-outlined text-sm">check_circle</span> Saved Successfully!';
            button.className = 'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-emerald-500 text-white font-bold transition-all shadow-md scale-105 ring-2 ring-emerald-200';
            setTimeout(() => {
                updateRoadmapSaveButton();
            }, 3000);
        }

        const nextBtn = document.getElementById('btn-next-phase');
        if (nextBtn) {
            nextBtn.classList.add('ring-4', 'ring-primary/40', 'scale-105');
            setTimeout(() => {
                nextBtn.classList.remove('ring-4', 'ring-primary/40', 'scale-105');
            }, 3000);
        }

    } catch (error) {
        console.error(error);
        alert(error.message || 'Failed to save roadmap.');
    } finally {
        if (!success) {
            if (button) button.innerHTML = original || '<span class="material-symbols-outlined text-sm">save</span> Save Roadmap';
            updateRoadmapSaveButton();
        }
    }
}

// ==========================================
// STORY PHASE LOGIC
// ==========================================

let storyGroups = []; // Array of grouped milestone objects

async function loadStoryRequirements() {
    if (!selectedProjectId) return;

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/story/pending`);
        const data = await response.json();

        if (data.status === 'success') {
            storyGroups = data.data.grouped_items || [];

            // Build old flat list used for 'All done' checking
            storyRequirements = [];
            storyGroups.forEach(g => {
                storyRequirements.push(...g.requirements);
            });

            renderStoryRequirementsList();
            updateCompleteStoryPhaseButton();
            await loadSprintCandidates();

            // Auto-select first if none selected
            if (!activeStoryReq && storyRequirements.length > 0) {
                const target = storyRequirements.find(r => !isResolvedStoryStatus(r.status)) || storyRequirements[0];
                selectStoryRequirement(target.requirement);
            }
        }
    } catch (e) {
        console.error("Failed to load story requirements:", e);
    }
}

function renderStoryRequirementsList() {
    const container = document.getElementById('story-req-list');
    if (!container) return;

    container.innerHTML = '';
    if (storyGroups.length === 0) {
        container.innerHTML = '<div class="text-[11px] text-slate-500 text-center italic py-4">No requirements found.</div>';
        return;
    }

    storyGroups.forEach((group, index) => {
        // Milestone Header
        const header = document.createElement('div');
        header.className = 'mt-4 mb-2 first:mt-0';
        header.innerHTML = `
            <div class="flex items-center gap-1.5 px-1">
                <span class="material-symbols-outlined text-[14px] text-slate-400">flag</span>
                <h4 class="text-xs font-bold text-slate-700 dark:text-slate-300 uppercase tracking-wide">Milestone ${index + 1}</h4>
            </div>
            ${group.theme ? `<p class="text-[10px] text-slate-500 px-1 mt-0.5 ml-5 italic border-l-2 border-slate-200">${group.theme}</p>` : ''}
        `;
        container.appendChild(header);

        // Milestone Items Container
        const itemsContainer = document.createElement('div');
        itemsContainer.className = 'space-y-1.5 ml-2 border-l-2 border-slate-100 dark:border-slate-800 pl-3';

        group.requirements.forEach(req => {
            let statusColor = 'bg-slate-200 dark:bg-slate-700'; // Default: Pending
            let statusIcon = 'circle';

            if (req.status === 'Saved') {
                statusColor = 'bg-emerald-500';
                statusIcon = 'check_circle';
            } else if (req.status === 'Merged') {
                statusColor = 'bg-sky-500';
                statusIcon = 'merge';
            } else if (req.status === 'Attempted') {
                statusColor = 'bg-amber-500';
                statusIcon = 'hourglass_bottom';
            }

            const isSelected = activeStoryReq === req.requirement;
            const selectedClasses = isSelected
                ? 'border-orange-500 bg-orange-50 dark:bg-orange-900/30 shadow-sm'
                : 'border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800/60 hover:border-orange-300 dark:hover:border-orange-700 cursor-pointer';

            const row = document.createElement('div');
            row.className = `p-2.5 rounded-lg border transition-all ${selectedClasses}`;
            row.onclick = () => {
                if (!isSelected) selectStoryRequirement(req.requirement);
            };

            row.innerHTML = `
                <div class="flex items-start gap-2">
                    <div class="mt-0.5 rounded-full ${statusColor} w-3 h-3 flex-shrink-0 flex items-center justify-center">
                        <span class="material-symbols-outlined text-[8px] text-white font-bold">${statusIcon}</span>
                    </div>
                    <div class="flex-1 min-w-0">
                        <p class="text-[11px] font-bold text-slate-800 dark:text-slate-200 truncate" title="${req.requirement.replace(/"/g, '&quot;')}">${req.requirement}</p>
                        <div class="flex items-center justify-between mt-1">
                            <span class="text-[9px] text-slate-500">${req.status}</span>
                            <span class="text-[9px] font-bold px-1.5 py-0 bg-slate-100 dark:bg-slate-700 text-slate-500 rounded">${req.attempt_count} runs</span>
                        </div>
                    </div>
                </div>
            `;
            itemsContainer.appendChild(row);
        });

        container.appendChild(itemsContainer);
    });
}

async function selectStoryRequirement(reqName) {
    activeStoryReq = reqName;
    renderStoryRequirementsList(); // update active styling

    // Toggle UI panels
    document.getElementById('story-detail-placeholder').classList.add('opacity-0', 'pointer-events-none');
    document.getElementById('story-detail-active').classList.remove('opacity-0', 'pointer-events-none');

    document.getElementById('story-active-req-title').innerText = reqName;

    // Clear input
    const input = document.getElementById('story-user-input');
    if (input) input.value = '';

    activeStoryAttemptCount = 0;
    activeStoryLatestClassification = null;
    applyStoryProjectionState();
    renderStoryHistory([]);
    renderStoryAttemptPanels(null, null);
    updateStorySaveButton();

    // Load history for specific req
    await loadStoryHistory(reqName);
}

function applyStoryProjectionState(payload) {
    const projection = deriveStoryProjectionState(payload);

    activeStoryIsComplete = projection.isComplete;
    activeStoryRetryAvailable = projection.retryAvailable;
    activeStoryRetryTargetAttemptId = projection.retryTargetAttemptId;
    activeStorySaveAvailable = projection.saveAvailable;
    activeStoryDraftKind = projection.draftKind;
    activeStoryResolutionAvailable = projection.resolutionAvailable;
    activeStoryResolutionCurrent = projection.resolutionCurrent;
    activeStoryResolutionRecommendation = projection.resolutionRecommendation;
}

function resolveStoryDisplayAttempt(items, payload) {
    if (!Array.isArray(items) || items.length === 0) {
        return null;
    }

    const projectedDraftAttemptId = payload?.save?.available
        && typeof payload?.current_draft?.attempt_id === 'string'
        ? payload.current_draft.attempt_id
        : null;

    if (projectedDraftAttemptId) {
        const projectedDraftAttempt = items.find((item) => item?.attempt_id === projectedDraftAttemptId);
        if (projectedDraftAttempt) {
            return projectedDraftAttempt;
        }
    }

    return items[items.length - 1];
}

async function loadStoryHistory(reqName) {
    if (!reqName || !selectedProjectId) return;

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/story/history?parent_requirement=${encodeURIComponent(reqName)}`);
        const data = await response.json();

        if (data.status === 'success') {
            const payload = data.data || {};
            const items = Array.isArray(payload.items) ? payload.items : [];
            activeStoryAttemptCount = items.length;
            activeStoryLatestClassification = items.length > 0 ? items[items.length - 1].classification || null : null;
            applyStoryProjectionState(payload);
            renderStoryHistory(items);

            if (items.length > 0) {
                const displayAttempt = resolveStoryDisplayAttempt(items, payload);
                renderStoryAttemptPanels(displayAttempt?.input_context || null, displayAttempt?.output_artifact || null);
            } else {
                renderStoryAttemptPanels(null, null);
            }
            updateStorySaveButton();
        }
    } catch (e) {
        console.error("Failed to load story history:", e);
        activeStoryAttemptCount = 0;
        activeStoryLatestClassification = null;
        applyStoryProjectionState();
        renderStoryHistory([]);
        renderStoryAttemptPanels(null, null);
        updateStorySaveButton();
    }
}

function renderStoryHistory(items) {
    const container = document.getElementById('story-history-list');
    if (!container) return;

    container.innerHTML = '';
    if (!items || items.length === 0) {
        container.innerHTML = '<p class="text-xs text-slate-500">No attempts yet.</p>';
        return;
    }

    const reversed = [...items].reverse();
    reversed.forEach((item, index) => {
        const badgeMeta = {
            reusable_content_result: ['Reusable draft', 'text-emerald-600 bg-emerald-50 dark:bg-emerald-900/30 ring-emerald-200'],
            nonreusable_provider_failure: ['Retryable failure', 'text-slate-700 bg-slate-100 dark:bg-slate-700/70 dark:text-slate-200 ring-slate-200 dark:ring-slate-600'],
            nonreusable_transport_failure: ['Retryable failure', 'text-slate-700 bg-slate-100 dark:bg-slate-700/70 dark:text-slate-200 ring-slate-200 dark:ring-slate-600'],
            nonreusable_schema_failure: ['Schema failure', 'text-red-700 bg-red-50 dark:bg-red-900/30 dark:text-red-300 ring-red-200 dark:ring-red-700'],
            reset_marker: ['Reset', 'text-amber-700 bg-amber-50 dark:bg-amber-900/30 dark:text-amber-300 ring-amber-200 dark:ring-amber-700'],
        };
        const [badgeLabel, badgeClasses] = badgeMeta[item.classification] || ['Attempt', 'text-amber-600 bg-amber-50 dark:bg-amber-900/30 dark:text-amber-300 ring-amber-200 dark:ring-amber-700'];
        const stamp = item.created_at || '-';
        const summary = item.summary ? `<p class="text-[10px] text-slate-500 mt-1">${item.summary}</p>` : '';

        const row = document.createElement('div');
        row.className = 'border border-slate-200 dark:border-slate-700 rounded-lg p-3 bg-slate-50 dark:bg-slate-800/60 transition-transform';
        row.innerHTML = `
            <div class="flex items-center justify-between">
                <span class="text-xs font-extrabold text-slate-700 dark:text-slate-300">Attempt ${items.length - index}</span>
                <span class="text-[10px] uppercase ${badgeClasses} px-2 py-0.5 rounded-full ring-1 ring-inset font-bold">${badgeLabel}</span>
            </div>
            <p class="text-[10px] text-slate-400 mt-2">${stamp}</p>
            ${summary}
        `;
        container.appendChild(row);
    });
}

function renderStoryAttemptPanels(inputContext, outputArtifact) {
    const inputCanvas = document.getElementById('story-input-context');
    const outputCanvas = document.getElementById('story-output-artifact');
    const copyBtn = document.getElementById('btn-copy-story-output');

    if (!inputCanvas || !outputCanvas) return;

    if (!inputContext) {
        inputCanvas.innerText = "No input context available for this attempt.";
    } else {
        inputCanvas.innerText = JSON.stringify(inputContext, null, 2);
    }

    if (!outputArtifact) {
        currentStoryArtifactJSON = null;
        if (copyBtn) copyBtn.classList.add('hidden');
        outputCanvas.innerHTML = `
            <div class="text-xs text-slate-500 flex flex-col items-center justify-center h-full gap-3 opacity-60">
                <span class="material-symbols-outlined text-4xl">receipt_long</span>
                <p>No stories generated yet.</p>
            </div>
        `;
        return;
    }

    outputCanvas.innerHTML = renderStoryArtifactHtml(outputArtifact);
    
    currentStoryArtifactJSON = outputArtifact || null;
    if (copyBtn) {
        if (currentStoryArtifactJSON) {
            copyBtn.classList.remove('hidden');
        } else {
            copyBtn.classList.add('hidden');
        }
    }
}

function renderStoryArtifactHtml(artifact) {
    let html = '';
    const resolutionDetails = activeStoryResolutionCurrent || activeStoryResolutionRecommendation;
    const resolutionAccepted = Boolean(activeStoryResolutionCurrent);

    if (artifact.error) {
        html += `
            <div class="bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 p-4 rounded-lg mb-4">
                <div class="flex items-center gap-2 text-red-700 dark:text-red-400 font-bold mb-2">
                    <span class="material-symbols-outlined">error</span> Generation Failed
                </div>
                <p class="text-[11px] font-mono text-red-600 dark:text-red-300 whitespace-pre-wrap">${artifact.message || 'Unknown error'}</p>
        `;
        const rawOutput = artifact.raw_output || artifact.raw_output_preview;
        if (rawOutput) {
            // Escape HTML just in case
            const safeRaw = rawOutput.replace(/</g, "&lt;").replace(/>/g, "&gt;");
            html += `
                <div class="mt-4 border-t border-red-200 dark:border-red-800/50 pt-3">
                    <p class="text-[10px] font-bold text-red-700 dark:text-red-400 mb-1 uppercase tracking-wide">Raw Agent Output:</p>
                    <pre class="text-[10px] font-mono text-red-600 dark:text-red-300 bg-white/50 dark:bg-black/20 p-2 rounded overflow-x-auto whitespace-pre-wrap">${safeRaw}</pre>
                </div>
            `;
        }
        html += `</div>`;
    } else if (!artifact.is_complete && artifact.clarifying_questions?.length > 0) {
        html += `
            <div class="bg-amber-50 dark:bg-amber-900/30 border border-amber-200 dark:border-amber-700 p-4 rounded-lg mb-4">
                <div class="flex items-center gap-2 text-amber-700 dark:text-amber-400 font-bold mb-2">
                    <span class="material-symbols-outlined">help</span> Agent Needs Clarification
                </div>
                <ul class="text-[11px] text-amber-800 dark:text-amber-300 list-disc list-inside space-y-1">
                    ${artifact.clarifying_questions.map(q => `<li>${q}</li>`).join('')}
                </ul>
            </div>
        `;
    }

    if (resolutionDetails) {
        const ownerRequirement = resolutionDetails.owner_requirement || 'the owner requirement';
        const criteria = Array.isArray(resolutionDetails.acceptance_criteria_to_move)
            ? resolutionDetails.acceptance_criteria_to_move
            : [];

        html += `
            <div class="${resolutionAccepted ? 'bg-sky-50 dark:bg-sky-900/30 border-sky-200 dark:border-sky-800/60' : 'bg-slate-50 dark:bg-slate-800/70 border-slate-200 dark:border-slate-700'} border p-4 rounded-lg mb-4">
                <div class="flex items-center gap-2 ${resolutionAccepted ? 'text-sky-700 dark:text-sky-300' : 'text-slate-700 dark:text-slate-200'} font-bold mb-2">
                    <span class="material-symbols-outlined">${resolutionAccepted ? 'check_circle' : 'merge'}</span>
                    ${resolutionAccepted ? 'Requirement marked as merged' : 'Merge recommended'}
                </div>
                <p class="text-[11px] ${resolutionAccepted ? 'text-sky-800 dark:text-sky-200' : 'text-slate-600 dark:text-slate-300'}">
                    ${resolutionAccepted ? 'This requirement has been resolved as a merged duplicate.' : 'This draft should not be saved as a standalone story.'}
                    Move the acceptance criteria into <strong>${ownerRequirement}</strong>.
                </p>
                ${resolutionDetails.reason ? `<p class="mt-2 text-[10px] text-slate-500 dark:text-slate-400">${resolutionDetails.reason}</p>` : ''}
                ${criteria.length > 0 ? `
                    <div class="mt-3">
                        <h5 class="text-[10px] uppercase font-bold text-slate-500 mb-2">Acceptance Criteria To Move</h5>
                        <ul class="text-[11px] text-slate-700 dark:text-slate-300 space-y-1.5 list-disc pl-4">
                            ${criteria.map(item => `<li>${item}</li>`).join('')}
                        </ul>
                    </div>
                ` : ''}
            </div>
        `;
    }

    const stories = Array.isArray(artifact.user_stories) ? artifact.user_stories : [];
    if (stories.length === 0 && !artifact.error) {
        html += `<p class="text-xs text-slate-500 italic">No stories generated.</p>`;
        return html;
    }

    html += `<div class="space-y-4">`;
    stories.forEach((story, index) => {
        let investBadge = '';
        if (story.invest_score === 'High') {
            investBadge = '<span class="px-2 py-0.5 rounded bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-400 text-[9px] font-black uppercase">INVEST: High</span>';
        } else if (story.invest_score === 'Medium') {
            investBadge = '<span class="px-2 py-0.5 rounded bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-400 text-[9px] font-black uppercase">INVEST: Medium</span>';
        } else {
            investBadge = '<span class="px-2 py-0.5 rounded bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-400 text-[9px] font-black uppercase">INVEST: Low</span>';
        }

        html += `
            <div class="border border-slate-200 dark:border-slate-700 rounded-lg p-4 bg-white dark:bg-slate-800/60 shadow-sm relative pt-6">
                <span class="absolute top-0 right-0 rounded-bl-lg rounded-tr-lg bg-slate-100 dark:bg-slate-700 text-slate-500 text-[9px] font-black px-2 py-1">STORY ${index + 1}</span>
                
                <div class="flex gap-2 items-start justify-between mb-3 border-b border-slate-100 dark:border-slate-700 pb-3">
                    <h4 class="font-bold text-sm text-slate-800 dark:text-slate-200">${story.story_title}</h4>
                    <div class="flex items-center gap-1.5 flex-wrap justify-end">
                        ${story.estimated_effort ? `<span class="px-2 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-400 text-[9px] font-black uppercase">Effort: ${story.estimated_effort}</span>` : ''}
                        ${investBadge}
                    </div>
                </div>
                
                <div class="bg-indigo-50/50 dark:bg-indigo-900/10 p-3 rounded-lg border border-indigo-100 dark:border-indigo-800/30 mb-4">
                    <p class="text-[12px] font-mono text-indigo-900 dark:text-indigo-300 leading-relaxed italic border-l-2 border-indigo-300 dark:border-indigo-600 pl-3">${story.statement}</p>
                </div>
                
                <div>
                    <h5 class="text-[10px] uppercase font-bold text-slate-400 mb-2">Acceptance Criteria</h5>
                    <ul class="text-[11px] text-slate-700 dark:text-slate-300 space-y-1.5 list-disc pl-4">
                        ${(story.acceptance_criteria || []).map(ac => `<li>${ac}</li>`).join('')}
                    </ul>
                </div>
                
                ${(story.produced_artifacts && story.produced_artifacts.length > 0) ? `
                <div class="mt-4 border-t border-slate-100 dark:border-slate-700 pt-3 flex items-start gap-2">
                    <span class="material-symbols-outlined text-[14px] text-indigo-400 mt-0.5 select-none text-transparent bg-clip-text">inventory_2</span>
                    <div>
                        <h5 class="text-[9px] uppercase font-bold text-slate-500 mb-1.5">Produced Artifacts</h5>
                        <div class="flex flex-wrap gap-1.5">
                            ${story.produced_artifacts.map(a => `<span class="px-2 py-0.5 bg-slate-100 dark:bg-slate-700/50 text-slate-700 dark:text-slate-300 text-[10px] rounded border border-slate-200 dark:border-slate-600 shadow-sm font-mono tracking-tight">${a}</span>`).join('')}
                        </div>
                    </div>
                </div>
                ` : ''}
                
                ${story.decomposition_warning ? `
                <div class="mt-4 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800/50 rounded-lg">
                    <h5 class="text-[9px] uppercase font-bold text-red-500 mb-1 flex items-center gap-1"><span class="material-symbols-outlined text-[12px]">warning</span> Decomposition Warning</h5>
                    <p class="text-[10px] text-red-700 dark:text-red-400">${story.decomposition_warning}</p>
                </div>
                ` : ''}
            </div>
        `;
    });
    html += `</div>`;

    // Add Copy Raw JSON button
    const rawJsonStr = JSON.stringify(artifact, null, 2);
    const escapedJson = rawJsonStr.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#039;');

    html += `
        <div class="mt-6 flex justify-end border-t border-slate-200 dark:border-slate-700 pt-4">
            <button onclick="navigator.clipboard.writeText(decodeURIComponent('${encodeURIComponent(rawJsonStr)}')).then(() => { const b=this; const o=b.innerHTML; b.innerHTML='<span class=\\'material-symbols-outlined text-sm\\'>check</span> Copied!'; setTimeout(()=>b.innerHTML=o, 2000); })" 
                    class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-600 dark:text-slate-300 text-[11px] font-bold transition-colors">
                <span class="material-symbols-outlined text-[14px]">content_copy</span>
                Copy Raw JSON
            </button>
        </div>
    `;

    return html;
}

function updateStorySaveButton() {
    const button = document.getElementById('btn-save-story');
    const mergeButton = document.getElementById('btn-merge-story');
    const retryButton = document.getElementById('btn-retry-story');
    const deleteBtn = document.getElementById('btn-delete-story');
    const hint = document.getElementById('story-save-hint');
    if (!button || !hint || !retryButton) return;

    const canSave = Boolean(selectedProjectId) && activeStoryReq && activeStorySaveAvailable;
    const canRetry = Boolean(selectedProjectId) && activeStoryReq && activeStoryRetryAvailable;
    const canMerge = Boolean(selectedProjectId) && activeStoryReq && activeStoryResolutionAvailable && !activeStoryResolutionCurrent;
    button.disabled = !canSave;
    retryButton.disabled = !canRetry;
    retryButton.classList.toggle('hidden', !activeStoryRetryAvailable);

    // Check if it's already saved to change text
    const reqObj = storyRequirements.find(r => r.requirement === activeStoryReq);
    const isSaved = reqObj?.status === 'Saved';
    const isMerged = reqObj?.status === 'Merged' || Boolean(activeStoryResolutionCurrent);
    const resetOnlyState = activeStoryAttemptCount > 0
        && activeStoryLatestClassification === 'reset_marker'
        && !activeStoryRetryAvailable
        && !activeStorySaveAvailable
        && !activeStoryDraftKind
        && !activeStoryResolutionCurrent;

    // Toggle delete button
    if (deleteBtn) {
        if ((activeStoryAttemptCount > 0 && !resetOnlyState) || isSaved || isMerged) {
            deleteBtn.classList.remove('hidden');
        } else {
            deleteBtn.classList.add('hidden');
        }
    }

    if (mergeButton) {
        const showMerge = Boolean(activeStoryResolutionAvailable || activeStoryResolutionCurrent);
        mergeButton.classList.toggle('hidden', !showMerge);
        mergeButton.disabled = !canMerge;
        mergeButton.className = canMerge
            ? 'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-sky-600 hover:bg-sky-500 text-white font-bold transition-all shadow-sm'
            : 'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-sky-200 text-sky-600 dark:bg-sky-900/30 dark:text-sky-300 font-bold cursor-not-allowed transition-all';
        mergeButton.innerHTML = activeStoryResolutionCurrent
            ? '<span class="material-symbols-outlined text-sm">merge</span> Marked as Merged'
            : '<span class="material-symbols-outlined text-sm">merge</span> Mark as Merged';
    }

    button.className = canSave
        ? 'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-bold transition-all shadow-sm'
        : 'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-primary/40 text-white font-bold cursor-not-allowed transition-all';

    if (isSaved) {
        button.innerHTML = '<span class="material-symbols-outlined text-sm">check</span> Saved';
        hint.innerText = activeStoryRetryAvailable
            ? 'Reusable complete draft is already saved. You can retry the latest failed input or save again to overwrite.'
            : 'Reusable complete draft is already saved. Save again to overwrite if needed.';
    } else if (isMerged) {
        const ownerRequirement = activeStoryResolutionCurrent?.owner_requirement || activeStoryResolutionRecommendation?.owner_requirement;
        hint.innerText = ownerRequirement
            ? `This requirement is resolved by merging it into "${ownerRequirement}". Use Reset Draft if you want to reconsider.`
            : 'This requirement is resolved as a merged duplicate. Use Reset Draft if you want to reconsider.';
    } else {
        button.innerHTML = '<span class="material-symbols-outlined text-sm">save</span> Save Stories';
        if (activeStorySaveAvailable && activeStoryRetryAvailable) {
            hint.innerText = 'Reusable complete draft is ready to save. You can also retry the latest failed input.';
        } else if (activeStorySaveAvailable) {
            hint.innerText = 'Reusable complete draft is ready to save.';
        } else if (canMerge && activeStoryResolutionRecommendation?.owner_requirement) {
            hint.innerText = `This draft recommends merging the requirement into "${activeStoryResolutionRecommendation.owner_requirement}" instead of saving duplicate stories.`;
        } else if (activeStoryRetryAvailable) {
            hint.innerText = 'Latest attempt failed without a reusable complete draft. Retry the same input or keep refining.';
        } else {
            hint.innerText = 'Save disabled until a complete reusable draft exists.';
        }
    }
}

function updateCompleteStoryPhaseButton() {
    const btn = document.getElementById('btn-complete-story-phase');
    if (!btn) return;

    // Allow completion if at least one requirement has stories saved
    const anySaved = storyRequirements.length > 0 && storyRequirements.some(r => r.status === 'Saved');

    btn.disabled = !anySaved;
    btn.className = anySaved
        ? 'inline-flex items-center gap-2 px-5 py-2.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-bold transition-all shadow-md animate-pulse-once ring-2 ring-emerald-300'
        : 'inline-flex items-center gap-2 px-5 py-2.5 rounded-lg bg-slate-200 text-slate-400 dark:bg-slate-800 dark:text-slate-600 font-bold cursor-not-allowed transition-all';
}

async function generateStoryDraft() {
    if (!selectedProjectId || !activeStoryReq) return;

    const input = document.getElementById('story-user-input');
    const userInput = input?.value?.trim() || '';
    const requiresRefinementInput = activeStoryAttemptCount > 0 && activeStoryLatestClassification !== 'reset_marker';

    if (requiresRefinementInput && !userInput) {
        alert('Please provide feedback to refine the generated stories.');
        return;
    }

    const button = document.getElementById('btn-generate-story');
    const original = button?.innerHTML;
    if (button) {
        button.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">cycle</span> Running...';
        button.disabled = true;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/story/generate?parent_requirement=${encodeURIComponent(activeStoryReq)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_input: userInput }),
        });

        if (response.status >= 400) {
            const errorBody = await response.json();
            throw new Error(errorBody.detail || 'Generation failed');
        }

        const data = await response.json();
        if (data.status !== 'success') throw new Error('Generation failed');

        // reload data
        await loadStoryRequirements();
        await loadStoryHistory(activeStoryReq);

    } catch (error) {
        console.error(error);
        alert(error.message || 'Generation failed.');
    } finally {
        if (button) {
            button.innerHTML = original || '<span class="material-symbols-outlined text-sm">cycle</span> Generate / Refine';
            button.disabled = false;
        }
    }
}

async function retryStoryDraft() {
    if (!selectedProjectId || !activeStoryReq || !activeStoryRetryAvailable) return;

    const button = document.getElementById('btn-retry-story');
    const original = button?.innerHTML;
    if (button) {
        button.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">refresh</span> Retrying...';
        button.disabled = true;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/story/retry?parent_requirement=${encodeURIComponent(activeStoryReq)}`, {
            method: 'POST',
        });

        if (response.status >= 400) {
            const body = await response.json();
            throw new Error(body.detail || 'Retry failed.');
        }

        const data = await response.json();
        if (data.status !== 'success') throw new Error('Retry failed.');

        await loadStoryRequirements();
        await loadStoryHistory(activeStoryReq);
    } catch (error) {
        console.error(error);
        alert(error.message || 'Retry failed.');
    } finally {
        if (button) {
            button.innerHTML = original || '<span class="material-symbols-outlined text-sm">refresh</span> Retry same input';
            button.disabled = false;
        }
    }
}

async function saveStoryDraft() {
    if (!selectedProjectId || !activeStoryReq) return;

    const button = document.getElementById('btn-save-story');
    const original = button?.innerHTML;
    if (button) {
        button.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">save</span> Saving...';
        button.disabled = true;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/story/save?parent_requirement=${encodeURIComponent(activeStoryReq)}`, {
            method: 'POST',
        });

        if (response.status >= 400) {
            const body = await response.json();
            throw new Error(body.detail || 'Failed to save stories.');
        }

        const data = await response.json();
        if (data.status !== 'success') throw new Error('Failed to save stories.');

        // Success effect
        if (button) {
            button.innerHTML = '<span class="material-symbols-outlined text-sm">check_circle</span> Saved!';
            button.className = 'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-emerald-500 text-white font-bold transition-all shadow-md scale-105 ring-2 ring-emerald-200';
            setTimeout(() => { updateStorySaveButton(); }, 2000);
        }

        // Reload lists to update status dot
        await loadStoryRequirements();

    } catch (error) {
        console.error(error);
        alert(error.message || 'Failed to save stories.');
        if (button) {
            button.innerHTML = original;
            button.disabled = false;
        }
    }
}

async function markStoryAsMerged() {
    if (!selectedProjectId || !activeStoryReq || !activeStoryResolutionAvailable) return;

    const button = document.getElementById('btn-merge-story');
    const original = button?.innerHTML;
    if (button) {
        button.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">merge</span> Marking...';
        button.disabled = true;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/story/merge?parent_requirement=${encodeURIComponent(activeStoryReq)}`, {
            method: 'POST',
        });

        if (response.status >= 400) {
            const body = await response.json();
            throw new Error(body.detail || 'Failed to mark requirement as merged.');
        }

        const data = await response.json();
        if (data.status !== 'success') throw new Error('Failed to mark requirement as merged.');

        await loadStoryRequirements();
        await loadStoryHistory(activeStoryReq);
    } catch (error) {
        console.error(error);
        alert(error.message || 'Failed to mark requirement as merged.');
        if (button) {
            button.innerHTML = original;
            button.disabled = false;
        }
    }
}

async function deleteStoryDraft() {
    if (!selectedProjectId || !activeStoryReq) return;

    if (!confirm(`Are you sure you want to delete the current story draft for "${activeStoryReq}" and reset this requirement? Attempt history will be kept.`)) {
        return;
    }

    const button = document.getElementById('btn-delete-story');
    const original = button?.innerHTML;
    if (button) {
        button.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">delete</span> Deleting...';
        button.disabled = true;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/story?parent_requirement=${encodeURIComponent(activeStoryReq)}`, {
            method: 'DELETE',
        });

        if (response.status >= 400) {
            const body = await response.json();
            throw new Error(body.detail || 'Failed to delete stories.');
        }

        const data = await response.json();
        if (data.status !== 'success') throw new Error('Failed to delete stories.');

        // Success - clear active UI state
        const input = document.getElementById('story-user-input');
        if (input) input.value = '';

        // Reload data from backend
        await loadStoryRequirements();

        // This will reset attempt count and re-render the empty panels
        await loadStoryHistory(activeStoryReq);

    } catch (error) {
        console.error(error);
        alert(error.message || 'Failed to delete stories.');
    } finally {
        if (button) {
            button.innerHTML = original;
            button.disabled = false;
        }
    }
}

async function completeStoryPhase() {
    if (!selectedProjectId) return;

    const btn = document.getElementById('btn-complete-story-phase');
    const original = btn?.innerHTML;
    if (btn) {
        btn.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">flag</span> Processing...';
        btn.disabled = true;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/story/complete_phase`, { method: 'POST' });
        if (response.status >= 400) {
            const body = await response.json();
            throw new Error(body.detail || 'Failed to complete phase.');
        }

        if (btn) {
            btn.innerHTML = original || '<span class="material-symbols-outlined text-sm">flag</span> Complete Refining Phase';
            btn.disabled = false;
        }

        await fetchProjectFSMState(selectedProjectId);
        await loadSprintCandidates();

    } catch (e) {
        alert(e.message || "Failed to complete phase.");
    } finally {
        if (btn) {
            btn.innerHTML = original || '<span class="material-symbols-outlined text-sm">flag</span> Complete Refining Phase';
            btn.disabled = false;
        }
        updateCompleteStoryPhaseButton();
    }
}

// ==========================================
// SPRINT PHASE LOGIC
// ==========================================

async function loadSavedSprints() {
    if (!selectedProjectId) {
        savedSprints = [];
        sprintRuntimeSummary = null;
        currentSprintId = null;
        currentSprintDetail = null;
        currentSprintClosePreview = null;
        sprintMode = null;
        renderOverviewPanel();
        renderSprintSavedWorkspace();
        return [];
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/sprints`);
        const data = await response.json();
        if (data.status !== 'success') {
            throw new Error('Failed to load saved sprints');
        }

        savedSprints = Array.isArray(data.data?.items) ? data.data.items : [];
        sprintRuntimeSummary = data.data?.runtime_summary || null;
    } catch (error) {
        console.error('Failed to load saved sprints:', error);
        savedSprints = [];
        sprintRuntimeSummary = null;
    }

    if (currentSprintId && !getSavedSprintById(currentSprintId)) {
        currentSprintId = null;
        currentSprintDetail = null;
        currentSprintClosePreview = null;
        sprintMode = null;
    }

    const selectedSprint = ensureCurrentSprintSelection();
    if (selectedSprint) {
        await loadSprintDetail(selectedSprint.id);
    } else {
        currentSprintDetail = null;
        currentSprintClosePreview = null;
    }

    renderOverviewPanel();
    renderSprintSavedWorkspace();
    updateProjectNavUI();

    return savedSprints;
}

async function loadSprintDetail(sprintId) {
    if (!selectedProjectId || !sprintId) {
        currentSprintDetail = null;
        currentSprintClosePreview = null;
        return null;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/sprints/${sprintId}`);
        const data = await response.json();
        if (data.status !== 'success') {
            throw new Error('Failed to load sprint detail');
        }

        currentSprintDetail = data.data?.sprint || null;
        sprintRuntimeSummary = data.data?.runtime_summary || sprintRuntimeSummary;
        currentSprintId = currentSprintDetail?.id || Number(sprintId);
        sprintMode = currentSprintDetail ? getSprintMode(currentSprintDetail) : null;
        currentSprintClosePreview = null;
        return currentSprintDetail;
    } catch (error) {
        console.error('Failed to load sprint detail:', error);
        currentSprintDetail = null;
        currentSprintClosePreview = null;
        return null;
    }
}

function resetSprintClosePanel() {
    currentSprintClosePreview = null;
    const panel = document.getElementById('sprint-close-panel');
    const summary = document.getElementById('sprint-close-summary');
    const confirmButton = document.getElementById('btn-confirm-sprint-close');
    const closeNotes = document.getElementById('sprint-close-notes');
    const followUpNotes = document.getElementById('sprint-follow-up-notes');

    if (panel) panel.classList.add('hidden');
    if (summary) summary.innerText = '';
    if (confirmButton) {
        confirmButton.disabled = true;
        confirmButton.classList.remove('cursor-not-allowed', 'opacity-60');
    }
    if (closeNotes) closeNotes.value = '';
    if (followUpNotes) followUpNotes.value = '';
}

function renderOverviewPanel() {
    const container = document.getElementById('overview-panel-content');
    if (!container) return;

    const completedCount = getCompletedPhaseCount();
    const nextPhase = getNextIncompletePlanningPhase(activeFsmState);
    const planningComplete = isPlanningCompleteState(activeFsmState);
    const activeSprintId = sprintRuntimeSummary?.active_sprint_id || null;
    const plannedSprintId = sprintRuntimeSummary?.planned_sprint_id || null;
    const latestCompletedSprintId = sprintRuntimeSummary?.latest_completed_sprint_id || null;
    const savedSprintCount = savedSprints.length;

    const primaryActionHtml = activeSprintId
        ? `<button type="button" onclick="selectSavedSprintById(${activeSprintId})" class="inline-flex items-center gap-2 rounded-lg bg-sky-600 px-5 py-2.5 text-sm font-bold text-white shadow-sm transition-colors hover:bg-sky-700">
                <span class="material-symbols-outlined text-sm">play_circle</span>
                Open Active Sprint
           </button>`
        : plannedSprintId
            ? `<button type="button" onclick="selectSavedSprintById(${plannedSprintId})" class="inline-flex items-center gap-2 rounded-lg bg-sky-600 px-5 py-2.5 text-sm font-bold text-white shadow-sm transition-colors hover:bg-sky-700">
                    <span class="material-symbols-outlined text-sm">schedule</span>
                    Open Planned Sprint
               </button>`
            : `<button type="button" onclick="openSprintPlanner()" class="inline-flex items-center gap-2 rounded-lg bg-sky-600 px-5 py-2.5 text-sm font-bold text-white shadow-sm transition-colors hover:bg-sky-700">
                    <span class="material-symbols-outlined text-sm">add_task</span>
                    Create Next Sprint
               </button>`;

    container.innerHTML = `
        <div class="space-y-6">
            <div class="rounded-2xl border border-sky-200 bg-gradient-to-r from-sky-50 via-white to-cyan-50 p-6 shadow-sm dark:border-sky-800 dark:from-sky-900/30 dark:via-slate-900 dark:to-cyan-900/20">
                <div class="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
                    <div class="space-y-2">
                        <div class="inline-flex items-center gap-2 rounded-full border border-sky-200 bg-white px-3 py-1 text-[11px] font-black uppercase tracking-[0.18em] text-sky-700 dark:border-sky-700 dark:bg-sky-900/30 dark:text-sky-300">
                            <span class="material-symbols-outlined text-sm">dashboard</span>
                            Workflow Overview
                        </div>
                        <div>
                            <h3 class="text-2xl font-black text-slate-800 dark:text-white">Planning is ${planningComplete ? 'complete' : 'still in progress'}</h3>
                            <p class="mt-1 text-sm text-slate-600 dark:text-slate-300">
                                ${planningComplete
                                    ? 'Sprint runtime continues in the sprint workspace.'
                                    : (nextPhase ? `The next unfinished planning step is ${capitalizePhase(nextPhase)}.` : 'Continue the planning pipeline before sprint runtime begins.')}
                            </p>
                        </div>
                    </div>
                    <div class="flex flex-wrap gap-3">
                        ${primaryActionHtml}
                    </div>
                </div>
            </div>

            <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900">
                    <div class="text-[10px] font-black uppercase tracking-[0.18em] text-slate-400">Completed Steps</div>
                    <div class="mt-3 text-3xl font-black text-slate-800 dark:text-white">${completedCount}/6</div>
                    <p class="mt-1 text-[11px] text-slate-500 dark:text-slate-400">Planner progress across setup, vision, backlog, roadmap, stories, and sprint.</p>
                </div>
                <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900">
                    <div class="text-[10px] font-black uppercase tracking-[0.18em] text-slate-400">Saved Sprints</div>
                    <div class="mt-3 text-3xl font-black text-slate-800 dark:text-white">${savedSprintCount}</div>
                    <p class="mt-1 text-[11px] text-slate-500 dark:text-slate-400">${savedSprintCount > 0 ? 'Completed sprints stay available as history while the next sprint is planned or run.' : 'No saved sprint plan exists yet for this project.'}</p>
                </div>
                <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900">
                    <div class="text-[10px] font-black uppercase tracking-[0.18em] text-slate-400">Planning Status</div>
                    <div class="mt-3 text-xl font-black text-slate-800 dark:text-white">${planningComplete ? 'Ready for Iteration' : 'In Progress'}</div>
                    <p class="mt-1 text-[11px] text-slate-500 dark:text-slate-400">${planningComplete ? 'The one-time planning pipeline is complete for this project shell.' : 'Continue setup, vision, backlog, roadmap, stories, and sprint planning.'}</p>
                </div>
                <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900">
                    <div class="text-[10px] font-black uppercase tracking-[0.18em] text-slate-400">Sprint Runtime</div>
                    <div class="mt-3 text-xl font-black text-slate-800 dark:text-white">${activeSprintId ? 'Sprint Active' : plannedSprintId ? 'Planned Sprint Ready' : latestCompletedSprintId ? 'Last Sprint Completed' : 'No Sprint Yet'}</div>
                    <p class="mt-1 text-[11px] text-slate-500 dark:text-slate-400">${activeSprintId
                        ? 'Execution is active. Open the sprint workspace to manage in-flight work.'
                        : plannedSprintId
                            ? 'A planned sprint is ready to start or refine.'
                            : latestCompletedSprintId
                                ? 'The last sprint is closed and available as read-only history.'
                                : 'Create the first sprint to begin iterative runtime.'}</p>
                </div>
            </div>
        </div>
    `;
}

const escapeHtml = (str) => {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
};

function renderSprintSavedWorkspace() {
    const savedWorkspace = document.getElementById('sprint-saved-workspace');
    const plannerWorkspace = document.getElementById('sprint-planner-workspace');
    const phaseTitle = document.getElementById('sprint-phase-title');
    const phaseSubtitle = document.getElementById('sprint-phase-subtitle');

    if (!savedWorkspace || !plannerWorkspace || !phaseTitle || !phaseSubtitle) return;

    const selectedSprintLite = ensureCurrentSprintSelection();
    const selectedSprint = currentSprintDetail && currentSprintDetail.id === currentSprintId
        ? currentSprintDetail
        : null;
    const sprintRecord = selectedSprint || selectedSprintLite;
    const shouldShowSavedWorkspace = viewPhaseId === 'sprint' && Boolean(sprintRecord) && !showSprintPlanner;

    savedWorkspace.classList.toggle('hidden', !shouldShowSavedWorkspace);
    plannerWorkspace.classList.toggle('hidden', shouldShowSavedWorkspace);

    if (!shouldShowSavedWorkspace || !sprintRecord) {
        resetSprintClosePanel();
        phaseTitle.innerText = 'Sprint Planning';
        phaseSubtitle.innerText = 'Commit User Stories to a Sprint Backlog with capacity planning.';
        return;
    }

    sprintMode = getSprintMode(sprintRecord);
    const isCompletedSprint = sprintMode === 'completed';
    const closeSnapshot = selectedSprint?.close_snapshot || null;
    const closeSummaryText = closeSnapshot
        ? `${closeSnapshot.completed_story_count || 0} stories completed, ${closeSnapshot.open_story_count || 0} unfinished at close.`
        : 'Closed sprint history remains read-only and available for reference.';

    phaseTitle.innerText = sprintMode === 'active'
        ? 'Current Sprint'
        : sprintMode === 'completed'
            ? 'Completed Sprint'
            : 'Sprint Ready to Start';
    phaseSubtitle.innerText = sprintMode === 'active'
        ? 'Focus on the saved sprint currently in execution.'
        : sprintMode === 'completed'
            ? 'Review the closed sprint history and plan the next iteration when ready.'
            : 'Your sprint plan is saved. Start it when the team is ready to begin work.';

    const statusPill = document.getElementById('sprint-saved-status-pill');
    if (statusPill) {
        statusPill.className = sprintMode === 'active'
            ? 'inline-flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-xs font-black uppercase tracking-wide text-emerald-700 dark:border-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300'
            : sprintMode === 'completed'
                ? 'inline-flex items-center gap-1.5 rounded-full border border-slate-300 bg-slate-100 px-3 py-1 text-xs font-black uppercase tracking-wide text-slate-700 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200'
                : 'inline-flex items-center gap-1.5 rounded-full border border-amber-200 bg-amber-50 px-3 py-1 text-xs font-black uppercase tracking-wide text-amber-700 dark:border-amber-700 dark:bg-amber-900/30 dark:text-amber-300';
        statusPill.innerHTML = `
            <span class="material-symbols-outlined text-sm">${sprintMode === 'active' ? 'play_circle' : sprintMode === 'completed' ? 'history' : 'schedule'}</span>
            ${sprintMode === 'active' ? 'Active Sprint' : sprintMode === 'completed' ? 'Completed Sprint' : 'Planned Sprint'}
        `;
    }

    document.getElementById('sprint-saved-title').innerText = sprintRecord.goal || 'Saved sprint plan';
    document.getElementById('sprint-saved-subtitle').innerText = sprintMode === 'active'
        ? `Started ${formatSafeDate(sprintRecord.started_at)} with ${sprintRecord.story_count || 0} committed stories.`
        : sprintMode === 'completed'
            ? `Closed ${formatSafeDate(sprintRecord.completed_at)}. ${closeSummaryText}`
            : `Planned for ${formatSafeDate(sprintRecord.start_date)} to ${formatSafeDate(sprintRecord.end_date)} with ${sprintRecord.story_count || 0} committed stories.`;

    const startButton = document.getElementById('btn-start-sprint');
    const closeButton = document.getElementById('btn-close-sprint');
    const plannerButton = document.getElementById('btn-open-sprint-planner');
    if (startButton) {
        const canStart = Boolean(sprintRecord.allowed_actions?.can_start);
        startButton.classList.toggle('hidden', !canStart);
        startButton.disabled = !canStart;
    }
    if (closeButton) {
        const canClose = Boolean(sprintRecord.allowed_actions?.can_close);
        closeButton.classList.toggle('hidden', !canClose);
        closeButton.disabled = !canClose;
    }
    if (plannerButton) {
        const canCreateNextSprint = Boolean(sprintRuntimeSummary?.can_create_next_sprint);
        const plannerLabel = canCreateNextSprint ? 'Create Next Sprint' : 'Modify Planned Sprint';
        plannerButton.innerHTML = `
            <span class="material-symbols-outlined text-sm">${canCreateNextSprint ? 'add_task' : 'edit_note'}</span>
            ${plannerLabel}
        `;
    }

    if (isCompletedSprint || sprintMode !== 'active') {
        resetSprintClosePanel();
    }

    const meta = document.getElementById('sprint-saved-meta');
    if (meta) {
        const totalTasks = Array.isArray(selectedSprint?.selected_stories)
            ? selectedSprint.selected_stories.reduce((sum, story) => sum + (Array.isArray(story.tasks) ? story.tasks.length : 0), 0)
            : 0;
        meta.innerHTML = `
            <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900">
                <div class="text-[10px] font-black uppercase tracking-[0.18em] text-slate-400">Team</div>
                <div class="mt-3 text-lg font-black text-slate-800 dark:text-white">${sprintRecord.team_name || 'Unassigned'}</div>
                <p class="mt-1 text-[11px] text-slate-500 dark:text-slate-400">Saved sprint owner.</p>
            </div>
            <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900">
                <div class="text-[10px] font-black uppercase tracking-[0.18em] text-slate-400">${isCompletedSprint ? 'Closed At' : 'Planned Window'}</div>
                <div class="mt-3 text-lg font-black text-slate-800 dark:text-white">${isCompletedSprint ? formatSafeDate(sprintRecord.completed_at) : `${formatSafeDate(sprintRecord.start_date)} - ${formatSafeDate(sprintRecord.end_date)}`}</div>
                <p class="mt-1 text-[11px] text-slate-500 dark:text-slate-400">${isCompletedSprint ? 'Completion remains preserved as historical runtime state.' : 'Planning dates remain separate from execution start.'}</p>
            </div>
            <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900">
                <div class="text-[10px] font-black uppercase tracking-[0.18em] text-slate-400">Committed Stories</div>
                <div class="mt-3 text-3xl font-black text-slate-800 dark:text-white">${sprintRecord.story_count || 0}</div>
                <p class="mt-1 text-[11px] text-slate-500 dark:text-slate-400">Saved scope for this sprint.</p>
            </div>
            <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900">
                <div class="text-[10px] font-black uppercase tracking-[0.18em] text-slate-400">${isCompletedSprint ? 'Close Notes' : 'Task Breakdown'}</div>
                <div class="mt-3 text-${isCompletedSprint ? 'lg' : '3xl'} font-black text-slate-800 dark:text-white">${isCompletedSprint ? escapeHtml(closeSnapshot?.completion_notes || 'No close notes recorded.') : totalTasks}</div>
                <p class="mt-1 text-[11px] text-slate-500 dark:text-slate-400">${isCompletedSprint ? escapeHtml(closeSnapshot?.follow_up_notes || 'No follow-up notes recorded.') : (sprintMode === 'active' ? 'Execution checklist carried from planning.' : 'Task decomposition ready for kickoff.')}</p>
            </div>
        `;
    }

    const switcher = document.getElementById('sprint-saved-switcher');
    const switcherList = document.getElementById('sprint-saved-switcher-list');
    if (switcher && switcherList) {
        switcher.classList.toggle('hidden', savedSprints.length <= 1);
        switcherList.innerHTML = savedSprints.map((sprint) => {
            const active = sprint.id === sprintRecord.id;
            const mode = getSprintMode(sprint);
            const modeLabel = mode === 'active' ? 'Active' : mode === 'completed' ? 'Completed' : 'Planned';
            return `
                <button type="button" onclick="selectSavedSprintById(${sprint.id})"
                    class="${active
                        ? 'inline-flex items-center gap-2 rounded-full border border-sky-200 bg-sky-50 px-3 py-1.5 text-[11px] font-bold text-sky-700 dark:border-sky-700 dark:bg-sky-900/30 dark:text-sky-300'
                        : 'inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-[11px] font-bold text-slate-600 transition-colors hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300 dark:hover:bg-slate-800'}">
                    <span class="material-symbols-outlined text-sm">${mode === 'active' ? 'play_circle' : mode === 'completed' ? 'history' : 'schedule'}</span>
                    Sprint ${sprint.id} · ${modeLabel}
                </button>
            `;
        }).join('');
    }

    const storyList = document.getElementById('sprint-saved-story-list');
    if (storyList) {
        const stories = Array.isArray(selectedSprint?.selected_stories) ? selectedSprint.selected_stories : [];
        if (!selectedSprint) {
            storyList.innerHTML = '<div class="rounded-xl border border-dashed border-slate-200 bg-slate-50 p-6 text-sm text-slate-500 dark:border-slate-700 dark:bg-slate-800/40 dark:text-slate-400">Loading sprint details...</div>';
            return;
        }
        if (stories.length === 0) {
            storyList.innerHTML = '<div class="rounded-xl border border-dashed border-slate-200 bg-slate-50 p-6 text-sm text-slate-500 dark:border-slate-700 dark:bg-slate-800/40 dark:text-slate-400">This saved sprint has no committed stories yet.</div>';
        } else {
            storyList.innerHTML = stories.map((story, index) => {
                const tasks = Array.isArray(story.tasks) ? story.tasks : [];
                const actionableTasks = tasks.filter((task) => task.is_executable !== false);
                const allTasksDone = actionableTasks.length > 0 && actionableTasks.every(t => t.status === 'Done' || t.status === 'Cancelled');
                const isStoryDone = story.status === 'Done';
                let storyStateBanner = '';
                
                if (isCompletedSprint) {
                    storyStateBanner = '';
                } else if (isStoryDone) {
                    storyStateBanner = `
                    <div class="mb-3 rounded-lg border border-purple-200 bg-purple-50 p-3 shadow-sm dark:border-purple-800/50 dark:bg-purple-900/30 flex items-center justify-between">
                        <div class="flex items-center gap-3">
                            <span class="material-symbols-outlined text-purple-600 dark:text-purple-400">task_alt</span>
                            <span class="text-sm font-bold text-purple-800 dark:text-purple-300">Story marked as Done.</span>
                        </div>
                        <button onclick="toggleStoryClose(event, ${selectedSprint.id}, ${story.story_id})" class="inline-flex items-center gap-1 text-[11px] font-bold px-3 py-1.5 rounded bg-purple-100 hover:bg-purple-200 text-purple-700 transition-colors dark:bg-purple-900/50 dark:hover:bg-purple-800 dark:text-purple-300 shadow-sm border border-purple-200 dark:border-purple-700">
                            <span class="material-symbols-outlined text-[14px]">visibility</span> View Close Log
                        </button>
                    </div>`;
                } else if (allTasksDone) {
                    storyStateBanner = `
                    <div class="mb-3 rounded-lg border border-emerald-200 bg-emerald-50 p-3 shadow-sm dark:border-emerald-800 dark:bg-emerald-900/30 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                        <div class="flex items-center gap-3">
                            <span class="material-symbols-outlined text-emerald-600 dark:text-emerald-400">check_circle</span>
                            <span class="text-sm font-bold text-emerald-800 dark:text-emerald-300">All actionable tasks completed. Ready for manual close.</span>
                        </div>
                        <button onclick="toggleStoryClose(event, ${selectedSprint.id}, ${story.story_id})" class="inline-flex items-center gap-1 text-[11px] font-bold px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-700 text-white transition-colors shadow-sm dark:bg-emerald-500 dark:hover:bg-emerald-600">
                            <span class="material-symbols-outlined text-[14px]">task_alt</span> Close Story
                        </button>
                    </div>`;
                }

                return `
                <div class="rounded-xl border border-slate-200 bg-slate-50 p-4 shadow-sm dark:border-slate-700 dark:bg-slate-800/40 relative">
                    ${storyStateBanner}
                    <div id="story-close-${story.story_id}" class="hidden mb-4 flex flex-col gap-3 rounded-lg border border-emerald-100 bg-emerald-50/50 p-4 shadow-sm dark:border-emerald-900/50 dark:bg-emerald-950/20"></div>
                    <div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                        <div class="space-y-2">
                            <div class="inline-flex items-center gap-2 rounded-full bg-slate-200 px-2.5 py-1 text-[10px] font-black uppercase tracking-wide text-slate-600 dark:bg-slate-700 dark:text-slate-300">
                                Story ${index + 1}
                            </div>
                            <h5 class="text-sm font-black text-slate-800 dark:text-slate-100">${story.story_title || `Story ${story.story_id}`}</h5>
                        </div>
                        <div class="flex flex-col items-start sm:items-end gap-2">
                            <div class="inline-flex items-center gap-2 rounded-full bg-white px-3 py-1 text-[11px] font-bold text-slate-600 shadow-sm dark:bg-slate-900 dark:text-slate-300">
                                <span class="material-symbols-outlined text-sm">flare</span>
                                ${Number.isFinite(story.story_points) ? `${story.story_points} pts` : 'Unestimated'}
                            </div>
                            <button onclick="copyStoryPrompt(event, ${selectedSprint.id}, ${story.story_id})" class="inline-flex items-center gap-1 text-[10px] font-bold px-2 py-1 rounded bg-slate-100 hover:bg-slate-200 text-slate-600 transition-colors dark:bg-slate-800 dark:hover:bg-slate-700 dark:text-slate-300 border border-slate-200 dark:border-slate-700 shadow-sm">
                                <span class="material-symbols-outlined text-[12px]">content_copy</span> Copy Story Prompt
                            </button>
                        </div>
                    </div>
                    <div class="mt-4 space-y-2">
                        <div class="text-[10px] font-black uppercase tracking-[0.18em] text-slate-400">Tasks</div>
                        ${(Array.isArray(story.tasks) && story.tasks.length > 0)
                            ? `<ul class="space-y-3 text-sm text-slate-700 dark:text-slate-300">
                                ${story.tasks.map((task) => {
                                    const desc = escapeHtml(task.description || task);
                                    const kindStr = task.task_kind ? escapeHtml(task.task_kind) : '';
                                    const kind = kindStr ? `<span class="inline-flex items-center px-2 py-0.5 rounded border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-[10px] font-black uppercase text-slate-600 dark:text-slate-300 shadow-sm">${kindStr}</span>` : '';
                                    const tags = Array.isArray(task.workstream_tags) ? task.workstream_tags.map(t => `<span class="inline-flex items-center bg-teal-50 dark:bg-teal-900/30 text-teal-700 dark:text-teal-300 px-2 py-0.5 rounded text-[10px] font-bold border border-teal-100 dark:border-teal-800 shadow-sm">#${escapeHtml(t)}</span>`).join(' ') : '';
                                    const targetsStr = Array.isArray(task.artifact_targets) ? task.artifact_targets.map(escapeHtml).join(', ') : '';
                                    const targets = targetsStr ? `<div class="text-[11px] text-slate-500 dark:text-slate-400 mt-2 flex gap-1.5 bg-slate-50 w-full p-2 rounded border border-slate-100 dark:border-slate-800 dark:bg-slate-950/50"><span class="font-bold shrink-0">Targets:</span> <span class="break-words">${targetsStr}</span></div>` : '';
                                    const isExecutable = task.is_executable !== false;
                                    const executionMode = isExecutable
                                        ? ''
                                        : `<span class="inline-flex items-center px-2 py-0.5 rounded border border-amber-200 bg-amber-50 text-[10px] font-black uppercase text-amber-700 shadow-sm dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-300">Reference only</span>`;

                                    const statusStr = task.status ? escapeHtml(task.status) : 'To Do';
                                    const statusColors = {
                                        'To Do': 'bg-slate-100 text-slate-600 border border-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700',
                                        'In Progress': 'bg-amber-50 text-amber-700 border border-amber-200 dark:bg-amber-900/30 dark:text-amber-300 dark:border-amber-800',
                                        'Done': 'bg-emerald-50 text-emerald-700 border border-emerald-200 dark:bg-emerald-900/30 dark:text-emerald-300 dark:border-emerald-800',
                                        'Cancelled': 'bg-rose-50 text-rose-700 border border-rose-200 dark:bg-rose-900/30 dark:text-rose-300 dark:border-rose-800'
                                    };
                                    const badgeColor = statusColors[statusStr] || statusColors['To Do'];
                                    const statusBadge = `<span id="task-badge-${task.id}" class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-black uppercase tracking-wider ${badgeColor} shadow-sm">${statusStr}</span>`;

                                    return `
                                <li class="flex flex-col gap-2 rounded-lg bg-white p-3 shadow-sm border border-slate-100 dark:border-slate-800 dark:bg-slate-900 group">
                                    <div class="flex items-start gap-2">
                                        <span class="material-symbols-outlined mt-0.5 text-sm text-teal-500 shrink-0">task_alt</span>
                                        <div class="flex-1 flex flex-col min-w-0 gap-1.5">
                                            <div class="flex flex-wrap items-center gap-1.5">${statusBadge} ${kind}${tags}${executionMode}</div>
                                            <span class="text-sm font-medium text-slate-800 dark:text-slate-200 leading-snug">${desc}</span>
                                            ${targets}
                                        </div>
                                    </div>
                                    ${task.id ? `
                                    <div class="flex flex-wrap items-center gap-2 ml-6 mt-2">
                                        ${isExecutable ? `
                                        <button onclick="copyTaskPrompt(event, ${selectedSprint.id}, ${task.id})" class="inline-flex items-center gap-1 text-[10px] font-bold px-2 py-1 rounded bg-slate-100 hover:bg-slate-200 text-slate-600 transition-colors dark:bg-slate-800 dark:hover:bg-slate-700 dark:text-slate-300 border border-slate-200 dark:border-slate-700 shadow-sm">
                                            <span class="material-symbols-outlined text-[12px]">content_copy</span> Copy Task Prompt
                                        </button>
                                        ` : `
                                        <span class="inline-flex items-center gap-1 text-[10px] font-bold px-2 py-1 rounded bg-amber-50 text-amber-700 border border-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:border-amber-800 shadow-sm">
                                            <span class="material-symbols-outlined text-[12px]">lock</span> Reference Only
                                        </span>
                                        `}
                                        <button onclick="toggleTaskBrief(event, ${selectedSprint.id}, ${task.id})" class="inline-flex items-center gap-1 text-[10px] font-bold px-2 py-1 rounded bg-sky-50 hover:bg-sky-100 text-sky-700 transition-colors dark:bg-sky-900/30 dark:hover:bg-sky-900/50 dark:text-sky-300 border border-sky-200 dark:border-sky-800 shadow-sm">
                                            <span class="material-symbols-outlined text-[12px]">visibility</span> View Brief
                                        </button>
                                        ${isExecutable && !isCompletedSprint ? `
                                        <button onclick="toggleTaskExecution(event, ${selectedSprint.id}, ${task.id})" class="inline-flex items-center gap-1 text-[10px] font-bold px-2 py-1 rounded bg-indigo-50 hover:bg-indigo-100 text-indigo-700 transition-colors dark:bg-indigo-900/30 dark:hover:bg-indigo-900/50 dark:text-indigo-300 border border-indigo-200 dark:border-indigo-800 shadow-sm">
                                            <span class="material-symbols-outlined text-[12px]">edit_note</span> Log Progress
                                        </button>
                                        ` : ''}
                                    </div>
                                    <div id="task-brief-${task.id}" class="hidden ml-6 mt-2 p-4 text-[12px] text-slate-700 dark:text-slate-300 bg-slate-50 border border-slate-200 rounded-md dark:bg-slate-950/50 dark:border-slate-800 relative">
                                        <div class="absolute inset-0 flex items-center justify-center bg-white/50 dark:bg-slate-900/50 hidden backdrop-blur-sm z-10" id="task-brief-loading-${task.id}">
                                             <span class="material-symbols-outlined animate-spin text-sky-500 text-2xl">cycle</span>
                                        </div>
                                        <div id="task-brief-content-${task.id}" class="whitespace-pre-wrap leading-relaxed selection:bg-sky-200 dark:selection:bg-sky-900"></div>
                                    </div>
                                    ${isExecutable && !isCompletedSprint ? `<div id="task-execution-${task.id}" class="hidden ml-6 mt-2 flex flex-col gap-3 rounded-lg border border-indigo-100 bg-indigo-50/30 p-4 shadow-sm dark:border-indigo-900/50 dark:bg-indigo-950/20"></div>` : ''}
                                    ` : ''}
                                </li>`;
                                }).join('')}
                               </ul>`
                            : '<div class="text-sm text-slate-500 dark:text-slate-400">No tasks were saved for this story.</div>'}
                    </div>
                </div>
            `;
            }).join('');
        }
    }
}

async function selectSavedSprintById(sprintId) {
    const savedSprint = getSavedSprintById(sprintId);
    if (!savedSprint) return;

    currentSprintId = savedSprint.id;
    await loadSprintDetail(savedSprint.id);
    sprintMode = getSprintMode(currentSprintDetail || savedSprint);
    showSprintPlanner = false;
    viewPhaseId = 'sprint';
    renderPhaseSection();
    updateNextButton();
}

function resetSprintPlannerWorkingSet() {
    selectedSprintStoryIds = new Set();
    latestSprintIsComplete = false;
    sprintAttemptCount = 0;
    currentSprintArtifactJSON = null;
    currentSprintInputContextJSON = null;
    renderSprintHistory([]);
    renderSprintAttemptPanels(null, null);
    updateSprintSaveButton();
}

async function resetSprintPlannerStateForCreateNext() {
    if (!selectedProjectId) return;

    const response = await fetch(`/api/projects/${selectedProjectId}/sprint/planner/reset`, {
        method: 'POST',
    });
    const data = await response.json().catch(() => null);

    if (response.status >= 400 || data?.status !== 'success') {
        throw new Error(data?.detail || 'Failed to reset sprint planner state.');
    }
}

async function openSprintPlanner() {
    resetSprintClosePanel();
    viewPhaseId = 'sprint';
    showSprintPlanner = true;
    const shouldStartFreshCycle = Boolean(sprintRuntimeSummary?.can_create_next_sprint);
    if (shouldStartFreshCycle) {
        resetSprintPlannerWorkingSet();
    }
    renderPhaseSection();
    updateNextButton();

    try {
        if (shouldStartFreshCycle) {
            await resetSprintPlannerStateForCreateNext();
        }
        await Promise.all([
            loadSprintCandidates(),
            loadSprintHistory(),
        ]);
    } catch (error) {
        console.error('Failed to open sprint planner:', error);
        alert(error.message || 'Failed to open sprint planner.');
    }
}

async function startCurrentSprint() {
    const savedSprint = currentSprintDetail && currentSprintDetail.id === currentSprintId
        ? currentSprintDetail
        : ensureCurrentSprintSelection();
    if (!selectedProjectId || !savedSprint || !savedSprint.allowed_actions?.can_start) return;

    const button = document.getElementById('btn-start-sprint');
    const originalHtml = button?.innerHTML;
    if (button) {
        button.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">progress_activity</span> Starting...';
        button.disabled = true;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/sprints/${savedSprint.id}/start`, {
            method: 'PATCH',
        });

        if (response.status >= 400) {
            const body = await response.json().catch(() => null);
            throw new Error(body?.detail || 'Failed to start sprint.');
        }

        await loadSavedSprints();
        await fetchProjectFSMState(selectedProjectId, { preserveView: true });
        await selectSavedSprintById(savedSprint.id);
    } catch (error) {
        console.error(error);
        alert(error.message || 'Failed to start sprint.');
    } finally {
        if (button) {
            button.innerHTML = originalHtml || '<span class="material-symbols-outlined text-sm">play_circle</span> Start Sprint';
            button.disabled = false;
        }
    }
}

async function openSprintClosePanel() {
    if (!selectedProjectId || !currentSprintId) return;

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/sprints/${currentSprintId}/close`);
        const data = await response.json();
        if (response.status >= 400) {
            throw new Error(data?.detail || 'Failed to load sprint close readiness.');
        }

        currentSprintClosePreview = data;
        const panel = document.getElementById('sprint-close-panel');
        const summary = document.getElementById('sprint-close-summary');
        const confirmButton = document.getElementById('btn-confirm-sprint-close');

        if (!panel || !summary || !confirmButton) return;

        panel.classList.remove('hidden');
        summary.innerText = data.close_eligible
            ? `${data.readiness.completed_story_count} stories completed, ${data.readiness.open_story_count} still open. Unfinished work can be explicitly selected into the next planned sprint.`
            : (data.ineligible_reason || 'Sprint cannot be closed yet.');
        confirmButton.disabled = !data.close_eligible;
        confirmButton.classList.toggle('cursor-not-allowed', !data.close_eligible);
        confirmButton.classList.toggle('opacity-60', !data.close_eligible);
    } catch (error) {
        console.error(error);
        alert(error.message || 'Failed to load sprint close readiness.');
    }
}

async function confirmSprintClose() {
    if (!selectedProjectId || !currentSprintId) return;

    const completionNotes = document.getElementById('sprint-close-notes')?.value?.trim() || '';
    const followUpNotes = document.getElementById('sprint-follow-up-notes')?.value?.trim() || '';
    if (!completionNotes) {
        alert('Sprint close notes are required.');
        return;
    }

    const button = document.getElementById('btn-confirm-sprint-close');
    const originalHtml = button?.innerHTML;
    if (button) {
        button.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">progress_activity</span> Closing...';
        button.disabled = true;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/sprints/${currentSprintId}/close`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                completion_notes: completionNotes,
                follow_up_notes: followUpNotes || null,
            }),
        });

        if (response.status >= 400) {
            const body = await response.json().catch(() => null);
            throw new Error(body?.detail || 'Failed to close sprint.');
        }

        await loadSavedSprints();
        await fetchProjectFSMState(selectedProjectId, { preserveView: true });
        await selectSavedSprintById(currentSprintId);
    } catch (error) {
        console.error(error);
        alert(error.message || 'Failed to close sprint.');
    } finally {
        if (button) {
            button.innerHTML = originalHtml || '<span class="material-symbols-outlined text-sm">check_circle</span> Confirm Close';
            button.disabled = false;
        }
    }
}

function attachSprintInputListeners() {
    const velocityInput = document.getElementById('sprint-velocity');
    const maxPointsInput = document.getElementById('sprint-max-story-points');
    const teamNameInput = document.getElementById('sprint-team-name');
    const startDateInput = document.getElementById('sprint-start-date');

    velocityInput?.addEventListener('change', updateSprintCapacityWarning);
    maxPointsInput?.addEventListener('input', updateSprintCapacityWarning);
    teamNameInput?.addEventListener('input', updateSprintSaveButton);
    startDateInput?.addEventListener('change', updateSprintSaveButton);

    initializeSprintSaveForm();
}

function getTodayLocalDateValue() {
    const now = new Date();
    const timezoneOffsetMs = now.getTimezoneOffset() * 60 * 1000;
    return new Date(now.getTime() - timezoneOffsetMs).toISOString().slice(0, 10);
}

function initializeSprintSaveForm() {
    const startDateInput = document.getElementById('sprint-start-date');
    if (startDateInput && !startDateInput.value) {
        startDateInput.value = getTodayLocalDateValue();
    }
}

function clearSprintSelection() {
    selectedSprintStoryIds = new Set();
    renderSprintCandidates();
    updateSprintCapacityWarning();
}

function getSelectedSprintCandidates() {
    return sprintCandidates.filter(candidate => selectedSprintStoryIds.has(candidate.story_id));
}

function updateSprintSelectionSummary() {
    const summary = document.getElementById('sprint-selection-summary');
    if (!summary) return;

    if (sprintCandidates.length === 0) {
        summary.innerText = 'No sprint-eligible stories are available yet.';
        return;
    }

    const selected = getSelectedSprintCandidates();
    if (selected.length === 0) {
        summary.innerText = `${sprintCandidates.length} eligible stories available. Leave all unchecked to let the planner choose from the full candidate pool.`;
        return;
    }

    const estimatedPoints = selected
        .map(item => Number.isFinite(item.story_points) ? item.story_points : null)
        .filter(value => value !== null);
    const pointsText = estimatedPoints.length === selected.length
        ? `${estimatedPoints.reduce((sum, value) => sum + value, 0)} estimated points`
        : 'partial point estimates';

    summary.innerText = `${selected.length} stories manually selected (${pointsText}).`;
}

function renderSprintCandidates() {
    const container = document.getElementById('sprint-candidate-list');
    if (!container) return;

    container.innerHTML = '';
    if (!sprintCandidates || sprintCandidates.length === 0) {
        container.innerHTML = '<p class="text-xs text-slate-500">No refined TO_DO stories are available for sprint planning yet.</p>';
        updateSprintSelectionSummary();
        updateSprintCapacityWarning();
        return;
    }

    sprintCandidates.forEach((story) => {
        const checked = selectedSprintStoryIds.has(story.story_id);
        const points = Number.isFinite(story.story_points) ? `${story.story_points} pts` : 'Unestimated';
        const persona = story.persona ? `Persona: ${story.persona}` : 'Persona not set';
        const origin = story.story_origin ? `Origin: ${story.story_origin}` : 'Origin unknown';

        const row = document.createElement('label');
        row.className = 'flex cursor-pointer items-start gap-3 rounded-lg border border-slate-200 bg-white p-3 transition-colors hover:border-teal-300 dark:border-slate-700 dark:bg-slate-800/60 dark:hover:border-teal-700';
        row.innerHTML = `
            <input type="checkbox" class="mt-0.5 rounded border-slate-300 text-teal-600 focus:ring-teal-500" ${checked ? 'checked' : ''}>
            <div class="min-w-0 flex-1">
                <div class="flex items-center justify-between gap-3">
                    <p class="text-sm font-bold text-slate-800 dark:text-slate-200">${story.story_title}</p>
                    <span class="shrink-0 rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-black uppercase text-slate-600 dark:bg-slate-700 dark:text-slate-300">#${story.priority}</span>
                </div>
                <div class="mt-1 flex flex-wrap gap-2 text-[10px] text-slate-500">
                    <span>${points}</span>
                    <span>${persona}</span>
                    <span>${origin}</span>
                </div>
            </div>
        `;

        const checkbox = row.querySelector('input[type="checkbox"]');
        checkbox?.addEventListener('change', (event) => {
            if (event.target.checked) {
                selectedSprintStoryIds.add(story.story_id);
            } else {
                selectedSprintStoryIds.delete(story.story_id);
            }
            updateSprintSelectionSummary();
            updateSprintCapacityWarning();
        });

        container.appendChild(row);
    });

    updateSprintSelectionSummary();
    updateSprintCapacityWarning();
}

function updateSprintCapacityWarning() {
    const warning = document.getElementById('sprint-capacity-warning');
    if (!warning) return;

    const selected = getSelectedSprintCandidates();
    if (selected.length === 0) {
        warning.classList.add('hidden');
        warning.innerHTML = '';
        return;
    }

    const velocity = document.getElementById('sprint-velocity')?.value || 'Medium';
    const maxStoryPointsText = document.getElementById('sprint-max-story-points')?.value?.trim() || '';
    const maxStoryPoints = maxStoryPointsText ? parseInt(maxStoryPointsText, 10) : null;
    const messages = [];

    const storyLimit = SPRINT_VELOCITY_LIMITS[velocity] || SPRINT_VELOCITY_LIMITS.Medium;
    if (selected.length > storyLimit) {
        messages.push(`Capacity Overload: ${selected.length} manually selected stories exceed the ${velocity} heuristic limit of ${storyLimit}.`);
    }

    const allSelectedHavePoints = selected.every(item => Number.isFinite(item.story_points));
    if (maxStoryPoints && allSelectedHavePoints) {
        const totalPoints = selected.reduce((sum, item) => sum + item.story_points, 0);
        if (totalPoints > maxStoryPoints) {
            messages.push(`Capacity Overload: ${totalPoints} selected story points exceed the optional cap of ${maxStoryPoints}.`);
        }
    }

    if (messages.length === 0) {
        warning.classList.add('hidden');
        warning.innerHTML = '';
        return;
    }

    warning.classList.remove('hidden');
    warning.innerHTML = messages.join(' ');
}

async function loadSprintCandidates() {
    if (!selectedProjectId) return;
    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/sprint/candidates`);
        const data = await response.json();
        if (data.status !== 'success') throw new Error('Failed to load sprint candidates');

        const items = Array.isArray(data.data?.items) ? data.data.items : [];
        const validIds = new Set(items.map(item => item.story_id));
        selectedSprintStoryIds = new Set(
            Array.from(selectedSprintStoryIds).filter(storyId => validIds.has(storyId))
        );
        sprintCandidates = items;
        renderSprintCandidates();
    } catch (e) {
        console.error('Failed to load sprint candidates:', e);
        sprintCandidates = [];
        selectedSprintStoryIds = new Set();
        renderSprintCandidates();
    }
}

async function loadSprintHistory() {
    if (!selectedProjectId) return;
    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/sprint/history`);
        const data = await response.json();
        if (data.status !== 'success') throw new Error('Failed to load history');

        const items = Array.isArray(data.data?.items) ? data.data.items : [];
        sprintAttemptCount = items.length;
        renderSprintHistory(items);

        if (items.length > 0) {
            const latest = items[items.length - 1];
            latestSprintIsComplete = Boolean(latest.is_complete);
            renderSprintAttemptPanels(latest.input_context || null, latest.output_artifact || null);
        } else {
            latestSprintIsComplete = false;
            renderSprintAttemptPanels(null, null);
        }

        updateSprintSaveButton();
    } catch (e) {
        console.error('Failed to load sprint history:', e);
    }
}

async function generateSprintDraft() {
    if (!selectedProjectId) return;

    const userInput = document.getElementById('sprint-user-input')?.value?.trim() || '';
    const velocityInput = document.getElementById('sprint-velocity')?.value || 'Medium';
    const durationInput = document.getElementById('sprint-duration')?.value || 14;
    const maxPointsInput = document.getElementById('sprint-max-story-points')?.value?.trim() || '';
    const decomposeInput = document.getElementById('sprint-decompose')?.checked ?? true;
    const selectedStoryIds = Array.from(selectedSprintStoryIds);

    const button = document.getElementById('btn-generate-sprint');
    const original = button?.innerHTML;
    if (button) {
        button.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">cycle</span> Running...';
        button.disabled = true;
    }

    try {
        const payload = {
            user_input: userInput,
            team_velocity_assumption: velocityInput,
            sprint_duration_days: parseInt(durationInput, 10),
            max_story_points: maxPointsInput ? parseInt(maxPointsInput, 10) : null,
            include_task_decomposition: decomposeInput
        };
        if (selectedStoryIds.length > 0) {
            payload.selected_story_ids = selectedStoryIds;
        }

        const response = await fetch(`/api/projects/${selectedProjectId}/sprint/generate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });

        if (response.status >= 400) {
            const errorBody = await response.json();
            throw new Error(errorBody.detail || 'Sprint generation failed');
        }

        const data = await response.json();
        if (data.status !== 'success') throw new Error('Sprint generation failed');

        latestSprintIsComplete = Boolean(data.data?.is_complete);
        renderSprintAttemptPanels(data.data?.input_context || null, data.data?.output_artifact || null);
        setPhaseState(data.data?.fsm_state || 'SPRINT_SETUP', 'sprint');

        await loadSprintHistory();
    } catch (error) {
        console.error(error);
        const message = error?.message || 'Sprint generation failed.';
        alert(message);
    } finally {
        if (button) {
            button.innerHTML = original || '<span class="material-symbols-outlined text-sm">cycle</span> Plan Sprint';
            button.disabled = false;
        }
        updateSprintSaveButton();
    }
}

async function saveSprintDraft() {
    if (!selectedProjectId) return;

    const teamNameInput = document.getElementById('sprint-team-name');
    const startDateInput = document.getElementById('sprint-start-date');
    const teamName = teamNameInput?.value?.trim() || '';
    const sprintStartDate = startDateInput?.value || '';

    if (!teamName) {
        alert('Team name is required before saving the sprint.');
        teamNameInput?.focus();
        updateSprintSaveButton();
        return;
    }

    if (!sprintStartDate) {
        alert('Sprint start date is required before saving the sprint.');
        startDateInput?.focus();
        updateSprintSaveButton();
        return;
    }

    const button = document.getElementById('btn-save-sprint');
    const original = button?.innerHTML;
    if (button) {
        button.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">save</span> Saving...';
        button.disabled = true;
    }

    let success = false;
    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/sprint/save`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                team_name: teamName,
                sprint_start_date: sprintStartDate,
            }),
        });

        if (response.status >= 400) {
            const body = await response.json().catch(() => null);
            const detail = Array.isArray(body?.detail)
                ? body.detail.map(item => item?.msg || 'Validation error').join('; ')
                : body?.detail;
            throw new Error(detail || 'Failed to save sprint.');
        }

        const data = await response.json();
        if (data.status !== 'success') throw new Error('Failed to save sprint.');

        setPhaseState('SPRINT_PERSISTENCE', 'sprint');
        latestSprintIsComplete = true;
        currentSprintId = data.data?.save_result?.sprint_id ?? currentSprintId;
        sprintMode = 'planned';
        showSprintPlanner = false;
        success = true;

        await loadSavedSprints();
        await fetchProjectFSMState(selectedProjectId, { preserveView: true });
        if (currentSprintId) {
            await selectSavedSprintById(currentSprintId);
        }

        if (button) {
            button.innerHTML = '<span class="material-symbols-outlined text-sm">check_circle</span> Saved Successfully!';
            button.className = 'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-emerald-500 text-white font-bold transition-all shadow-md scale-105 ring-2 ring-emerald-200';
            setTimeout(() => { updateSprintSaveButton(); }, 3000);
        }

    } catch (error) {
        console.error(error);
        alert(error.message || 'Failed to save sprint plan.');
    } finally {
        if (!success) {
            if (button) button.innerHTML = original;
            updateSprintSaveButton();
        }
    }
}

function renderSprintHistory(items) {
    const container = document.getElementById('sprint-history-list');
    if (!container) return;

    container.innerHTML = '';
    if (!items || items.length === 0) {
        container.innerHTML = '<p class="text-xs text-slate-500">No attempts yet.</p>';
        return;
    }

    const reversed = [...items].reverse();
    reversed.forEach((item, index) => {
        const stamp = item.created_at || '-';
        const state = item.is_complete ? 'Complete' : 'Needs input';
        const color = item.is_complete ? 'text-emerald-600 bg-emerald-50 dark:bg-emerald-900/30 ring-emerald-200' : 'text-amber-600 bg-amber-50 dark:bg-amber-900/30 ring-amber-200';

        const row = document.createElement('div');
        row.className = 'border border-slate-200 dark:border-slate-700 rounded-lg p-3 bg-slate-50 dark:bg-slate-800/60 transition-transform';
        row.innerHTML = `
            <div class="flex items-center justify-between">
                <span class="text-xs font-extrabold text-slate-700 dark:text-slate-300">Attempt ${items.length - index}</span>
                <span class="text-[10px] uppercase ${color} px-2 py-0.5 rounded-full ring-1 ring-inset font-bold">${state}</span>
            </div>
            <p class="text-[10px] text-slate-400 mt-2">${stamp}</p>
        `;
        container.appendChild(row);
    });
}

function renderSprintAttemptPanels(inputContext, outputArtifact) {
    const inputCanvas = document.getElementById('sprint-input-context');
    const outputCanvas = document.getElementById('sprint-output-artifact');
    const copyBtn = document.getElementById('btn-copy-sprint-output');

    currentSprintInputContextJSON = inputContext || null;
    if (inputCanvas) {
        inputCanvas.innerText = inputContext ? JSON.stringify(inputContext, null, 2) : 'No input context available.';
    }

    if (outputCanvas) {
        if (!outputArtifact) {
            outputCanvas.innerHTML = `
                <div class="text-xs text-slate-500 flex flex-col items-center justify-center h-full gap-3 opacity-60">
                    <span class="material-symbols-outlined text-4xl">bolt</span>
                    <p>No sprint run yet.</p>
                </div>
            `;
        } else {
            outputCanvas.innerHTML = renderSprintArtifactHtml(outputArtifact, currentSprintInputContextJSON);
        }
    }
    
    currentSprintArtifactJSON = outputArtifact || null;
    if (copyBtn) {
        if (currentSprintArtifactJSON) {
            copyBtn.classList.remove('hidden');
        } else {
            copyBtn.classList.add('hidden');
        }
    }
}

function renderSprintValidationErrors(validationErrors) {
    const items = Array.isArray(validationErrors)
        ? validationErrors.filter((error) => typeof error === 'string' && error.trim().length > 0)
        : [];

    if (items.length === 0) return '';

    const escapeValidationText = (value) => String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');

    return `
        <div class="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-4 dark:border-amber-800 dark:bg-amber-900/20">
            <div class="flex items-center gap-2 text-amber-800 dark:text-amber-300 font-bold mb-2">
                <span class="material-symbols-outlined text-[18px]">rule</span>
                What to fix
            </div>
            <ul class="space-y-1.5 text-[11px] text-amber-900 dark:text-amber-200 list-disc list-inside">
                ${items.map((error) => `<li>${escapeValidationText(error.trim())}</li>`).join('')}
            </ul>
        </div>
    `;
}

function renderSprintArtifactHtml(artifact, inputContext) {
    let html = '';
    const availableStories = Array.isArray(inputContext?.available_stories) ? inputContext.available_stories : [];
    const storyMetaById = new Map(availableStories.map(story => [story.story_id, story]));

    if (artifact.error) {
        const rawOutput = artifact.raw_output || artifact.raw_output_preview || '';
        html += `
            <div class="bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 p-4 rounded-lg mb-4">
                <div class="flex items-center gap-2 text-red-700 dark:text-red-400 font-bold mb-2">
                    <span class="material-symbols-outlined">error</span> Generation Failed
                </div>
                <p class="text-[11px] font-mono text-red-600 dark:text-red-300 whitespace-pre-wrap">${artifact.message || 'Unknown error'}</p>
        `;
        html += renderSprintValidationErrors(artifact.validation_errors);
        if (rawOutput) {
            const safeRaw = rawOutput.replace(/</g, "&lt;").replace(/>/g, "&gt;");
            html += `
                <div class="mt-4 border-t border-red-200 dark:border-red-800/50 pt-3">
                    <p class="text-[10px] font-bold text-red-700 dark:text-red-400 mb-1 uppercase tracking-wide">Raw Agent Output Preview:</p>
                    <pre class="text-[10px] font-mono text-red-600 dark:text-red-300 bg-white/50 dark:bg-black/20 p-2 rounded overflow-x-auto whitespace-pre-wrap">${safeRaw}</pre>
                </div>
            `;
        }
        html += `</div>`;
    } else if (!artifact.is_complete && artifact.clarifying_questions?.length > 0) {
        html += `
            <div class="bg-amber-50 dark:bg-amber-900/30 border border-amber-200 dark:border-amber-700 p-4 rounded-lg mb-4">
                <div class="flex items-center gap-2 text-amber-700 dark:text-amber-400 font-bold mb-2">
                    <span class="material-symbols-outlined">help</span> Agent Needs Clarification
                </div>
                <ul class="text-[11px] text-amber-800 dark:text-amber-300 list-disc list-inside space-y-1">
                    ${artifact.clarifying_questions.map(q => `<li>${q}</li>`).join('')}
                </ul>
            </div>
        `;
    }

    if (!artifact.error && artifact.sprint_goal) {
        const capacity = artifact.capacity_analysis || {};
        html += `
            <div class="mb-5 bg-gradient-to-r from-teal-50 to-emerald-50 dark:from-teal-900/20 dark:to-emerald-900/20 border border-emerald-200 dark:border-emerald-800 p-4 rounded-xl">
                <h4 class="text-xs font-black text-emerald-800 dark:text-emerald-400 uppercase tracking-widest mb-2 flex items-center gap-1.5"><span class="material-symbols-outlined text-[16px]">flag_circle</span> Sprint Goal</h4>
                <p class="text-sm font-medium text-slate-800 dark:text-slate-200">${artifact.sprint_goal}</p>
                
                <div class="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-2 text-[11px] text-slate-600 dark:text-slate-400">
                    <div><span class="font-bold text-slate-700 dark:text-slate-300">Sprint #:</span> ${artifact.sprint_number || '?'}</div>
                    <div><span class="font-bold text-slate-700 dark:text-slate-300">Duration:</span> ${artifact.duration_days || '?'} days</div>
                    <div><span class="font-bold text-slate-700 dark:text-slate-300">Velocity:</span> ${capacity.velocity_assumption || '-'}</div>
                    <div><span class="font-bold text-slate-700 dark:text-slate-300">Capacity Band:</span> ${capacity.capacity_band || '-'}</div>
                    <div><span class="font-bold text-slate-700 dark:text-slate-300">Selected Stories:</span> ${capacity.selected_count ?? artifact.selected_stories?.length ?? 0}</div>
                    <div><span class="font-bold text-slate-700 dark:text-slate-300">Story Points Used:</span> ${capacity.story_points_used ?? 'N/A'}</div>
                </div>
                ${capacity.commitment_note ? `<p class="mt-3 text-[11px] font-medium text-emerald-800 dark:text-emerald-300">${capacity.commitment_note}</p>` : ''}
                ${capacity.reasoning ? `<p class="mt-2 text-[11px] text-slate-600 dark:text-slate-400">${capacity.reasoning}</p>` : ''}
            </div>
        `;
    }

    const stories = Array.isArray(artifact.selected_stories) ? artifact.selected_stories : [];
    if (stories.length === 0 && !artifact.error) {
        html += `<p class="text-xs text-slate-500 italic">No stories selected for sprint.</p>`;
    } else {
        html += `<div class="space-y-4">`;
        stories.forEach((story, idx) => {
            const storyMeta = storyMetaById.get(story.story_id) || {};
            const pointsLabel = Number.isFinite(storyMeta.story_points) ? `${storyMeta.story_points} Points` : 'Points N/A';
            const taskItems = Array.isArray(story.tasks) ? story.tasks : [];
            html += `
                <div class="border border-slate-200 dark:border-slate-700 rounded-lg p-4 bg-white dark:bg-slate-800/60 shadow-sm relative pt-4">
                    <div class="absolute top-0 right-0 bg-slate-100 dark:bg-slate-700 text-slate-500 text-[9px] font-black px-2 py-1 rounded-bl-lg rounded-tr-lg">STORY ${idx + 1}</div>
                    
                    <div class="flex gap-2 items-start justify-between mb-2 border-b border-slate-100 dark:border-slate-700 pb-2">
                        <h4 class="font-bold text-sm text-slate-800 dark:text-slate-200 pr-12">${story.story_title}</h4>
                        <span class="shrink-0 px-2 py-0.5 rounded bg-indigo-100 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-400 text-[10px] font-black uppercase">${pointsLabel}</span>
                    </div>
                    
                    <p class="text-[11px] text-slate-600 dark:text-slate-400 italic mb-3">${story.reason_for_selection || 'Selected for sprint scope.'}</p>
                    
                    <div>
                        <h5 class="text-[10px] uppercase font-bold text-slate-500 mb-1.5">Tasks (${taskItems.length})</h5>
                        ${taskItems.length > 0 ? `
                        <ul class="text-[11px] text-slate-700 dark:text-slate-300 space-y-3">
                            ${taskItems.map(task => {
                                const desc = escapeHtml(task?.description || task);
                                const isObj = typeof task === 'object' && task !== null;
                                const kindStr = isObj && task.task_kind ? escapeHtml(task.task_kind) : '';
                                const kind = kindStr ? `<span class="inline-flex items-center px-1.5 py-0.5 rounded-sm bg-slate-100 dark:bg-slate-700 text-[9px] font-black uppercase text-slate-600 dark:text-slate-300">${kindStr}</span>` : '';
                                const tags = isObj && Array.isArray(task.workstream_tags) ? task.workstream_tags.map(t => `<span class="inline-flex items-center bg-sky-50 dark:bg-sky-900/30 text-sky-700 dark:text-sky-300 px-1.5 py-0.5 rounded-sm text-[9px] font-bold border border-sky-100 dark:border-sky-800">#${escapeHtml(t)}</span>`).join(' ') : '';
                                const targetsStr = isObj && Array.isArray(task.artifact_targets) ? task.artifact_targets.map(escapeHtml).join(', ') : '';
                                const targets = targetsStr ? `<div class="text-[10px] text-slate-500 dark:text-slate-400 mt-1 flex gap-1.5"><span class="font-bold">Targets:</span> ${targetsStr}</div>` : '';
                                
                                return `<li class="flex flex-col rounded bg-slate-50 dark:bg-slate-900/50 p-2 border border-slate-200 dark:border-slate-700">
                                    <div class="flex items-start gap-2">
                                        <span class="material-symbols-outlined mt-0.5 text-[12px] text-emerald-600 shrink-0">check_circle</span>
                                        <div class="flex-1 min-w-0">
                                            <div class="text-slate-800 dark:text-slate-200 font-medium leading-snug">${desc}</div>
                                            <div class="flex flex-wrap items-center gap-1.5 mt-1.5">${kind}${tags}</div>
                                            ${targets}
                                        </div>
                                    </div>
                                </li>`;
                            }).join('')}
                        </ul>
                        ` : '<p class="text-[11px] text-slate-400">No tasks defined.</p>'}
                    </div>
                </div>
            `;
        });
        html += `</div>`;
    }

    const deselectedStories = Array.isArray(artifact.deselected_stories) ? artifact.deselected_stories : [];
    if (deselectedStories.length > 0) {
        html += `
            <div class="mt-6 pt-4 border-t border-slate-200 dark:border-slate-700">
                <h4 class="text-xs font-black text-slate-500 uppercase tracking-widest mb-3 flex items-center gap-1.5"><span class="material-symbols-outlined text-[14px]">inventory_2</span> Deselected Stories</h4>
                <div class="space-y-2">
        `;
        deselectedStories.forEach((story) => {
            const storyMeta = storyMetaById.get(story.story_id) || {};
            const title = storyMeta.story_title || `Story ${story.story_id}`;
            html += `
                <div class="text-[11px] bg-slate-100 dark:bg-slate-800/80 p-2 rounded border border-slate-200 dark:border-slate-700">
                    <span class="font-bold text-slate-700 dark:text-slate-300">${title}</span>
                    <p class="text-slate-500 mt-0.5">${story.reason}</p>
                </div>
            `;
        });
        html += `</div></div>`;
    }

    return html;
}

function updateSprintSaveButton() {
    const button = document.getElementById('btn-save-sprint');
    const hint = document.getElementById('sprint-save-hint');
    const teamNameInput = document.getElementById('sprint-team-name');
    const startDateInput = document.getElementById('sprint-start-date');
    if (!button || !hint || !teamNameInput || !startDateInput) return;

    const savePhaseReady = Boolean(selectedProjectId) && latestSprintIsComplete && activeFsmState === 'SPRINT_DRAFT';
    const hasRequiredFields = Boolean(teamNameInput.value.trim()) && Boolean(startDateInput.value);
    const canSave = savePhaseReady && hasRequiredFields;
    button.disabled = !canSave;
    teamNameInput.disabled = !savePhaseReady;
    startDateInput.disabled = !savePhaseReady;

    button.className = canSave
        ? 'inline-flex items-center gap-2 px-6 py-2.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-bold transition-all shadow-sm'
        : 'inline-flex items-center gap-2 px-6 py-2.5 rounded-lg bg-primary/40 text-white font-bold cursor-not-allowed transition-all shadow-sm';

    if (activeFsmState === 'SPRINT_PERSISTENCE') {
        hint.innerText = 'Sprint already saved for this draft.';
        return;
    }

    if (!latestSprintIsComplete) {
        hint.innerText = 'Save is disabled until the latest Sprint output is complete.';
        return;
    }

    if (!teamNameInput.value.trim()) {
        hint.innerText = 'Provide a team name to confirm this sprint.';
        return;
    }

    if (!startDateInput.value) {
        hint.innerText = 'Choose a sprint start date to confirm this sprint.';
        return;
    }

    hint.innerText = 'Sprint plan is complete. Proceed to save.';
}

async function deleteCurrentProject() {
    if (!selectedProjectId) return;

    if (!confirm('Are you sure you want to delete this project? This will permanently delete the specification, stories, and all AI generated data.')) {
        return;
    }

    const btn = document.getElementById('header-btn-delete-project');
    const original = btn?.innerHTML;
    if (btn) {
        btn.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">refresh</span>';
        btn.disabled = true;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}`, {
            method: 'DELETE',
        });
        const data = await response.json();
        if (data.status === 'success') {
            window.location.href = '/dashboard';
        } else {
            alert(data.detail || 'Failed to delete project.');
        }
    } catch (error) {
        console.error('Error deleting project:', error);
        alert('Network error while deleting project.');
    } finally {
        if (btn) {
            btn.innerHTML = original;
            btn.disabled = false;
        }
    }
}


function formatSafeDate(dateString) {
    if (!dateString) return '-';
    try {
        const d = new Date(dateString);
        return isNaN(d) ? dateString : d.toLocaleDateString();
    } catch {
        return dateString;
    }
}

async function copyArtifactToClipboard(phase) {
    let payload = null;
    let btnId = null;

    if (phase === 'vision') {
        payload = currentVisionArtifactJSON;
        btnId = 'btn-copy-vision-output';
    } else if (phase === 'backlog') {
        payload = currentBacklogArtifactJSON;
        btnId = 'btn-copy-backlog-output';
    } else if (phase === 'roadmap') {
        payload = currentRoadmapArtifactJSON;
        btnId = 'btn-copy-roadmap-output';
    } else if (phase === 'story') {
        payload = currentStoryArtifactJSON;
        btnId = 'btn-copy-story-output';
    } else if (phase === 'sprint') {
        payload = currentSprintArtifactJSON;
        btnId = 'btn-copy-sprint-output';
    }

    if (!payload) {
        console.warn(`No artifact payload available for phase: ${phase}`);
        return;
    }

    try {
        const jsonString = JSON.stringify(payload, null, 2);
        await navigator.clipboard.writeText(jsonString);

        const btn = document.getElementById(btnId);
        if (btn) {
            const originalHtml = btn.innerHTML;
            btn.innerHTML = `<span class="material-symbols-outlined text-[12px]">check</span> Copied!`;
            btn.classList.add('bg-emerald-100', 'text-emerald-700', 'border-emerald-200');
            btn.classList.remove('bg-white', 'text-slate-600', 'border-slate-200', 'dark:bg-slate-900', 'dark:text-slate-400');

            setTimeout(() => {
                btn.innerHTML = originalHtml;
                btn.classList.remove('bg-emerald-100', 'text-emerald-700', 'border-emerald-200');
                btn.classList.add('bg-white', 'text-slate-600', 'border-slate-200', 'dark:bg-slate-900', 'dark:text-slate-400');
            }, 2000);
        }
    } catch (err) {
        console.error('Failed to copy to clipboard', err);
        alert('Failed to copy to clipboard. Ensure you are using HTTPS or localhost.');
    }
}

// ==========================================
// TASK PACKET HANDLERS
// ==========================================

async function copyTaskPrompt(event, sprintId, taskId) {
    if (!selectedProjectId) return;
    const btn = event.currentTarget;
    const originalText = btn.innerHTML;
    
    try {
        btn.innerHTML = '<span class="material-symbols-outlined text-[12px] animate-spin">cycle</span> Fetching...';
        btn.disabled = true;

        const res = await fetch(`/api/projects/${selectedProjectId}/sprints/${sprintId}/tasks/${taskId}/packet?flavor=cursor`);
        if (!res.ok) throw new Error("Failed to fetch packet");
        
        const data = await res.json();
        const output = data.data?.render;
        
        if (!output) throw new Error("No rendered packet returned");
        
        await navigator.clipboard.writeText(output);
        
        btn.innerHTML = '<span class="material-symbols-outlined text-[12px]">check</span> Copied!';
        btn.classList.add('bg-emerald-50', 'text-emerald-700', 'border-emerald-200', 'dark:bg-emerald-900/30', 'dark:text-emerald-400', 'dark:border-emerald-800');
    } catch (err) {
        console.error("Copy Prompt Error:", err);
        btn.innerHTML = '<span class="material-symbols-outlined text-[12px]">error</span> Error';
    } finally {
        setTimeout(() => {
            btn.innerHTML = originalText;
            btn.className = 'inline-flex items-center gap-1 text-[10px] font-bold px-2 py-1 rounded bg-slate-100 hover:bg-slate-200 text-slate-600 transition-colors dark:bg-slate-800 dark:hover:bg-slate-700 dark:text-slate-300 border border-slate-200 dark:border-slate-700 shadow-sm';
            btn.disabled = false;
        }, 2000);
    }
}

async function copyStoryPrompt(event, sprintId, storyId) {
    if (!selectedProjectId) return;
    const btn = event.currentTarget;
    const originalText = btn.innerHTML;

    try {
        btn.innerHTML = '<span class="material-symbols-outlined text-[12px] animate-spin">cycle</span> Fetching...';
        btn.disabled = true;

        const res = await fetch(`/api/projects/${selectedProjectId}/sprints/${sprintId}/stories/${storyId}/packet?flavor=cursor`);
        if (!res.ok) throw new Error("Failed to fetch packet");

        const data = await res.json();
        const output = data.data?.render;

        if (!output) throw new Error("No rendered packet returned");

        await navigator.clipboard.writeText(output);

        btn.innerHTML = '<span class="material-symbols-outlined text-[12px]">check</span> Copied!';
        btn.classList.add('bg-emerald-50', 'text-emerald-700', 'border-emerald-200', 'dark:bg-emerald-900/30', 'dark:text-emerald-400', 'dark:border-emerald-800');
    } catch (err) {
        console.error("Copy Story Prompt Error:", err);
        btn.innerHTML = '<span class="material-symbols-outlined text-[12px]">error</span> Error';
    } finally {
        setTimeout(() => {
            btn.innerHTML = originalText;
            btn.className = 'inline-flex items-center gap-1 text-[10px] font-bold px-2 py-1 rounded bg-slate-100 hover:bg-slate-200 text-slate-600 transition-colors dark:bg-slate-800 dark:hover:bg-slate-700 dark:text-slate-300 border border-slate-200 dark:border-slate-700 shadow-sm';
            btn.disabled = false;
        }, 2000);
    }
}

async function toggleTaskBrief(event, sprintId, taskId) {
    if (!selectedProjectId) return;
    
    const containerItem = document.getElementById(`task-brief-${taskId}`);
    const loaderItem = document.getElementById(`task-brief-loading-${taskId}`);
    const contentItem = document.getElementById(`task-brief-content-${taskId}`);
    const btn = event.currentTarget;
    
    if (!containerItem.classList.contains('hidden')) {
        // Hide it
        containerItem.classList.add('hidden');
        btn.classList.remove('bg-sky-600', 'text-white', 'hover:bg-sky-700', 'dark:bg-sky-500', 'dark:text-white', 'dark:hover:bg-sky-600');
        btn.classList.add('bg-sky-50', 'text-sky-700', 'hover:bg-sky-100', 'dark:bg-sky-900/30', 'dark:hover:bg-sky-900/50', 'dark:text-sky-300');
        return;
    }
    
    // Show it
    containerItem.classList.remove('hidden');
    btn.classList.remove('bg-sky-50', 'text-sky-700', 'hover:bg-sky-100', 'dark:bg-sky-900/30', 'dark:hover:bg-sky-900/50', 'dark:text-sky-300');
    btn.classList.add('bg-sky-600', 'text-white', 'hover:bg-sky-700', 'dark:bg-sky-600', 'dark:text-white', 'dark:hover:bg-sky-500');
    
    // Only fetch if content is empty or resulted in prior error
    if (!contentItem.innerHTML.trim() || contentItem.dataset.error === 'true') {
        loaderItem.classList.remove('hidden');
        contentItem.dataset.error = 'false';
        try {
            const res = await fetch(`/api/projects/${selectedProjectId}/sprints/${sprintId}/tasks/${taskId}/packet?flavor=human`);
            if (!res.ok) throw new Error("Failed to fetch human brief");
            
            const data = await res.json();
            const output = data.data?.render;
            if (!output) throw new Error("No rendered packet returned");
            
            // Format markdown strictly as requested with proper regex
            let formattedHtml = output
                .replace(/^### (.*)/gm, '<h4 class="font-bold text-sm mt-3 mb-1 text-slate-800 dark:text-slate-200">$1</h4>')
                .replace(/^## (.*)/gm, '<h3 class="font-black text-sm uppercase tracking-wider mt-4 mb-2 text-slate-500 border-b border-slate-200 dark:border-slate-700 pb-1">$1</h3>')
                .replace(/^# (.*)/gm, '<h2 class="font-black text-lg mb-2 text-sky-700 dark:text-sky-400">$1</h2>')
                .replace(/\*\*(.*?)\*\*/g, '<strong class="font-black text-slate-800 dark:text-white">$1</strong>')
                .replace(/^> (.*)/gm, '<blockquote class="border-l-4 border-slate-300 dark:border-slate-600 pl-3 italic text-slate-600 dark:text-slate-400 my-2">$1</blockquote>');
            
            contentItem.innerHTML = formattedHtml;
        } catch (err) {
            console.error("View Brief Error:", err);
            contentItem.innerHTML = `<span class="text-rose-600 dark:text-rose-400"><span class="material-symbols-outlined text-[12px] relative top-0.5">error</span> Failed to load brief. (${err.message})</span>`;
            contentItem.dataset.error = 'true';
        } finally {
            loaderItem.classList.add('hidden');
        }
    }
}

// Assign globally for inline onclick handlers attached in project.html
window.retryProjectSetup = retryProjectSetup;
window.switchAuthorityReviewTab = switchAuthorityReviewTab;
window.loadAuthorityReview = loadAuthorityReview;
window.acceptAuthorityReview = acceptAuthorityReview;
window.rejectAuthorityReview = rejectAuthorityReview;
window.handleNextPhase = handleNextPhase;
window.generateVisionDraft = generateVisionDraft;
window.saveVisionDraft = saveVisionDraft;
window.generateBacklogDraft = generateBacklogDraft;
window.copyArtifactToClipboard = copyArtifactToClipboard;
window.saveBacklogDraft = saveBacklogDraft;
window.generateRoadmapDraft = generateRoadmapDraft;
window.saveRoadmapDraft = saveRoadmapDraft;
window.selectStoryRequirement = selectStoryRequirement;
window.generateStoryDraft = generateStoryDraft;
window.retryStoryDraft = retryStoryDraft;
window.saveStoryDraft = saveStoryDraft;
window.deleteStoryDraft = deleteStoryDraft;
window.completeStoryPhase = completeStoryPhase;
window.generateSprintDraft = generateSprintDraft;
window.saveSprintDraft = saveSprintDraft;
window.selectSavedSprintById = selectSavedSprintById;
window.openSprintPlanner = openSprintPlanner;
window.startCurrentSprint = startCurrentSprint;
window.navigateToOverview = navigateToOverview;
window.deleteCurrentProject = deleteCurrentProject;
window.copyTaskPrompt = copyTaskPrompt;
window.copyStoryPrompt = copyStoryPrompt;
window.toggleTaskBrief = toggleTaskBrief;

async function toggleTaskExecution(event, sprintId, taskId) {
    if (!selectedProjectId) return;
    
    const containerItem = document.getElementById(`task-execution-${taskId}`);
    const btn = event.currentTarget;
    
    if (!containerItem.classList.contains('hidden')) {
        containerItem.classList.add('hidden');
        btn.classList.remove('bg-indigo-600', 'text-white', 'hover:bg-indigo-700', 'dark:bg-indigo-500', 'dark:text-white', 'dark:hover:bg-indigo-600');
        btn.classList.add('bg-indigo-50', 'text-indigo-700', 'hover:bg-indigo-100', 'dark:bg-indigo-900/30', 'dark:hover:bg-indigo-900/50', 'dark:text-indigo-300');
        return;
    }
    
    containerItem.classList.remove('hidden');
    btn.classList.remove('bg-indigo-50', 'text-indigo-700', 'hover:bg-indigo-100', 'dark:bg-indigo-900/30', 'dark:hover:bg-indigo-900/50', 'dark:text-indigo-300');
    btn.classList.add('bg-indigo-600', 'text-white', 'hover:bg-indigo-700', 'dark:bg-indigo-600', 'dark:text-white', 'dark:hover:bg-indigo-500');
    
    containerItem.innerHTML = `<div class="flex items-center justify-center p-4"><span class="material-symbols-outlined animate-spin text-indigo-500 text-2xl">cycle</span></div>`;
    
    try {
        const res = await fetch(`/api/projects/${selectedProjectId}/sprints/${sprintId}/tasks/${taskId}/execution`);
        if (!res.ok) throw new Error("Failed to fetch execution history");
        
        const data = await res.json();
        
        let historyHtml = '';
        if (data.history && data.history.length > 0) {
            historyHtml = `<div class="flex flex-col gap-2 mt-4 pt-4 border-t border-indigo-200/50 dark:border-indigo-800/50">
                <h5 class="text-[10px] font-black uppercase tracking-wider text-indigo-400">Execution History</h5>
                <ul class="space-y-2">
                    ${data.history.map(entry => {
                        const dateStr = new Date(entry.changed_at).toLocaleString();
                        return `<li class="text-[11px] bg-white dark:bg-slate-900 p-2 rounded shadow-sm flex flex-col gap-1">
                            <span class="font-bold text-slate-700 dark:text-slate-300">${escapeHtml(entry.changed_by)} &rarr; ${escapeHtml(entry.new_status)} <span class="text-slate-400 font-normal">(${dateStr})</span></span>
                            ${entry.outcome_summary ? `<span class="italic text-slate-600">${escapeHtml(entry.outcome_summary)}</span>` : ''}
                        </li>`;
                    }).join('')}
                </ul>
            </div>`;
        }

        containerItem.innerHTML = `
            <div class="flex items-center justify-between">
                <h4 class="text-xs font-bold text-indigo-900 dark:text-indigo-200">Log Task Execution</h4>
                <div class="text-[10px] uppercase font-bold text-indigo-500">Current: ${data.current_status}</div>
            </div>
            <form onsubmit="submitTaskExecution(event, ${sprintId}, ${taskId})" class="flex flex-col gap-3">
                <div class="flex gap-2">
                    <label class="flex-1 text-[11px] font-bold text-slate-700 dark:text-slate-300 flex flex-col gap-1">
                        New Status
                        <select id="task-exc-status-${taskId}" required onchange="toggleTaskExecutionFields(${taskId})" class="p-1.5 rounded form-select text-xs dark:bg-slate-800 dark:border-slate-700 focus:ring-1 focus:ring-indigo-500">
                            <option value="To Do" ${data.current_status === 'To Do' ? 'selected' : ''}>To Do</option>
                            <option value="In Progress" ${data.current_status === 'In Progress' ? 'selected' : ''}>In Progress</option>
                            <option value="Done" ${data.current_status === 'Done' ? 'selected' : ''}>Done</option>
                            <option value="Cancelled" ${data.current_status === 'Cancelled' ? 'selected' : ''}>Cancelled</option>
                        </select>
                    </label>
                    <label class="flex-1 text-[11px] font-bold text-slate-700 dark:text-slate-300 flex flex-col gap-1">
                        Checklist Result
                        <select id="task-exc-acceptance-${taskId}" required class="p-1.5 rounded form-select text-xs dark:bg-slate-800 dark:border-slate-700 focus:ring-1 focus:ring-indigo-500">
                            <option value="not_checked" ${data.latest_entry?.acceptance_result === 'not_checked' ? 'selected' : ''}>Not Checked</option>
                            <option value="partially_met" ${data.latest_entry?.acceptance_result === 'partially_met' ? 'selected' : ''}>Partially Met</option>
                            <option value="fully_met" ${data.latest_entry?.acceptance_result === 'fully_met' ? 'selected' : ''}>Fully Met</option>
                        </select>
                    </label>
                </div>
                
                <label class="text-[11px] font-bold text-slate-700 dark:text-slate-300 flex flex-col gap-1">
                    Outcome Summary <span class="font-normal text-slate-500">(Required if Done)</span>
                    <input type="text" id="task-exc-summary-${taskId}" placeholder="e.g. Completed frontend mockup" value="${escapeHtml(data.latest_entry?.outcome_summary || '')}" class="p-1.5 rounded form-input text-xs dark:bg-slate-800 dark:border-slate-700">
                </label>
                
                <label class="text-[11px] font-bold text-slate-700 dark:text-slate-300 flex flex-col gap-1">
                    Artifact Refs <span class="font-normal text-slate-500">(Comma separated)</span>
                    <input type="text" id="task-exc-artifacts-${taskId}" placeholder="e.g. file.txt" value="${escapeHtml(data.latest_entry?.artifact_refs?.join(', ') || '')}" class="p-1.5 rounded form-input text-xs dark:bg-slate-800 dark:border-slate-700">
                </label>

                <div class="flex justify-end gap-2 mt-1">
                    <button type="button" onclick="const btn = document.querySelector('#task-execution-${taskId}').previousElementSibling.querySelector('button:last-child'); toggleTaskExecution({currentTarget: btn}, ${sprintId}, ${taskId})" class="px-3 py-1.5 text-xs font-bold text-slate-600 hover:bg-slate-200 rounded dark:text-slate-300 dark:hover:bg-slate-700 transition">Cancel</button>
                    <button type="submit" id="task-exc-submit-${taskId}" class="px-3 py-1.5 text-xs font-bold text-white bg-indigo-600 hover:bg-indigo-700 rounded transition flex items-center gap-1">
                        <span class="material-symbols-outlined text-[14px]">save</span> Save Log
                    </button>
                </div>
            </form>
            <div id="task-exc-error-${taskId}" class="hidden text-xs text-rose-600 font-bold mt-2"></div>
            ${historyHtml}
        `;
        
        toggleTaskExecutionFields(taskId);
        
    } catch (err) {
        console.error("View Execution Error:", err);
        containerItem.innerHTML = `<span class="text-rose-600 dark:text-rose-400 p-4"><span class="material-symbols-outlined text-[12px] relative top-0.5">error</span> Failed to load execution history. (${err.message})</span>`;
    }
}

function toggleTaskExecutionFields(taskId) {
    const statusSel = document.getElementById(`task-exc-status-${taskId}`);
    const summaryInp = document.getElementById(`task-exc-summary-${taskId}`);
    if (statusSel && summaryInp) {
        if (statusSel.value === 'Done') {
            summaryInp.required = true;
        } else {
            summaryInp.required = false;
        }
    }
}

async function submitTaskExecution(event, sprintId, taskId) {
    event.preventDefault();
    if (!selectedProjectId) return;
    
    const submitBtn = document.getElementById(`task-exc-submit-${taskId}`);
    const errCont = document.getElementById(`task-exc-error-${taskId}`);
    
    const newStatus = document.getElementById(`task-exc-status-${taskId}`).value;
    const acceptResult = document.getElementById(`task-exc-acceptance-${taskId}`).value;
    const summary = document.getElementById(`task-exc-summary-${taskId}`).value;
    const artifactsRaw = document.getElementById(`task-exc-artifacts-${taskId}`).value;
    
    const artifactRefs = artifactsRaw ? artifactsRaw.split(',').map(s => s.trim()).filter(s => s.length > 0) : [];
    
    if (newStatus === 'Done' && !summary.trim()) {
        errCont.innerText = "Outcome summary is required when marking Done.";
        errCont.classList.remove('hidden');
        return;
    }
    
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<span class="material-symbols-outlined text-[14px] animate-spin">cycle</span> Saving...';
    errCont.classList.add('hidden');
    
    try {
        const payload = {
            new_status: newStatus,
            acceptance_result: acceptResult,
            outcome_summary: summary.trim() || null,
            artifact_refs: artifactRefs.length > 0 ? artifactRefs : null,
            notes: "Manual UI update"
        };
        
        const res = await fetch(`/api/projects/${selectedProjectId}/sprints/${sprintId}/tasks/${taskId}/execution`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail?.map(d => d.msg).join(' ') || data.detail || "Failed to save execution");
        await fetchProjectFSMState(selectedProjectId, { preserveView: true });
        await loadSavedSprints();
        await selectSavedSprintById(sprintId);
    } catch (err) {
        console.error(err);
        errCont.innerText = err.message;
        errCont.classList.remove('hidden');
        submitBtn.disabled = false;
        submitBtn.innerHTML = '<span class="material-symbols-outlined text-[14px]">save</span> Save Log';
    }
}

window.toggleTaskExecution = toggleTaskExecution;
window.submitTaskExecution = submitTaskExecution;

async function toggleStoryClose(event, sprintId, storyId) {
    if (!selectedProjectId) return;
    
    const containerItem = document.getElementById(`story-close-${storyId}`);
    if (!containerItem) return;
    
    const btn = event.currentTarget;
    const isShowing = !containerItem.classList.contains('hidden');
    
    if (isShowing) {
        containerItem.classList.add('hidden');
        return;
    }
    
    containerItem.classList.remove('hidden');
    containerItem.innerHTML = `<div class="flex items-center justify-center p-4"><span class="material-symbols-outlined animate-spin text-emerald-500 text-2xl">cycle</span></div>`;
    
    try {
        const res = await fetch(`/api/projects/${selectedProjectId}/sprints/${sprintId}/stories/${storyId}/close`);
        const data = await res.json();
        
        if (!res.ok) throw new Error(data.detail || "Failed to fetch close data");
        
        const isDone = data.current_status === 'Done';
        
        if (isDone) {
            containerItem.innerHTML = `
                <div class="flex items-center justify-between border-b border-emerald-200/50 pb-2 mb-2 dark:border-emerald-800/50">
                    <h4 class="text-xs font-bold text-emerald-900 dark:text-emerald-200">Story Completion Record</h4>
                    <span class="text-[10px] uppercase font-bold text-emerald-500 bg-emerald-100 dark:bg-emerald-900/50 px-2 py-0.5 rounded">${data.resolution || 'Completed'}</span>
                </div>
                <div class="flex flex-col gap-3 text-xs text-slate-700 dark:text-slate-300">
                    <div>
                        <span class="font-bold text-slate-500 uppercase tracking-wider text-[10px]">Completion Notes</span>
                        <p class="mt-1 whitespace-pre-wrap">${escapeHtml(data.completion_notes || 'No notes provided.')}</p>
                    </div>
                    ${data.evidence_links ? `
                    <div>
                        <span class="font-bold text-slate-500 uppercase tracking-wider text-[10px]">Evidence / Artifacts</span>
                        <p class="mt-1 font-mono text-[10px] bg-white dark:bg-slate-900 p-2 rounded shadow-sm">${escapeHtml(data.evidence_links)}</p>
                    </div>` : ''}
                </div>
                <div class="flex justify-end gap-2 mt-4 pt-2 border-t border-emerald-200/50 dark:border-emerald-800/50">
                    <button type="button" onclick="document.getElementById('story-close-${storyId}').classList.add('hidden')" class="px-3 py-1.5 text-xs font-bold text-slate-600 hover:bg-slate-200 rounded dark:text-slate-300 dark:hover:bg-slate-700 transition">Close Panel</button>
                </div>
            `;
            return;
        }

        containerItem.innerHTML = `
            <div class="flex items-center justify-between border-b border-emerald-200/50 pb-2 mb-2 dark:border-emerald-800/50">
                <h4 class="text-xs font-bold text-emerald-900 dark:text-emerald-200">Manual Story Close</h4>
                <div class="text-[10px] font-bold text-emerald-600 dark:text-emerald-400 text-right">
                    ${data.readiness.done_tasks}/${data.readiness.total_tasks} tasks done
                </div>
            </div>
            
            <form onsubmit="submitStoryClose(event, ${sprintId}, ${storyId})" class="flex flex-col gap-3">
                <label class="text-[11px] font-bold text-slate-700 dark:text-slate-300 flex flex-col gap-1">
                    Resolution Status
                    <select id="story-close-res-${storyId}" required class="p-1.5 rounded form-select text-xs dark:bg-slate-800 dark:border-slate-700 focus:ring-1 focus:ring-emerald-500">
                        <option value="Completed">Completed</option>
                        <option value="Completed with AC changes">Completed (with AC changes)</option>
                        <option value="Partial">Partial Drop</option>
                        <option value="Won't Do">Won't Do / Cancelled</option>
                    </select>
                </label>
                
                <label class="text-[11px] font-bold text-slate-700 dark:text-slate-300 flex flex-col gap-1">
                    Completion Summary / Delivered Features <span class="text-rose-500">*</span>
                    <textarea id="story-close-notes-${storyId}" required placeholder="Describe what was delivered..." class="p-2 rounded form-textarea text-xs dark:bg-slate-800 dark:border-slate-700 h-16"></textarea>
                </label>
                
                <label class="text-[11px] font-bold text-slate-700 dark:text-slate-300 flex flex-col gap-1">
                    Evidence Links / Commits / PRs <span class="font-normal text-slate-500">(Comma separated)</span>
                    <input type="text" id="story-close-evidence-${storyId}" placeholder="e.g. github.com/pull/123" class="p-1.5 rounded form-input text-xs dark:bg-slate-800 dark:border-slate-700">
                </label>
                
                <div class="grid grid-cols-2 gap-3">
                    <label class="text-[11px] font-bold text-slate-700 dark:text-slate-300 flex flex-col gap-1">
                        Known Gaps <span class="font-normal text-slate-500">(Optional)</span>
                        <textarea id="story-close-gaps-${storyId}" placeholder="Any bugs or edge cases..." class="p-2 rounded form-textarea text-xs dark:bg-slate-800 dark:border-slate-700 h-12"></textarea>
                    </label>
                    <label class="text-[11px] font-bold text-slate-700 dark:text-slate-300 flex flex-col gap-1">
                        Follow-up Notes <span class="font-normal text-slate-500">(Optional)</span>
                        <textarea id="story-close-followups-${storyId}" placeholder="What to do next..." class="p-2 rounded form-textarea text-xs dark:bg-slate-800 dark:border-slate-700 h-12"></textarea>
                    </label>
                </div>

                <div class="flex justify-end gap-2 mt-2 pt-2 border-t border-emerald-200/50 dark:border-emerald-800/50">
                    <button type="button" onclick="document.getElementById('story-close-${storyId}').classList.add('hidden')" class="px-3 py-1.5 text-xs font-bold text-slate-600 hover:bg-slate-200 rounded dark:text-slate-300 dark:hover:bg-slate-700 transition">Cancel</button>
                    <button type="submit" id="story-close-submit-${storyId}" class="px-3 py-1.5 text-xs font-bold text-white bg-emerald-600 hover:bg-emerald-700 rounded transition flex items-center gap-1 shadow-sm">
                        <span class="material-symbols-outlined text-[14px]">task_alt</span> Confirm Close
                    </button>
                </div>
            </form>
            <div id="story-close-error-${storyId}" class="hidden text-xs text-rose-600 font-bold mt-2"></div>
        `;
        
    } catch (err) {
        console.error("View Story Close Error:", err);
        containerItem.innerHTML = `<span class="text-rose-600 dark:text-rose-400 p-4"><span class="material-symbols-outlined text-[12px] relative top-0.5">error</span> Failed to load close panel. (${err.message})</span>`;
    }
}

async function submitStoryClose(event, sprintId, storyId) {
    event.preventDefault();
    if (!selectedProjectId) return;
    
    const submitBtn = document.getElementById(`story-close-submit-${storyId}`);
    const errCont = document.getElementById(`story-close-error-${storyId}`);
    
    const resValue = document.getElementById(`story-close-res-${storyId}`).value;
    const notesValue = document.getElementById(`story-close-notes-${storyId}`).value;
    const evidenceRaw = document.getElementById(`story-close-evidence-${storyId}`).value;
    const gapsValue = document.getElementById(`story-close-gaps-${storyId}`).value;
    const followupsValue = document.getElementById(`story-close-followups-${storyId}`).value;
    
    const evidenceLinks = evidenceRaw ? evidenceRaw.split(',').map(s => s.trim()).filter(s => s.length > 0) : [];
    
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<span class="material-symbols-outlined text-[14px] animate-spin">cycle</span> Closing...';
    errCont.classList.add('hidden');
    
    try {
        const payload = {
            resolution: resValue,
            completion_notes: notesValue,
            evidence_links: evidenceLinks.length > 0 ? evidenceLinks : null,
            known_gaps: gapsValue.trim() || null,
            follow_up_notes: followupsValue.trim() || null,
            changed_by: "manual-ui"
        };
        
        const res = await fetch(`/api/projects/${selectedProjectId}/sprints/${sprintId}/stories/${storyId}/close`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail?.map ? data.detail.map(d => d.msg).join(' ') : data.detail || "Failed to close story");
        
        await fetchProjectFSMState(selectedProjectId, { preserveView: true });
        await loadSavedSprints();
        await selectSavedSprintById(sprintId);
    } catch (err) {
        console.error(err);
        errCont.innerText = err.message;
        errCont.classList.remove('hidden');
        submitBtn.disabled = false;
        submitBtn.innerHTML = '<span class="material-symbols-outlined text-[14px]">task_alt</span> Confirm Close';
    }
}

window.toggleStoryClose = toggleStoryClose;
window.submitStoryClose = submitStoryClose;

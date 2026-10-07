export function dashboardBundle(overrides = {}) {
    const names = [
        'project', 'position', 'vision', 'goal', 'specification', 'repository',
        'backlogReview', 'roadmapReview', 'acceptedRoadmap', 'storyReviews',
        'sprintPlanReview', 'storyPending', 'storyDependencies', 'sprintCandidates',
        'sprintStatusResponse', 'sprintHistory',
    ];
    return {
        status: 'success',
        data: {
            ...Object.fromEntries(names.map((name) => [name, { status: 200, body: { data: {} } }])),
            ...overrides,
        },
    };
}

export function sourceOnlyLifecycleState({ projectId = 7 } = {}) {
    const source = {
        schema_version: 'agileforge.specification-source.v1',
        specification_source_id: 31, source_fingerprint: 'sha256:source-31',
        producer_capability: 'to-spec', preparation_capability: 'grill-with-docs',
        source: { source_id: 'requirements', relative_path: 'requirements.md', byte_length: 100, content_fingerprint: 'sha256:requirements' },
        context: { state: 'absent', document: null }, adrs: [],
        repository: { repository_binding_id: 11, head_sha: 'a'.repeat(40), branch_name: null, detached_head: true, dirty: false, status_fingerprint: 'sha256:clean', probe_version: 'agileforge.repository-probe.v1' },
        accepted_vision: { vision_artifact_id: 12, fingerprint: 'sha256:vision-12' },
        active_product_goal: { product_goal_artifact_id: 21, fingerprint: 'sha256:goal-21' },
        supersedes_specification_source_id: null, supersedes_source_fingerprint: null,
        registered_by: 'test-operator', registered_at: '2026-10-06T12:00:00Z',
    };
    const decision = (nodeId, requestKind, category, recommendationKind, reasonCode) => ({
        node_id: nodeId, request_kind: requestKind, category,
        recommendation_kind: recommendationKind, reason_code: reasonCode,
        instance_key: null, decision_fingerprint: `sha256:${nodeId}`, fact_references: [],
    });
    return {
        project: { id: projectId, name: 'Lifecycle fixture', description: 'Provider-free lifecycle summary.' },
        position: { project_id: projectId, decisions: [
            decision('specification.source', 'register_specification_source', 'available', 'optional_reentry', 'SPECIFICATION_SOURCE_REPLACEMENT_AVAILABLE'),
            decision('specification.structure', 'structure_specification', 'available', 'required', 'SPECIFICATION_STRUCTURING_REQUIRED'),
            decision('backlog.generate', 'record_backlog_draft', 'blocked', 'required', 'SPECIFICATION_NOT_APPROVED'),
            decision('roadmap.generate', 'record_roadmap_draft', 'blocked', 'required', 'BACKLOG_NOT_ACCEPTED'),
        ] },
        actions: [{ node_id: 'specification.structure', request_kind: 'structure_specification', endpoint: 'specification/structure', instance_key: null, availability: 'available' }],
        vision: { bootstrap_available: false, current: { statement: 'Accepted direction' }, draft: null, transcript: [], candidate: null, review: { state: 'accepted', rationale: 'Accepted for this fixture.' }, stale_reason: null },
        goal: {
            accepted_vision: { statement: 'Accepted direction' },
            active: { product_goal_artifact_id: 21, fingerprint: 'sha256:goal-21', statement: 'Deliver a traceable outcome.', goal_number: 1, revision_number: 1 },
            transcript: [], latest_questions: [], effective_questions: null,
            candidate: null, review: { state: 'accepted', rationale: 'Accepted for this fixture.' }, outcome: null, stale_reason: null,
        },
        specification: { schema_version: 'agileforge.specification_review.v2', source, candidate: null, review: null, stale_reason: 'SPECIFICATION_NOT_STRUCTURED' },
        repository: { repository: null },
        planningReviews: { backlog: {}, roadmap: {}, stories: { items: [] }, sprintPlan: {} },
        acceptedRoadmap: { kind: 'ready', data: { project_id: projectId, state: 'absent', roadmap: null, acceptance: null, progress: null } },
        storyPending: { project_id: projectId, accepted_backlog: null, items: [], count: 0, pending_count: 0 },
        storyDependencies: { project_id: projectId, stories: [], dependencies: [] },
        sprintCandidates: { project_id: projectId, items: [], sprint_owner: null },
        sprintStatus: { kind: 'absent' }, sprintHistory: { project_id: projectId, items: [], execution_attempts: [] },
    };
}

export function acceptedLifecycleState({ projectId = 7 } = {}) {
    const state = sourceOnlyLifecycleState({ projectId });
    state.position.decisions = [];
    state.actions = [];
    state.specification.candidate = {
        specification_candidate_id: 41, candidate_fingerprint: 'sha256:candidate-41',
        specification_source_id: 31, registered_source_fingerprint: 'sha256:source-31',
        canonical_payload: { requirements: [] }, rendered_markdown: 'Accepted requirements.',
        decision_state: 'accepted', supersedes_specification_candidate_id: null,
        supersedes_candidate_fingerprint: null,
    };
    state.specification.review = { state: 'accepted', specification_decision_id: 42, decision: 'accepted', rationale: 'Exact candidate accepted.', reviewer: 'test-operator' };
    state.specification.stale_reason = 'CANDIDATE_REVIEWED';
    state.storyPending.accepted_backlog = { backlog_artifact_id: 51, artifact_fingerprint: 'sha256:backlog-51' };
    state.acceptedRoadmap.data = {
        project_id: projectId, state: 'accepted',
        roadmap: { roadmap_artifact_id: 61, artifact_fingerprint: 'sha256:roadmap-61', roadmap_summary: 'Traceable delivery.', roadmap_releases: [], is_complete: true, clarifying_questions: [] },
        acceptance: { rationale: 'Exact Roadmap accepted.', reviewer: 'test-operator', decided_at: '2026-10-06T12:00:00Z' },
        progress: { state: 'available', active_sprint: null, milestones: [] },
    };
    return state;
}

export function activeSprintLifecycleState({ projectId = 7 } = {}) {
    const state = acceptedLifecycleState({ projectId });
    const fingerprint = `sha256:${'a'.repeat(64)}`;
    const ownerKey = `agileforge:sprint-owner:solo-project:v1:project:${projectId}`;
    const ownerDisplay = `Solo operator for Project ${projectId}`;
    const plan = {
        sprint_id: 71, status: 'active', goal: 'Deliver the accepted slice.',
        sprint_plan_artifact_id: 72, sprint_plan_artifact_decision_id: 73,
        plan_fingerprint: fingerprint, candidate_set_fingerprint: fingerprint, task_content_fingerprint: fingerprint,
        owner: { kind: 'solo_project', key: ownerKey, label: `[${ownerKey}] ${ownerDisplay}`, display_label: ownerDisplay },
        acceptance: { rationale: 'Accepted Sprint scope.', reviewer: 'test-operator', decided_at: '2026-10-06T12:00:00Z' },
        selected_stories: [{ story_id: 81, story_item_id: 'US-81', title: 'Traceable delivery', story_points: 1, task_count: 1 }],
        total_points: 1, task_count: 1,
    };
    state.sprintStatus = { kind: 'ready', data: {
        project_id: projectId, sprint: { sprint_id: 71, status: 'active' }, effective_status: 'active',
        accepted_plan: plan, current_retry: null, original_triage: [],
        start: { sprint_id: 71, sprint_plan_artifact_id: 72, sprint_plan_artifact_decision_id: 73, plan_fingerprint: fingerprint, candidate_set_fingerprint: fingerprint, task_content_fingerprint: fingerprint },
        tasks: [{ task_id: 91, story_id: 81, sprint_id: 71, description: 'Retain exact evidence.', status: 'To Do', fact_fingerprint: fingerprint }],
        stories: [{ story_id: 81 }], story_completions: [],
    } };
    return state;
}

function lifecycleDashboardBundle(state) {
    const slot = (data) => ({ status: 200, body: { status: 'success', data } });
    return dashboardBundle({
        ...Object.fromEntries(['project', 'vision', 'goal', 'specification', 'repository', 'storyPending', 'storyDependencies', 'sprintCandidates', 'sprintHistory'].map((name) => [name, slot(state[name])])),
        position: { status: 200, body: { status: 'success', data: state.position, actions: state.actions } },
        backlogReview: slot(state.planningReviews.backlog), roadmapReview: slot(state.planningReviews.roadmap),
        storyReviews: slot(state.planningReviews.stories), sprintPlanReview: slot(state.planningReviews.sprintPlan),
        acceptedRoadmap: slot(state.acceptedRoadmap.data),
        sprintStatusResponse: state.sprintStatus.kind === 'absent'
            ? { status: 404, body: { code: 'SPRINT_NOT_FOUND', message: 'No Sprint has been started.' } }
            : slot(state.sprintStatus.data),
    });
}

export function sourceOnlyDashboardBundle(options = {}) {
    return lifecycleDashboardBundle(sourceOnlyLifecycleState(options));
}

export function acceptedDashboardBundle(options = {}) {
    return lifecycleDashboardBundle(acceptedLifecycleState(options));
}

"""Pure optional scope-extension workflow rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from workflow.contracts import (
    GRAPH_VERSION,
    Blocker,
    FactReference,
    InputField,
    RecommendationKind,
)
from workflow.definitions.authority import AUTHORITY_NODES, accepted_current_authority
from workflow.definitions.planning import (
    readiness_fingerprint,
    story_dependency_source_fingerprint,
)
from workflow.fingerprints import canonical_hash
from workflow.graph import (
    ChildGraphSpec,
    NodeSpec,
    RuleCategory,
    RuleEvaluation,
    WorkflowGraph,
)
from workflow.planning_integrity import (
    active_dependency_review_edges,
    dependency_review_fingerprint,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import (
        AuthorityFact,
        DiscoveryRunFact,
        PhaseArtifactFact,
        PlanningArtifactFact,
        PrdVersionFact,
        ReviewDecisionFact,
        ScopeExtensionRegistrationFact,
        SpecDraftFact,
        StoryFact,
        WorkflowFactSnapshot,
    )


@dataclass(frozen=True)
class _RunState:
    """Validated unresolved extension facts."""

    run: DiscoveryRunFact | None
    conflict: bool


@dataclass(frozen=True)
class _DownstreamEvidence:
    """Exact current downstream facts or one closed prerequisite problem."""

    references: tuple[FactReference, ...] = ()
    blocker: tuple[str, str] | None = None
    conflict: bool = False


def _evaluation(
    category: RuleCategory,
    reason_code: str,
    *,
    instance_key: str | None = None,
    references: tuple[FactReference, ...] = (),
    blockers: tuple[Blocker, ...] = (),
) -> tuple[RuleEvaluation, ...]:
    return (
        RuleEvaluation(
            category=category,
            reason_code=reason_code,
            instance_key=instance_key,
            fact_references=references,
            blockers=blockers,
        ),
    )


def _recovery_evaluation(
    reason_code: str,
    instance_key: str,
    references: tuple[FactReference, ...],
) -> tuple[RuleEvaluation, ...]:
    return (
        RuleEvaluation(
            category=RuleCategory.AVAILABLE,
            reason_code=reason_code,
            instance_key=instance_key,
            fact_references=references,
            recommendation_kind=RecommendationKind.RECOVERY,
        ),
    )


def _invalid(instance_key: str | None = None) -> tuple[RuleEvaluation, ...]:
    return _evaluation(
        RuleCategory.INVALID,
        "WORKFLOW_FACT_CONFLICT",
        instance_key=instance_key,
    )


def _blocked(
    code: str,
    message: str,
    *,
    instance_key: str | None = None,
) -> tuple[RuleEvaluation, ...]:
    return _evaluation(
        RuleCategory.BLOCKED,
        code,
        instance_key=instance_key,
        blockers=(Blocker(code=code, message=message),),
    )


def _reference(fact_type: str, fact_id: int, fingerprint: str) -> FactReference:
    return FactReference(
        fact_type=fact_type,
        fact_id=str(fact_id),
        fingerprint=fingerprint,
    )


def _run_state(snapshot: WorkflowFactSnapshot) -> _RunState:
    extensions = tuple(
        item for item in snapshot.discovery_runs if item.purpose == "extension"
    )
    open_runs = tuple(item for item in extensions if item.closed_at is None)
    abandonments = {
        item.discovery_run_id for item in snapshot.discovery_run_abandonments
    }
    run_ids = {item.discovery_run_id for item in extensions}
    conflict = (
        len(open_runs) > 1
        or not abandonments <= run_ids
        or any(
            item.base_spec_version_id is None or item.base_spec_hash is None
            for item in extensions
        )
        or any(
            item.discovery_run_id in abandonments and item.closed_at is None
            for item in extensions
        )
    )
    return _RunState(open_runs[0] if len(open_runs) == 1 else None, conflict)


def _instance(run: DiscoveryRunFact) -> str:
    return f"run:{run.discovery_run_id}"


def _review_for(
    snapshot: WorkflowFactSnapshot,
    artifact_type: str,
    artifact_id: int,
    fingerprint: str,
) -> tuple[ReviewDecisionFact | None, bool]:
    rows = tuple(
        item
        for item in snapshot.review_decisions
        if item.artifact_type == artifact_type and item.artifact_id == artifact_id
    )
    if len(rows) > 1:
        return None, True
    if rows and rows[0].artifact_fingerprint != fingerprint:
        return None, True
    return (rows[0] if rows else None), False


def _active_prd(
    snapshot: WorkflowFactSnapshot,
    run: DiscoveryRunFact,
) -> tuple[PrdVersionFact | None, bool]:
    rows = tuple(
        item
        for item in snapshot.prd_versions
        if item.discovery_run_id == run.discovery_run_id
    )
    ids = {item.prd_version_id for item in rows}
    parents = {item.supersedes_id for item in rows if item.supersedes_id is not None}
    leaves = tuple(item for item in rows if item.prd_version_id not in parents)
    conflict = not parents <= ids or (bool(rows) and len(leaves) != 1)
    return (leaves[0] if len(leaves) == 1 else None), conflict


def _active_spec(
    snapshot: WorkflowFactSnapshot,
    run: DiscoveryRunFact,
) -> tuple[SpecDraftFact | None, bool]:
    rows = tuple(
        item
        for item in snapshot.spec_drafts
        if item.discovery_run_id == run.discovery_run_id
    )
    ids = {item.spec_draft_id for item in rows}
    parents = {item.supersedes_id for item in rows if item.supersedes_id is not None}
    leaves = tuple(item for item in rows if item.spec_draft_id not in parents)
    conflict = (
        not parents <= ids
        or (bool(rows) and len(leaves) != 1)
        or any(
            item.kind != "amendment"
            or item.base_spec_version_id != run.base_spec_version_id
            or item.base_spec_hash != run.base_spec_hash
            for item in rows
        )
    )
    return (leaves[0] if len(leaves) == 1 else None), conflict


def _execution_completion_blocker(
    snapshot: WorkflowFactSnapshot,
) -> tuple[str, str] | None:
    if any(item.status == "active" for item in snapshot.sprints):
        return "ACTIVE_SPRINT_EXISTS", "Complete the active Sprint first."
    if any(
        item.sprint_candidate and item.status not in {"Done", "Accepted", "Cancelled"}
        for item in snapshot.stories
    ):
        return (
            "SPRINT_CANDIDATES_EXIST",
            "Current accepted Sprint candidates must be exhausted first.",
        )
    completed = tuple(item for item in snapshot.sprints if item.status == "completed")
    triaged_ids = {item.sprint_id for item in snapshot.post_sprint_triage}
    if not completed or any(item.sprint_id not in triaged_ids for item in completed):
        return (
            "POST_SPRINT_TRIAGE_REQUIRED",
            "Every completed Sprint requires durable post-Sprint triage.",
        )
    return None


def scope_execution_is_complete(snapshot: WorkflowFactSnapshot) -> bool:
    """Return whether current Sprint work is exhausted and durably triaged."""
    return _execution_completion_blocker(snapshot) is None


def _start_blocker(snapshot: WorkflowFactSnapshot) -> tuple[str, str] | None:
    authority, authority_conflict = accepted_current_authority(snapshot)
    if authority_conflict:
        return "WORKFLOW_FACT_CONFLICT", "Current authority facts conflict."
    if authority is None:
        return (
            "ACCEPTED_AUTHORITY_REQUIRED",
            "Scope extension requires accepted current authority.",
        )
    return _execution_completion_blocker(snapshot)


def _available_start(snapshot: WorkflowFactSnapshot) -> tuple[RuleEvaluation, ...]:
    authority, conflict = accepted_current_authority(snapshot)
    approved = tuple(
        item for item in snapshot.spec_versions if item.status == "approved"
    )
    if conflict or authority is None or len(approved) != 1:
        return _invalid()
    spec = approved[0]
    return _evaluation(
        RuleCategory.AVAILABLE,
        "SCOPE_EXTENSION_AVAILABLE",
        references=(
            _reference("spec_version", spec.spec_version_id, spec.spec_hash),
            _reference(
                "authority",
                authority.authority_id,
                authority.authority_fingerprint,
            ),
        ),
    )


def _start_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return _evaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED")
    state = _run_state(snapshot)
    if state.conflict:
        return _invalid()
    if state.run is not None:
        return _evaluation(RuleCategory.SATISFIED, "SCOPE_EXTENSION_ACTIVE")
    blocker = _start_blocker(snapshot)
    if blocker is not None:
        if blocker[0] == "WORKFLOW_FACT_CONFLICT":
            return _invalid()
        return _blocked(*blocker)
    return _available_start(snapshot)


def _challenge_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _run_state(snapshot)
    if state.conflict:
        return _invalid()
    if state.run is None:
        return _evaluation(RuleCategory.SATISFIED, "NO_ACTIVE_SCOPE_EXTENSION")
    instance = _instance(state.run)
    rows = tuple(
        item
        for item in snapshot.challenge_artifacts
        if item.discovery_run_id == state.run.discovery_run_id
    )
    if len(rows) > 1:
        return _invalid(instance)
    if rows:
        return _evaluation(
            RuleCategory.SATISFIED,
            "EXTENSION_CHALLENGE_RECORDED",
            instance_key=instance,
        )
    return _evaluation(
        RuleCategory.AVAILABLE,
        "EXTENSION_CHALLENGE_REQUIRED",
        instance_key=instance,
    )


def _prd_after_challenge(
    snapshot: WorkflowFactSnapshot,
    run: DiscoveryRunFact,
) -> tuple[RuleEvaluation, ...]:
    instance = _instance(run)
    active, conflict = _active_prd(snapshot, run)
    if conflict:
        return _invalid(instance)
    if active is None:
        return _evaluation(
            RuleCategory.AVAILABLE,
            "EXTENSION_PRD_REQUIRED",
            instance_key=instance,
        )
    review, conflict = _review_for(
        snapshot,
        "prd",
        active.prd_version_id,
        active.content_fingerprint,
    )
    if conflict:
        return _invalid(instance)
    if review is None or review.decision == "accepted":
        return _evaluation(
            RuleCategory.SATISFIED,
            "EXTENSION_PRD_RECORDED",
            instance_key=instance,
        )
    return _recovery_evaluation(
        "EXTENSION_PRD_REVISION_REQUIRED",
        instance,
        (_reference("prd", active.prd_version_id, active.content_fingerprint),),
    )


def _prd_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _run_state(snapshot)
    if state.conflict:
        return _invalid()
    if state.run is None:
        return _evaluation(RuleCategory.SATISFIED, "NO_ACTIVE_SCOPE_EXTENSION")
    instance = _instance(state.run)
    challenges = tuple(
        item
        for item in snapshot.challenge_artifacts
        if item.discovery_run_id == state.run.discovery_run_id
    )
    if len(challenges) != 1:
        return _blocked(
            "EXTENSION_CHALLENGE_REQUIRED",
            "Record the extension challenge first.",
            instance_key=instance,
        )
    return _prd_after_challenge(snapshot, state.run)


def _prd_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _run_state(snapshot)
    if state.conflict or state.run is None:
        return (
            _invalid()
            if state.conflict
            else _evaluation(RuleCategory.SATISFIED, "NO_ACTIVE_SCOPE_EXTENSION")
        )
    active, conflict = _active_prd(snapshot, state.run)
    if conflict:
        return _invalid(_instance(state.run))
    if active is None:
        return _evaluation(RuleCategory.SATISFIED, "EXTENSION_PRD_NOT_READY")
    instance = f"prd:{active.prd_version_id}"
    review, conflict = _review_for(
        snapshot,
        "prd",
        active.prd_version_id,
        active.content_fingerprint,
    )
    if conflict:
        return _invalid(instance)
    if review is not None:
        return _evaluation(
            RuleCategory.SATISFIED,
            "EXTENSION_PRD_REVIEWED",
            instance_key=instance,
        )
    return _evaluation(
        RuleCategory.WAITING,
        "EXTENSION_PRD_REVIEW_REQUIRED",
        instance_key=instance,
        references=(
            _reference("prd", active.prd_version_id, active.content_fingerprint),
        ),
    )


def _accepted_prd(
    snapshot: WorkflowFactSnapshot,
    run: DiscoveryRunFact,
) -> tuple[PrdVersionFact | None, bool]:
    active, conflict = _active_prd(snapshot, run)
    if conflict or active is None:
        return None, conflict
    review, review_conflict = _review_for(
        snapshot,
        "prd",
        active.prd_version_id,
        active.content_fingerprint,
    )
    return (
        active if review is not None and review.decision == "accepted" else None,
        review_conflict,
    )


def _spec_after_prd(
    snapshot: WorkflowFactSnapshot,
    run: DiscoveryRunFact,
    prd: PrdVersionFact,
) -> tuple[RuleEvaluation, ...]:
    instance = _instance(run)
    active, conflict = _active_spec(snapshot, run)
    if conflict:
        return _invalid(instance)
    if active is None:
        return _evaluation(
            RuleCategory.AVAILABLE,
            "AMENDMENT_SPEC_REQUIRED",
            instance_key=instance,
            references=(
                _reference("prd", prd.prd_version_id, prd.content_fingerprint),
            ),
        )
    review, conflict = _review_for(
        snapshot,
        "spec_draft",
        active.spec_draft_id,
        active.content_fingerprint,
    )
    if conflict:
        return _invalid(instance)
    if review is None or review.decision == "accepted":
        return _evaluation(
            RuleCategory.SATISFIED,
            "AMENDMENT_SPEC_RECORDED",
            instance_key=instance,
        )
    return _recovery_evaluation(
        "AMENDMENT_SPEC_REVISION_REQUIRED",
        instance,
        (
            _reference(
                "spec_draft",
                active.spec_draft_id,
                active.content_fingerprint,
            ),
        ),
    )


def _spec_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _run_state(snapshot)
    if state.conflict:
        return _invalid()
    if state.run is None:
        return _evaluation(RuleCategory.SATISFIED, "NO_ACTIVE_SCOPE_EXTENSION")
    instance = _instance(state.run)
    prd, conflict = _accepted_prd(snapshot, state.run)
    if conflict:
        return _invalid(instance)
    if prd is None:
        return _blocked(
            "ACCEPTED_EXTENSION_PRD_REQUIRED",
            "Accept the extension PRD first.",
            instance_key=instance,
        )
    return _spec_after_prd(snapshot, state.run, prd)


def _spec_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _run_state(snapshot)
    if state.conflict or state.run is None:
        return (
            _invalid()
            if state.conflict
            else _evaluation(RuleCategory.SATISFIED, "NO_ACTIVE_SCOPE_EXTENSION")
        )
    active, conflict = _active_spec(snapshot, state.run)
    if conflict:
        return _invalid(_instance(state.run))
    if active is None:
        return _evaluation(RuleCategory.SATISFIED, "AMENDMENT_SPEC_NOT_READY")
    instance = f"spec:{active.spec_draft_id}"
    review, conflict = _review_for(
        snapshot,
        "spec_draft",
        active.spec_draft_id,
        active.content_fingerprint,
    )
    if conflict:
        return _invalid(instance)
    if review is not None:
        return _evaluation(
            RuleCategory.SATISFIED,
            "AMENDMENT_SPEC_REVIEWED",
            instance_key=instance,
        )
    return _evaluation(
        RuleCategory.WAITING,
        "AMENDMENT_SPEC_REVIEW_REQUIRED",
        instance_key=instance,
        references=(
            _reference(
                "spec_draft",
                active.spec_draft_id,
                active.content_fingerprint,
            ),
        ),
    )


def _registration_for_run(
    snapshot: WorkflowFactSnapshot,
    run_id: int,
) -> tuple[ScopeExtensionRegistrationFact | None, bool]:
    rows = tuple(
        item
        for item in snapshot.extension_registrations
        if item.discovery_run_id == run_id
    )
    return (rows[0] if len(rows) == 1 else None), len(rows) > 1


def _registration_after_draft(
    snapshot: WorkflowFactSnapshot,
    run: DiscoveryRunFact,
) -> tuple[RuleEvaluation, ...]:
    instance = _instance(run)
    active, conflict = _active_spec(snapshot, run)
    if conflict:
        return _invalid(instance)
    if active is None:
        return _blocked(
            "ACCEPTED_AMENDMENT_REQUIRED",
            "Accept an amendment draft first.",
            instance_key=instance,
        )
    review, conflict = _review_for(
        snapshot,
        "spec_draft",
        active.spec_draft_id,
        active.content_fingerprint,
    )
    if conflict:
        return _invalid(instance)
    if review is None or review.decision != "accepted":
        return _blocked(
            "ACCEPTED_AMENDMENT_REQUIRED",
            "Accept an amendment draft first.",
            instance_key=instance,
        )
    return _evaluation(
        RuleCategory.AVAILABLE,
        "SCOPE_EXTENSION_REGISTRATION_REQUIRED",
        instance_key=instance,
        references=(
            _reference(
                "spec_draft",
                active.spec_draft_id,
                active.content_fingerprint,
            ),
        ),
    )


def _registration_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _run_state(snapshot)
    if state.conflict:
        return _invalid()
    if state.run is None:
        return _evaluation(RuleCategory.SATISFIED, "NO_ACTIVE_SCOPE_EXTENSION")
    instance = _instance(state.run)
    registration, conflict = _registration_for_run(snapshot, state.run.discovery_run_id)
    if conflict:
        return _invalid(instance)
    if registration is not None:
        return _evaluation(
            RuleCategory.SATISFIED,
            "SCOPE_EXTENSION_REGISTERED",
            instance_key=instance,
        )
    return _registration_after_draft(snapshot, state.run)


def _replacement_authority(
    snapshot: WorkflowFactSnapshot,
    run: DiscoveryRunFact,
) -> tuple[AuthorityFact | None, bool]:
    registration, conflict = _registration_for_run(snapshot, run.discovery_run_id)
    if conflict or registration is None:
        return None, conflict
    authority, authority_conflict = accepted_current_authority(snapshot)
    if authority_conflict:
        return None, True
    if authority is None or authority.spec_version_id != registration.spec_version_id:
        return None, False
    return authority, False


def _authority_after_registration(
    snapshot: WorkflowFactSnapshot,
    run: DiscoveryRunFact,
    registration: ScopeExtensionRegistrationFact,
) -> tuple[RuleEvaluation, ...]:
    instance = _instance(run)
    authority, conflict = _replacement_authority(snapshot, run)
    if conflict:
        return _invalid(instance)
    if authority is None:
        return _evaluation(
            RuleCategory.WAITING,
            "REPLACEMENT_AUTHORITY_REQUIRED",
            instance_key=instance,
            references=(
                _reference(
                    "spec_version",
                    registration.spec_version_id,
                    registration.spec_hash,
                ),
            ),
        )
    return _evaluation(
        RuleCategory.SATISFIED,
        "REPLACEMENT_AUTHORITY_ACCEPTED",
        instance_key=instance,
    )


def _authority_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _run_state(snapshot)
    if state.conflict:
        return _invalid()
    if state.run is None:
        return _evaluation(RuleCategory.SATISFIED, "NO_ACTIVE_SCOPE_EXTENSION")
    instance = _instance(state.run)
    registration, conflict = _registration_for_run(snapshot, state.run.discovery_run_id)
    if conflict:
        return _invalid(instance)
    if registration is None:
        return _evaluation(
            RuleCategory.SATISFIED,
            "EXTENSION_AUTHORITY_NOT_READY",
            instance_key=instance,
        )
    return _authority_after_registration(snapshot, state.run, registration)


def _current_phase_artifact(
    snapshot: WorkflowFactSnapshot,
    artifact_type: str,
) -> tuple[PhaseArtifactFact | None, bool]:
    rows = tuple(
        item for item in snapshot.phase_artifacts if item.artifact_type == artifact_type
    )
    if not rows:
        return None, False
    if any(
        isinstance(item.artifact_id, bool) or not isinstance(item.artifact_id, int)
        for item in rows
    ):
        return None, True
    by_id = {int(item.artifact_id): item for item in rows}
    parents = {
        item.supersedes_artifact_id
        for item in rows
        if item.supersedes_artifact_id is not None
    }
    current = tuple(
        item
        for item in rows
        if int(item.artifact_id) not in parents and item.status != "superseded"
    )
    conflict = (
        len(by_id) != len(rows)
        or not parents <= set(by_id)
        or any(
            parent is not None and parent >= int(item.artifact_id)
            for item in rows
            if (parent := item.supersedes_artifact_id) is not None
        )
        or len(current) != 1
    )
    return (current[0] if len(current) == 1 else None), conflict


def _current_planning_artifacts(
    snapshot: WorkflowFactSnapshot,
    artifact_type: str,
) -> tuple[tuple[PlanningArtifactFact, ...], bool]:
    rows = tuple(
        item
        for item in snapshot.planning_artifacts
        if item.artifact_type == artifact_type
    )
    if not rows:
        return (), False
    groups: dict[str, list[PlanningArtifactFact]] = {}
    for item in rows:
        if artifact_type == "story":
            if item.requirement_id is None:
                return (), True
            group_key = item.requirement_id
        else:
            group_key = artifact_type
        groups.setdefault(group_key, []).append(item)
    current: list[PlanningArtifactFact] = []
    for group_rows in groups.values():
        by_id = {item.artifact_id: item for item in group_rows}
        parents = {
            item.supersedes_artifact_id
            for item in group_rows
            if item.supersedes_artifact_id is not None
        }
        leaves = tuple(
            item
            for item in group_rows
            if item.artifact_id not in parents and item.status != "superseded"
        )
        if (
            len(by_id) != len(group_rows)
            or not parents <= set(by_id)
            or any(
                parent is not None and parent >= item.artifact_id
                for item in group_rows
                if (parent := item.supersedes_artifact_id) is not None
            )
            or len(leaves) != 1
        ):
            return (), True
        current.append(leaves[0])
    return (
        tuple(
            sorted(
                current,
                key=lambda item: (item.requirement_id or "", item.artifact_id),
            )
        ),
        False,
    )


def _downstream_state_fingerprint(snapshot: WorkflowFactSnapshot) -> str:
    review_types = {"vision", "backlog", "roadmap", "story", "sprint"}
    return canonical_hash(
        {
            "phase_artifacts": [
                item.model_dump(mode="json")
                for item in sorted(
                    snapshot.phase_artifacts,
                    key=lambda item: (item.artifact_type, str(item.artifact_id)),
                )
            ],
            "backlog_reconciliations": [
                item.model_dump(mode="json")
                for item in sorted(
                    snapshot.backlog_reconciliations,
                    key=lambda item: item.reconciliation_id,
                )
            ],
            "backlog_requirements": [
                item.model_dump(mode="json")
                for item in sorted(
                    snapshot.backlog_requirements,
                    key=lambda item: (item.backlog_artifact_id, item.requirement_id),
                )
            ],
            "planning_artifacts": [
                item.model_dump(mode="json")
                for item in sorted(
                    snapshot.planning_artifacts,
                    key=lambda item: (
                        item.artifact_type,
                        item.requirement_id or "",
                        item.artifact_id,
                    ),
                )
            ],
            "review_decisions": [
                item.model_dump(mode="json")
                for item in sorted(
                    (
                        item
                        for item in snapshot.review_decisions
                        if item.artifact_type in review_types
                    ),
                    key=lambda item: (
                        item.artifact_type,
                        item.artifact_id,
                        item.decision_id,
                    ),
                )
            ],
            "stories": [
                item.model_dump(mode="json")
                for item in sorted(snapshot.stories, key=lambda item: item.story_id)
            ],
            "story_dependencies": [
                item.model_dump(mode="json")
                for item in sorted(
                    snapshot.story_dependencies,
                    key=lambda item: (
                        item.dependent_story_id,
                        item.prerequisite_story_id,
                        item.dependency_id,
                    ),
                )
            ],
            "story_dependency_reviews": [
                item.model_dump(mode="json")
                for item in sorted(
                    snapshot.story_dependency_reviews,
                    key=lambda item: item.review_id,
                )
            ],
            "sprints": [
                item.model_dump(mode="json")
                for item in sorted(snapshot.sprints, key=lambda item: item.sprint_id)
            ],
            "sprint_starts": [
                item.model_dump(mode="json")
                for item in sorted(
                    snapshot.sprint_starts,
                    key=lambda item: item.start_id,
                )
            ],
            "tasks": [
                item.model_dump(mode="json")
                for item in sorted(
                    snapshot.tasks,
                    key=lambda item: (item.sprint_id, item.story_id, item.task_id),
                )
            ],
        }
    )


def _sorted_references(
    references: list[FactReference],
) -> tuple[FactReference, ...]:
    return tuple(
        sorted(
            references,
            key=lambda item: (item.fact_type, int(item.fact_id)),
        )
    )


def _phase_evidence(snapshot: WorkflowFactSnapshot) -> _DownstreamEvidence:
    references: list[FactReference] = []
    for artifact_type in ("vision", "backlog"):
        artifact, conflict = _current_phase_artifact(snapshot, artifact_type)
        if conflict:
            return _DownstreamEvidence(conflict=True)
        if artifact is None:
            return _DownstreamEvidence(
                blocker=(
                    "DOWNSTREAM_ARTIFACT_MISSING",
                    (
                        f"Current {artifact_type} facts are required before "
                        "reconciliation."
                    ),
                )
            )
        if artifact.status != "accepted":
            return _DownstreamEvidence(
                blocker=(
                    "DOWNSTREAM_REVIEW_UNRESOLVED",
                    f"Current {artifact_type} review must be accepted first.",
                )
            )
        references.append(
            _reference(
                artifact_type,
                int(artifact.artifact_id),
                artifact.artifact_fingerprint,
            )
        )
    return _DownstreamEvidence(references=tuple(references))


def _planning_evidence(snapshot: WorkflowFactSnapshot) -> _DownstreamEvidence:
    references: list[FactReference] = []
    for artifact_type in ("roadmap", "story", "sprint_plan"):
        artifacts, conflict = _current_planning_artifacts(snapshot, artifact_type)
        if conflict:
            return _DownstreamEvidence(conflict=True)
        if not artifacts:
            return _DownstreamEvidence(
                blocker=(
                    "DOWNSTREAM_ARTIFACT_MISSING",
                    (
                        f"Current {artifact_type} facts are required before "
                        "reconciliation."
                    ),
                )
            )
        if any(item.status != "accepted" for item in artifacts):
            return _DownstreamEvidence(
                blocker=(
                    "DOWNSTREAM_REVIEW_UNRESOLVED",
                    f"Current {artifact_type} review must be accepted first.",
                )
            )
        references.extend(
            _reference(
                artifact_type,
                item.artifact_id,
                item.artifact_fingerprint,
            )
            for item in artifacts
        )
    return _DownstreamEvidence(references=tuple(references))


def _candidate_stories(snapshot: WorkflowFactSnapshot) -> tuple[StoryFact, ...]:
    return tuple(
        sorted(
            (item for item in snapshot.stories if item.sprint_candidate),
            key=lambda item: item.story_id,
        )
    )


def _dependency_candidate_problem(
    snapshot: WorkflowFactSnapshot,
    candidates: tuple[StoryFact, ...],
) -> _DownstreamEvidence | None:
    if any(not item.content_accepted for item in candidates):
        return _DownstreamEvidence(
            blocker=(
                "STORY_CONTENT_NOT_ACCEPTED",
                "Current candidate Story content must be accepted first.",
            )
        )
    candidate_ids = {item.story_id for item in candidates}
    if any(
        item.status == "proposed"
        and (
            item.dependent_story_id in candidate_ids
            or item.prerequisite_story_id in candidate_ids
        )
        for item in snapshot.story_dependencies
    ):
        return _DownstreamEvidence(
            blocker=(
                "STORY_DEPENDENCIES_UNREVIEWED",
                "Current candidate Story dependencies require review.",
            )
        )
    return None


def _reviewed_dependency_evidence(
    snapshot: WorkflowFactSnapshot,
    candidates: tuple[StoryFact, ...],
) -> _DownstreamEvidence:
    if not candidates:
        return _DownstreamEvidence()
    source = story_dependency_source_fingerprint(candidates)
    selected_story_ids = tuple(item.story_id for item in candidates)
    reviews = tuple(
        item
        for item in snapshot.story_dependency_reviews
        if item.source_fingerprint == source
        and item.selected_story_ids == selected_story_ids
    )
    if len(reviews) > 1:
        return _DownstreamEvidence(conflict=True)
    if not reviews:
        return _DownstreamEvidence(
            blocker=(
                "STORY_DEPENDENCIES_UNREVIEWED",
                "Current candidate Story dependencies require review.",
            )
        )
    try:
        current_edges = active_dependency_review_edges(snapshot.story_dependencies)
    except ValueError:
        return _DownstreamEvidence(conflict=True)
    review = reviews[0]
    if (
        review.reviewed_edges != current_edges
        or review.dependency_fingerprint != dependency_review_fingerprint(current_edges)
    ):
        return _DownstreamEvidence(
            blocker=(
                "STORY_DEPENDENCY_REVIEW_STALE",
                "Current dependency review no longer matches Story facts.",
            )
        )
    return _DownstreamEvidence(
        references=(
            _reference(
                "story_dependency_review",
                review.review_id,
                canonical_hash(review.model_dump(mode="json")),
            ),
        )
    )


def _dependency_evidence(snapshot: WorkflowFactSnapshot) -> _DownstreamEvidence:
    candidates = _candidate_stories(snapshot)
    problem = _dependency_candidate_problem(snapshot, candidates)
    if problem is not None:
        return problem
    return _reviewed_dependency_evidence(snapshot, candidates)


def _readiness_evidence(snapshot: WorkflowFactSnapshot) -> _DownstreamEvidence:
    candidates = _candidate_stories(snapshot)
    if any(item.story_points is None or item.rank is None for item in candidates):
        return _DownstreamEvidence(
            blocker=(
                "STORY_READINESS_INCOMPLETE",
                "Current candidate Stories require readiness repair.",
            )
        )
    return _DownstreamEvidence(
        references=(
            _reference(
                "story_readiness",
                snapshot.project.project_id,
                readiness_fingerprint(snapshot.stories),
            ),
        )
    )


def _execution_evidence(snapshot: WorkflowFactSnapshot) -> _DownstreamEvidence:
    completion_blocker = _execution_completion_blocker(snapshot)
    if completion_blocker is not None:
        return _DownstreamEvidence(blocker=completion_blocker)
    plans, conflict = _current_planning_artifacts(snapshot, "sprint_plan")
    if conflict or len(plans) != 1:
        return _DownstreamEvidence(conflict=True)
    current_plan = plans[0]
    starts = tuple(
        item
        for item in snapshot.sprint_starts
        if item.sprint_plan_artifact_id == current_plan.artifact_id
        and item.plan_fingerprint == current_plan.artifact_fingerprint
    )
    if len(starts) > 1:
        return _DownstreamEvidence(conflict=True)
    if not starts:
        return _DownstreamEvidence(
            blocker=(
                "SPRINT_START_REQUIRED",
                "The accepted current Sprint plan has not been started.",
            )
        )
    return _DownstreamEvidence(
        references=(
            _reference(
                "sprint_start",
                starts[0].start_id,
                canonical_hash(starts[0].model_dump(mode="json")),
            ),
        )
    )


def _downstream_evidence(snapshot: WorkflowFactSnapshot) -> _DownstreamEvidence:
    references: list[FactReference] = []
    for evidence in (
        _phase_evidence(snapshot),
        _planning_evidence(snapshot),
        _dependency_evidence(snapshot),
        _readiness_evidence(snapshot),
        _execution_evidence(snapshot),
    ):
        if evidence.conflict or evidence.blocker is not None:
            return evidence
        references.extend(evidence.references)
    references.append(
        _reference(
            "scope_downstream_state",
            snapshot.project.project_id,
            _downstream_state_fingerprint(snapshot),
        )
    )
    return _DownstreamEvidence(references=_sorted_references(references))


def _persisted_reconciliation_references(
    snapshot: WorkflowFactSnapshot,
) -> tuple[FactReference, ...] | None:
    authority, conflict = accepted_current_authority(snapshot)
    if conflict or authority is None:
        return None
    matching = tuple(
        item
        for item in snapshot.scope_extension_reconciliations
        if item.replacement_authority_id == authority.authority_id
        and item.replacement_authority_fingerprint == authority.authority_fingerprint
    )
    return matching[0].artifact_references if len(matching) == 1 else None


def _current_reconciliation_references(
    snapshot: WorkflowFactSnapshot,
) -> tuple[FactReference, ...] | None:
    persisted = _persisted_reconciliation_references(snapshot)
    evidence = _downstream_evidence(snapshot)
    if (
        persisted is None
        or evidence.conflict
        or evidence.blocker is not None
        or persisted != evidence.references
    ):
        return None
    return persisted


def scope_reconciliation_is_current(snapshot: WorkflowFactSnapshot) -> bool:
    """Return whether reconciliation covers exact current downstream facts."""
    return _current_reconciliation_references(snapshot) is not None


_NODE_RECONCILIATION_FACT_TYPE: dict[str, str] = {
    "vision.generate": "vision",
    "vision.review": "vision",
    "backlog.generate": "backlog",
    "backlog.review": "backlog",
    "backlog.reconcile": "backlog",
    "planning.roadmap.generate": "roadmap",
    "planning.roadmap.review": "roadmap",
    "planning.story.generate": "story",
    "planning.story.review": "story",
    "planning.story_dependencies": "story_dependency_review",
    "planning.story_readiness": "story_readiness",
    "planning.sprint.plan": "sprint_plan",
    "planning.sprint.review": "sprint_plan",
    "planning.sprint.start": "sprint_start",
}


def _current_references_for_type(
    snapshot: WorkflowFactSnapshot,
    fact_type: str,
) -> tuple[FactReference, ...] | None:
    if fact_type in {"vision", "backlog"}:
        artifact, conflict = _current_phase_artifact(snapshot, fact_type)
        if conflict or artifact is None or artifact.status != "accepted":
            return None
        return (
            _reference(
                fact_type,
                int(artifact.artifact_id),
                artifact.artifact_fingerprint,
            ),
        )
    if fact_type in {"roadmap", "story", "sprint_plan"}:
        artifacts, conflict = _current_planning_artifacts(snapshot, fact_type)
        if (
            conflict
            or not artifacts
            or any(item.status != "accepted" for item in artifacts)
        ):
            return None
        return tuple(
            _reference(fact_type, item.artifact_id, item.artifact_fingerprint)
            for item in artifacts
        )
    evidence = {
        "story_dependency_review": _dependency_evidence,
        "story_readiness": _readiness_evidence,
        "sprint_start": _execution_evidence,
    }[fact_type](snapshot)
    if evidence.conflict or evidence.blocker is not None:
        return None
    return tuple(item for item in evidence.references if item.fact_type == fact_type)


def scope_reconciliation_retires_node(
    snapshot: WorkflowFactSnapshot,
    node_id: str,
) -> bool:
    """Retire only lifecycle nodes backed by exact persisted references."""
    fact_type = _NODE_RECONCILIATION_FACT_TYPE.get(node_id)
    persisted = _persisted_reconciliation_references(snapshot)
    if fact_type is None or persisted is None:
        return False
    current = _current_references_for_type(snapshot, fact_type)
    expected = tuple(item for item in persisted if item.fact_type == fact_type)
    return current is not None and current == expected


def scope_reconciled_snapshot(
    snapshot: WorkflowFactSnapshot,
) -> WorkflowFactSnapshot:
    """Project only exact reconciled artifacts onto current authority lineage."""
    authority, conflict = accepted_current_authority(snapshot)
    references = _persisted_reconciliation_references(snapshot)
    if conflict or authority is None or references is None:
        return snapshot
    reference_keys = {
        (item.fact_type, item.fact_id, item.fingerprint) for item in references
    }
    phase_artifacts = tuple(
        item.model_copy(
            update={
                "authority_id": authority.authority_id,
                "authority_fingerprint": authority.authority_fingerprint,
            }
        )
        if (
            item.artifact_type,
            str(item.artifact_id),
            item.artifact_fingerprint,
        )
        in reference_keys
        else item
        for item in snapshot.phase_artifacts
    )
    planning_artifacts = tuple(
        item.model_copy(
            update={
                "authority_id": authority.authority_id,
                "authority_fingerprint": authority.authority_fingerprint,
            }
        )
        if (
            item.artifact_type,
            str(item.artifact_id),
            item.artifact_fingerprint,
        )
        in reference_keys
        else item
        for item in snapshot.planning_artifacts
    )
    stories = tuple(
        item.model_copy(
            update={
                "authority_id": authority.authority_id,
                "authority_fingerprint": authority.authority_fingerprint,
            }
        )
        if item.story_artifact_id is not None
        and item.content_fingerprint is not None
        and (
            "story",
            str(item.story_artifact_id),
            item.content_fingerprint,
        )
        in reference_keys
        else item
        for item in snapshot.stories
    )
    return snapshot.model_copy(
        update={
            "phase_artifacts": phase_artifacts,
            "planning_artifacts": planning_artifacts,
            "stories": stories,
        }
    )


def _available_reconciliation(
    snapshot: WorkflowFactSnapshot,
    instance: str,
    authority: AuthorityFact,
) -> tuple[RuleEvaluation, ...]:
    evidence = _downstream_evidence(snapshot)
    if evidence.conflict:
        return _invalid(instance)
    if evidence.blocker is not None:
        return _blocked(*evidence.blocker, instance_key=instance)
    return _evaluation(
        RuleCategory.AVAILABLE,
        "SCOPE_EXTENSION_RECONCILIATION_REQUIRED",
        instance_key=instance,
        references=(
            _reference(
                "authority",
                authority.authority_id,
                authority.authority_fingerprint,
            ),
            *evidence.references,
        ),
    )


def _reconciliation_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _run_state(snapshot)
    if state.conflict:
        return _invalid()
    if state.run is None:
        return _evaluation(RuleCategory.SATISFIED, "NO_ACTIVE_SCOPE_EXTENSION")
    instance = _instance(state.run)
    rows = tuple(
        item
        for item in snapshot.scope_extension_reconciliations
        if item.discovery_run_id == state.run.discovery_run_id
    )
    if rows:
        return _invalid(instance)
    authority, conflict = _replacement_authority(snapshot, state.run)
    if conflict:
        return _invalid(instance)
    if authority is None:
        return _blocked(
            "REPLACEMENT_AUTHORITY_REQUIRED",
            "Accept replacement authority before reconciliation.",
            instance_key=instance,
        )
    return _available_reconciliation(snapshot, instance, authority)


def _abandon_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _run_state(snapshot)
    if state.conflict:
        return _invalid()
    if state.run is None:
        return _evaluation(RuleCategory.SATISFIED, "NO_ACTIVE_SCOPE_EXTENSION")
    instance = _instance(state.run)
    authority, conflict = _replacement_authority(snapshot, state.run)
    if conflict:
        return _invalid(instance)
    if authority is not None:
        return _evaluation(
            RuleCategory.SATISFIED,
            "REPLACEMENT_AUTHORITY_ACCEPTED",
            instance_key=instance,
        )
    return _evaluation(
        RuleCategory.AVAILABLE,
        "SCOPE_EXTENSION_ABANDONMENT_AVAILABLE",
        instance_key=instance,
    )


SCOPE_EXTENSION_NODES: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="scope_extension.start",
        child_graph_id="scope_extension",
        request_kind="start_scope_extension",
        recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        required_inputs=(
            InputField(name="base_spec_version_id", value_type="integer"),
            InputField(name="base_spec_hash", value_type="string"),
        ),
        evaluate_rule=_start_rule,
    ),
    NodeSpec(
        node_id="scope_extension.challenge",
        child_graph_id="scope_extension",
        request_kind="record_extension_challenge",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(InputField(name="canonical_content", value_type="object"),),
        evaluate_rule=_challenge_rule,
    ),
    NodeSpec(
        node_id="scope_extension.prd",
        child_graph_id="scope_extension",
        request_kind="record_extension_prd",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="challenge_artifact_id", value_type="integer"),
            InputField(name="canonical_content", value_type="object"),
        ),
        evaluate_rule=_prd_rule,
    ),
    NodeSpec(
        node_id="scope_extension.prd_review",
        child_graph_id="scope_extension",
        request_kind="decide_extension_prd",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="prd_version_id", value_type="integer"),
            InputField(name="artifact_fingerprint", value_type="string"),
            InputField(name="decision", value_type="string"),
            InputField(name="notes", value_type="string"),
        ),
        evaluate_rule=_prd_review_rule,
    ),
    NodeSpec(
        node_id="scope_extension.spec",
        child_graph_id="scope_extension",
        request_kind="record_amendment_spec_draft",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="prd_version_id", value_type="integer"),
            InputField(name="canonical_content", value_type="object"),
            InputField(name="base_spec_version_id", value_type="integer"),
            InputField(name="base_spec_hash", value_type="string"),
        ),
        evaluate_rule=_spec_rule,
    ),
    NodeSpec(
        node_id="scope_extension.spec_review",
        child_graph_id="scope_extension",
        request_kind="decide_amendment_spec_draft",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="spec_draft_id", value_type="integer"),
            InputField(name="artifact_fingerprint", value_type="string"),
            InputField(name="decision", value_type="string"),
            InputField(name="notes", value_type="string"),
        ),
        evaluate_rule=_spec_review_rule,
    ),
    NodeSpec(
        node_id="scope_extension.registration",
        child_graph_id="scope_extension",
        request_kind="register_scope_extension",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(InputField(name="spec_draft_id", value_type="integer"),),
        evaluate_rule=_registration_rule,
    ),
    NodeSpec(
        node_id="scope_extension.authority",
        child_graph_id="scope_extension",
        request_kind="compile_authority",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(),
        evaluate_rule=_authority_rule,
    ),
    NodeSpec(
        node_id="scope_extension.reconciliation",
        child_graph_id="scope_extension",
        request_kind="reconcile_scope_extension",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="replacement_authority_id", value_type="integer"),
            InputField(name="replacement_authority_fingerprint", value_type="string"),
            InputField(name="artifact_references", value_type="array"),
        ),
        evaluate_rule=_reconciliation_rule,
    ),
    NodeSpec(
        node_id="scope_extension.abandon",
        child_graph_id="scope_extension",
        request_kind="abandon_scope_extension",
        recommendation_kind=RecommendationKind.RECOVERY,
        required_inputs=(InputField(name="reason", value_type="string"),),
        evaluate_rule=_abandon_rule,
    ),
)


def scope_extension_graph() -> WorkflowGraph:
    """Return scope extension with the shared Task 9 authority graph."""
    return WorkflowGraph(
        graph_version=GRAPH_VERSION,
        root=ChildGraphSpec(
            child_graph_id="product_lifecycle",
            nodes=(),
            children=(
                ChildGraphSpec(child_graph_id="authority", nodes=AUTHORITY_NODES),
                ChildGraphSpec(
                    child_graph_id="scope_extension",
                    nodes=SCOPE_EXTENSION_NODES,
                ),
            ),
        ),
    )


__all__ = [
    "SCOPE_EXTENSION_NODES",
    "scope_execution_is_complete",
    "scope_extension_graph",
    "scope_reconciled_snapshot",
    "scope_reconciliation_is_current",
    "scope_reconciliation_retires_node",
]

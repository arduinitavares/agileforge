"""Issue 193 regression through persisted domain transitions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session, col, select

from models.core import Task, UserStory, UserStoryDependency
from models.enums import StoryStatus, TaskStatus
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from models.workflow import (
    BacklogArtifactDecision,
    DiscoveryRun,
    PostSprintTriage,
    RoadmapArtifactDecision,
    ScopeExtensionReconciliation,
    ScopeExtensionRegistration,
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    StoryArtifact,
    StoryArtifactDecision,
    VisionArtifact,
    VisionArtifactDecision,
)
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.agent_workbench.authority_review import (
    AuthorityReviewSnapshot,
    build_authority_review_snapshot_in_session,
)
from services.specs import compiler_service
from tests.workflow.execution_fixtures import _accept_and_start_sprint
from tests.workflow.test_authority_transitions import _success_artifact
from tests.workflow.test_planning_transitions import (
    _domain as _planning_domain,
)
from tests.workflow.test_planning_transitions import (
    _record_and_accept_roadmap,
    _record_and_accept_story,
    _record_sprint_plan_draft,
    _seed_accepted_backlog,
)
from tests.workflow.test_scope_extension_transitions import (
    EVALUATED_AT,
    _complete_current_scope,
    _current_spec,
    _decision,
    _domain,
    _guards,
    _seed_completed_project_history,
    accept_amendment_draft,
    register_amendment,
    seed_terminal_project,
    start_extension,
)
from tests.workflow.test_vision_backlog_transitions import _vision_content
from workflow.contracts import (
    FactReference,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.definitions.root import project_graph
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.requests import (
    CompileAuthority,
    DecideAuthority,
    DecideVision,
    ReconcileScopeExtension,
    RecordPostSprintTriage,
    ScopeExtensionArtifactReference,
    StartScopeExtension,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from workflow.domain import WorkflowDomain
    from workflow.requests import RegisterScopeExtension


@dataclass(frozen=True)
class _AcceptedReplacement:
    """Persisted extension state immediately before reconciliation."""

    domain: WorkflowDomain
    project_id: int
    run_id: int
    replacement: SpecRegistry
    review: AuthorityReviewSnapshot
    replacement_authority_id: int
    replacement_authority_fingerprint: str
    reconciliation: NodeDecision
    old_authority_id: int
    old_authority_fingerprint: str
    advertised_start: NodeDecision
    old_start_request: StartScopeExtension
    old_registration_request: RegisterScopeExtension


def _install_fake_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        compiler_service,
        "_invoke_compiler_for_version",
        lambda *_args, **_kwargs: compiler_service._CompilerInvocationResult(
            success=_success_artifact()
        ),
    )


def _seed_terminal_project_with_future_stories(
    engine: Engine,
) -> tuple[WorkflowDomain, int, tuple[int, int]]:
    """Complete one Sprint while retaining two isolated accepted future Stories."""
    requirements = (
        "Plan immutable work",
        "Review future dependency A",
        "Review future dependency B",
    )
    project_id = _seed_accepted_backlog(engine, requirements=requirements)
    planning_domain = _planning_domain(engine)
    _record_and_accept_roadmap(
        planning_domain,
        project_id,
        requirements=requirements,
    )
    _selected_artifact_id, selected_story_id = _record_and_accept_story(
        planning_domain,
        project_id,
        requirement=requirements[0],
    )
    future_story_a = _record_and_accept_story(
        planning_domain,
        project_id,
        requirement=requirements[1],
        idempotency_suffix="-future-1",
    )[1]
    future_story_b = _record_and_accept_story(
        planning_domain,
        project_id,
        requirement=requirements[2],
        idempotency_suffix="-future-2",
    )[1]
    future_story_ids = (
        future_story_a,
        future_story_b,
    )
    with Session(engine) as session:
        for story_id in future_story_ids:
            story = session.get(UserStory, story_id)
            assert story is not None
            story.status = StoryStatus.DONE
            session.add(story)
        session.commit()

    plan_binding = _record_sprint_plan_draft(
        engine,
        planning_domain,
        project_id,
        selected_story_id,
        team_name="Task 13 future-isolation team",
        idempotency_key="task-13-record-isolated-sprint-plan",
    )
    _accept_and_start_sprint(
        planning_domain,
        project_id=project_id,
        plan_binding=plan_binding,
        idempotency_suffix="-task-13-future-isolation",
    )
    _plan_id, sprint_id, _candidate_fingerprint, _plan = plan_binding
    with Session(engine) as session:
        task = session.exec(
            select(Task).where(col(Task.story_id) == selected_story_id)
        ).one()
        assert task.task_id is not None
        task.status = TaskStatus.IN_PROGRESS
        session.add(task)
        session.commit()
        task_id = task.task_id

    _seed_completed_project_history(engine, project_id)
    domain = _domain(engine)
    _complete_current_scope(
        domain,
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=selected_story_id,
        task_id=task_id,
    )
    return domain, project_id, future_story_ids


def _artifact_reference(reference: FactReference) -> ScopeExtensionArtifactReference:
    if reference.fact_type == "vision":
        artifact_type = "vision"
    elif reference.fact_type == "backlog":
        artifact_type = "backlog"
    elif reference.fact_type == "roadmap":
        artifact_type = "roadmap"
    elif reference.fact_type == "story":
        artifact_type = "story"
    else:
        message = f"Unsupported reconciliation fact type: {reference.fact_type}"
        raise ValueError(message)
    return ScopeExtensionArtifactReference(
        artifact_type=artifact_type,
        artifact_id=int(reference.fact_id),
        artifact_fingerprint=reference.fingerprint,
    )


def _accepted_replacement(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    *,
    provenance_path: Path | None,
    delete_provenance: bool = False,
    seeded_project: tuple[WorkflowDomain, int] | None = None,
) -> _AcceptedReplacement:
    """Persist an extension through accepted replacement authority."""
    domain, project_id = (
        seed_terminal_project(engine) if seeded_project is None else seeded_project
    )
    advertised_start = _decision(domain.position(project_id), "scope_extension.start")
    with Session(engine) as session:
        old_acceptance = session.exec(
            select(SpecAuthorityAcceptance).where(
                col(SpecAuthorityAcceptance.product_id) == project_id,
                col(SpecAuthorityAcceptance.status) == "accepted",
            )
        ).one()
        assert old_acceptance.pending_authority_id is not None
        assert old_acceptance.authority_fingerprint is not None
        old_authority_id = old_acceptance.pending_authority_id
        old_authority_fingerprint = old_acceptance.authority_fingerprint

    old_start_request, run_id = start_extension(domain, engine, project_id)
    draft_id, _content = accept_amendment_draft(
        domain,
        engine,
        project_id,
        run_id,
        provenance_path=provenance_path,
    )
    old_registration_request = register_amendment(
        domain,
        project_id,
        run_id,
        draft_id,
    )
    if delete_provenance and provenance_path is not None:
        provenance_path.unlink()

    _install_fake_compiler(monkeypatch)
    replacement = _current_spec(engine, project_id)
    assert replacement.spec_version_id is not None
    compiled = domain.transition(
        CompileAuthority(
            **_guards(
                domain,
                project_id,
                "authority.compile",
                f"spec:{replacement.spec_version_id}:{replacement.spec_hash}",
            ),
            idempotency_key="task-13-compile-replacement",
            spec_version_id=replacement.spec_version_id,
            expected_spec_hash=replacement.spec_hash,
        )
    )
    assert compiled.ok is True

    with Session(engine) as session:
        review = build_authority_review_snapshot_in_session(
            session,
            project_id=project_id,
        )
        assert isinstance(review, AuthorityReviewSnapshot)
        assert review.pending_authority_id is not None
        assert review.authority_fingerprint is not None
    accepted = domain.transition(
        DecideAuthority(
            **_guards(domain, project_id, "authority.review"),
            idempotency_key="task-13-accept-replacement-authority",
            pending_authority_id=review.pending_authority_id,
            authority_fingerprint=review.authority_fingerprint,
            review_fingerprint=review.review_fingerprint,
            decision="accepted",
            rationale="Replacement authority matches the accepted amendment.",
        )
    )
    assert accepted.ok is True
    reconciliation = _decision(
        domain.position(project_id),
        "scope_extension.reconciliation",
        f"run:{run_id}",
    )
    return _AcceptedReplacement(
        domain=domain,
        project_id=project_id,
        run_id=run_id,
        replacement=replacement,
        review=review,
        replacement_authority_id=review.pending_authority_id,
        replacement_authority_fingerprint=review.authority_fingerprint,
        reconciliation=reconciliation,
        old_authority_id=old_authority_id,
        old_authority_fingerprint=old_authority_fingerprint,
        advertised_start=advertised_start,
        old_start_request=old_start_request,
        old_registration_request=old_registration_request,
    )


def _reconcile_request(
    context: _AcceptedReplacement,
    *,
    authority_id: int,
    authority_fingerprint: str,
    idempotency_key: str,
) -> ReconcileScopeExtension:
    references = tuple(
        _artifact_reference(item)
        for item in context.reconciliation.fact_references
        if item.fact_type in {"vision", "backlog", "roadmap", "story"}
    )
    return ReconcileScopeExtension(
        **_guards(
            context.domain,
            context.project_id,
            "scope_extension.reconciliation",
            f"run:{context.run_id}",
        ),
        idempotency_key=idempotency_key,
        discovery_run_id=context.run_id,
        replacement_authority_id=authority_id,
        replacement_authority_fingerprint=authority_fingerprint,
        artifact_references=references,
    )


def _start_request_for_position(
    context: _AcceptedReplacement,
    position: WorkflowPosition,
    *,
    fallback: NodeDecision,
    idempotency_key: str,
) -> StartScopeExtension:
    """Build a direct start attempt even when the current node is suppressed."""
    current = next(
        (
            item
            for item in position.decisions
            if item.node_id == "scope_extension.start" and item.instance_key is None
        ),
        None,
    )
    assert context.replacement.spec_version_id is not None
    return StartScopeExtension(
        project_id=context.project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=(
            current.decision_fingerprint
            if current is not None
            else fallback.decision_fingerprint
        ),
        instance_key=None,
        idempotency_key=idempotency_key,
        actor="operator@example.com",
        correlation_id="task-13-second-review",
        base_spec_version_id=context.replacement.spec_version_id,
        base_spec_hash=context.replacement.spec_hash,
    )


def _discovery_run_count(engine: Engine, project_id: int) -> int:
    with Session(engine) as session:
        return len(
            session.exec(
                select(DiscoveryRun).where(col(DiscoveryRun.project_id) == project_id)
            ).all()
        )


def _assert_fresh_optional_start(
    context: _AcceptedReplacement,
    previous: NodeDecision,
) -> NodeDecision:
    position = context.domain.position(context.project_id)
    start = _decision(position, "scope_extension.start")
    assert position.terminal is True
    assert start.category is NodeCategory.AVAILABLE
    assert start.recommendation_kind is RecommendationKind.OPTIONAL_REENTRY
    assert start.decision_fingerprint != previous.decision_fingerprint
    return start


def _set_artifact_review_state(
    session: Session,
    *,
    project_id: int,
    artifact_type: str,
    decision: str,
) -> None:
    """Persist one pending or rejected downstream review state."""
    if artifact_type == "vision":
        row = session.exec(
            select(VisionArtifactDecision).where(
                col(VisionArtifactDecision.project_id) == project_id
            )
        ).one()
        if decision == "pending":
            session.delete(row)
        else:
            row.decision = "rejected"
            session.add(row)
        return
    if artifact_type == "backlog":
        row = session.exec(
            select(BacklogArtifactDecision).where(
                col(BacklogArtifactDecision.project_id) == project_id
            )
        ).one()
        if decision == "pending":
            session.delete(row)
        else:
            row.decision = "rejected"
            session.add(row)
        return
    if artifact_type == "roadmap":
        row = session.exec(
            select(RoadmapArtifactDecision).where(
                col(RoadmapArtifactDecision.project_id) == project_id
            )
        ).one()
        if decision == "pending":
            session.delete(row)
        else:
            row.decision = "rejected"
            session.add(row)
        return
    if artifact_type == "story":
        row = session.exec(
            select(StoryArtifactDecision).where(
                col(StoryArtifactDecision.project_id) == project_id
            )
        ).one()
        if decision == "pending":
            session.delete(row)
        else:
            row.decision = "rejected"
            session.add(row)
        return
    message = f"Unsupported review artifact: {artifact_type}"
    raise AssertionError(message)


def _append_sprint_plan_review_state(
    session: Session,
    *,
    project_id: int,
    decision: str,
) -> None:
    """Append a newer pending or rejected Sprint-plan review."""
    current = session.exec(
        select(SprintPlanArtifact).where(
            col(SprintPlanArtifact.project_id) == project_id
        )
    ).one()
    assert current.sprint_plan_artifact_id is not None
    payload = json.loads(current.canonical_task_plan_json)
    assert isinstance(payload, dict)
    payload["sprint_goal"] = "Review replacement-scope planning before closure."
    fingerprint = canonical_hash(payload)
    replacement = SprintPlanArtifact(
        project_id=project_id,
        sprint_id=current.sprint_id,
        version_number=current.version_number + 1,
        selected_story_ids_json=current.selected_story_ids_json,
        canonical_task_plan_json=canonical_json(payload),
        plan_fingerprint=fingerprint,
        candidate_set_fingerprint=current.candidate_set_fingerprint,
        supersedes_sprint_plan_artifact_id=current.sprint_plan_artifact_id,
        created_by="operator@example.com",
        created_at=EVALUATED_AT,
    )
    session.add(replacement)
    session.flush()
    assert replacement.sprint_plan_artifact_id is not None
    if decision == "rejected":
        session.add(
            SprintPlanArtifactDecision(
                project_id=project_id,
                sprint_plan_artifact_id=replacement.sprint_plan_artifact_id,
                plan_fingerprint=fingerprint,
                decision="rejected",
                rationale="Replacement-scope Sprint plan needs revision.",
                reviewer="operator@example.com",
                idempotency_key="task-13-rejected-new-sprint-plan",
                decided_at=EVALUATED_AT,
            )
        )


def _append_story_review_state(
    session: Session,
    *,
    project_id: int,
    story_id: int,
    decision: str,
) -> None:
    """Append pending or rejected Story review outside completed Sprint scope."""
    previous = next(
        item
        for item in session.exec(
            select(StoryArtifact).where(col(StoryArtifact.project_id) == project_id)
        ).all()
        if story_id in json.loads(item.story_ids_json)
    )
    assert previous.story_artifact_id is not None
    payload = json.loads(previous.canonical_content_json)
    assert isinstance(payload, dict)
    payload["remaining_scope"] = ["Review replacement-scope Story content."]
    fingerprint = canonical_hash(payload)
    replacement = StoryArtifact(
        project_id=project_id,
        requirement_id=previous.requirement_id,
        roadmap_artifact_id=previous.roadmap_artifact_id,
        roadmap_artifact_fingerprint=previous.roadmap_artifact_fingerprint,
        version_number=previous.version_number + 1,
        canonical_content_json=canonical_json(payload),
        content_fingerprint=fingerprint,
        story_ids_json=previous.story_ids_json,
        supersedes_story_artifact_id=previous.story_artifact_id,
        created_by="operator@example.com",
        created_at=EVALUATED_AT,
    )
    session.add(replacement)
    session.flush()
    assert replacement.story_artifact_id is not None
    if decision == "rejected":
        session.add(
            StoryArtifactDecision(
                project_id=project_id,
                story_artifact_id=replacement.story_artifact_id,
                artifact_fingerprint=fingerprint,
                decision="rejected",
                rationale="Replacement-scope Story content needs revision.",
                reviewer="operator@example.com",
                idempotency_key="task-13-rejected-future-story",
                decided_at=EVALUATED_AT,
            )
        )


def _append_vision_review_state(
    session: Session,
    *,
    context: _AcceptedReplacement,
    decision: str,
) -> int:
    """Append replacement-authority Vision work after reconciliation."""
    current = session.exec(
        select(VisionArtifact).where(
            col(VisionArtifact.project_id) == context.project_id
        )
    ).one()
    assert current.vision_artifact_id is not None
    payload = _vision_content("Review newly added replacement scope.")
    fingerprint = canonical_hash(payload)
    replacement = VisionArtifact(
        project_id=context.project_id,
        authority_id=context.replacement_authority_id,
        authority_fingerprint=context.replacement_authority_fingerprint,
        version_number=current.version_number + 1,
        canonical_content_json=canonical_json(payload),
        content_fingerprint=fingerprint,
        supersedes_vision_artifact_id=current.vision_artifact_id,
        created_by="operator@example.com",
        created_at=EVALUATED_AT,
    )
    session.add(replacement)
    session.flush()
    assert replacement.vision_artifact_id is not None
    if decision == "rejected":
        session.add(
            VisionArtifactDecision(
                project_id=context.project_id,
                vision_artifact_id=replacement.vision_artifact_id,
                artifact_fingerprint=fingerprint,
                decision="rejected",
                rationale="New replacement-scope Vision needs revision.",
                reviewer="operator@example.com",
                idempotency_key="task-13-rejected-new-vision",
                decided_at=EVALUATED_AT,
            )
        )
    return replacement.vision_artifact_id


def test_reconciliation_rejects_historical_authority_without_writes_or_drift(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Bind reconciliation to the exact replacement authority decision."""
    context = _accepted_replacement(
        engine,
        monkeypatch,
        provenance_path=tmp_path / "wrong-authority-source.json",
    )
    with Session(engine) as session:
        old_authority = session.get(
            CompiledSpecAuthority,
            context.old_authority_id,
        )
        assert old_authority is not None
        assert old_authority.spec_version_id != context.replacement.spec_version_id
    before = context.domain.position(context.project_id)
    request = _reconcile_request(
        context,
        authority_id=context.old_authority_id,
        authority_fingerprint=context.old_authority_fingerprint,
        idempotency_key="task-13-wrong-reconciliation-authority",
    )

    result = context.domain.transition(request)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    with Session(engine) as session:
        run = session.get(DiscoveryRun, context.run_id)
        assert run is not None
        assert run.closed_at is None
        assert (
            session.exec(
                select(ScopeExtensionReconciliation).where(
                    col(ScopeExtensionReconciliation.project_id) == context.project_id
                )
            ).all()
            == []
        )
    after = context.domain.position(context.project_id)
    restarted = _domain(engine).position(context.project_id)
    assert after.fact_fingerprint == before.fact_fingerprint
    assert tuple(item.decision_fingerprint for item in after.decisions) == tuple(
        item.decision_fingerprint for item in before.decisions
    )
    assert restarted == after


def test_persisted_reconciliation_rejects_cross_spec_authority_linkage(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fail loading when a closed run is rebound to another accepted spec."""
    context = _accepted_replacement(
        engine,
        monkeypatch,
        provenance_path=tmp_path / "tampered-linkage-source.json",
    )
    result = context.domain.transition(
        _reconcile_request(
            context,
            authority_id=context.replacement_authority_id,
            authority_fingerprint=context.replacement_authority_fingerprint,
            idempotency_key="task-13-valid-before-tamper",
        )
    )
    assert result.ok is True

    with Session(engine) as session:
        row = session.exec(
            select(ScopeExtensionReconciliation).where(
                col(ScopeExtensionReconciliation.project_id) == context.project_id
            )
        ).one()
        row.replacement_authority_id = context.old_authority_id
        row.replacement_authority_fingerprint = context.old_authority_fingerprint
        session.add(row)
        session.commit()

    with pytest.raises(
        WorkflowFactLoadError,
        match="Scope-extension reconciliation relationship is invalid",
    ):
        _domain(engine).position(context.project_id)


@pytest.mark.parametrize("provenance_mode", ["absent", "deleted"])
def test_extension_authority_review_uses_registered_content_without_source_file(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provenance_mode: str,
) -> None:
    """Complete authority acceptance from canonical SpecRegistry content."""
    provenance_path = (
        None
        if provenance_mode == "absent"
        else tmp_path / "deleted-after-registration.json"
    )

    context = _accepted_replacement(
        engine,
        monkeypatch,
        provenance_path=provenance_path,
        delete_provenance=provenance_path is not None,
    )

    assert context.review.source_content == context.replacement.content
    assert context.review.source_spec_hash == context.replacement.spec_hash
    assert context.review.disk_spec_hash == context.replacement.spec_hash
    if provenance_path is None:
        assert context.review.content_ref is None
    else:
        assert not provenance_path.exists()
    reconciled = context.domain.transition(
        _reconcile_request(
            context,
            authority_id=context.replacement_authority_id,
            authority_fingerprint=context.replacement_authority_fingerprint,
            idempotency_key=f"task-13-reconcile-{provenance_mode}",
        )
    )
    assert reconciled.ok is True
    assert context.domain.position(context.project_id).terminal is True


@pytest.mark.parametrize(
    ("artifact_type", "decision"),
    [
        ("vision", "pending"),
        ("vision", "rejected"),
        ("backlog", "pending"),
        ("backlog", "rejected"),
        ("roadmap", "pending"),
        ("roadmap", "rejected"),
        ("story", "pending"),
        ("story", "rejected"),
        ("sprint_plan", "pending"),
        ("sprint_plan", "rejected"),
    ],
)
def test_nonaccepted_downstream_review_blocks_scope_reconciliation(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact_type: str,
    decision: str,
) -> None:
    """Do not close over pending or rejected downstream review work."""
    seeded_project: tuple[WorkflowDomain, int] | None = None
    future_story_ids: tuple[int, int] | None = None
    if artifact_type == "story":
        domain, project_id, future_story_ids = (
            _seed_terminal_project_with_future_stories(engine)
        )
        seeded_project = (domain, project_id)
    context = _accepted_replacement(
        engine,
        monkeypatch,
        provenance_path=tmp_path / f"{artifact_type}-{decision}.json",
        seeded_project=seeded_project,
    )
    with Session(engine) as session:
        if artifact_type == "sprint_plan":
            _append_sprint_plan_review_state(
                session,
                project_id=context.project_id,
                decision=decision,
            )
        elif artifact_type == "story":
            assert future_story_ids is not None
            _append_story_review_state(
                session,
                project_id=context.project_id,
                story_id=future_story_ids[0],
                decision=decision,
            )
        else:
            _set_artifact_review_state(
                session,
                project_id=context.project_id,
                artifact_type=artifact_type,
                decision=decision,
            )
        session.commit()

    position = _domain(engine).position(context.project_id)
    reconciliation = _decision(
        position,
        "scope_extension.reconciliation",
        f"run:{context.run_id}",
    )
    assert reconciliation.category is NodeCategory.BLOCKED
    assert reconciliation.reason_code == "DOWNSTREAM_REVIEW_UNRESOLVED"
    assert position.terminal is False


@pytest.mark.parametrize("unresolved_kind", ["dependency", "readiness"])
def test_unresolved_dependency_or_readiness_blocks_scope_reconciliation(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unresolved_kind: str,
) -> None:
    """Require exact dependency and readiness facts before reconciliation."""
    seeded_project: tuple[WorkflowDomain, int] | None = None
    future_story_ids: tuple[int, int] | None = None
    if unresolved_kind == "dependency":
        domain, project_id, future_story_ids = (
            _seed_terminal_project_with_future_stories(engine)
        )
        seeded_project = (domain, project_id)
    context = _accepted_replacement(
        engine,
        monkeypatch,
        provenance_path=tmp_path / f"{unresolved_kind}.json",
        seeded_project=seeded_project,
    )
    with Session(engine) as session:
        if unresolved_kind == "dependency":
            assert future_story_ids is not None
            dependent_story_id, prerequisite_story_id = future_story_ids
            for story_id in future_story_ids:
                story = session.get(UserStory, story_id)
                assert story is not None
                story.status = StoryStatus.TO_DO
                session.add(story)
            session.add(
                UserStoryDependency(
                    product_id=context.project_id,
                    dependent_story_id=dependent_story_id,
                    prerequisite_story_id=prerequisite_story_id,
                    status="proposed",
                    source="manual_review",
                    confidence="reviewed",
                    reason="Unresolved replacement-scope dependency.",
                )
            )
        else:
            story = session.exec(
                select(UserStory).where(col(UserStory.product_id) == context.project_id)
            ).one()
            story.story_points = None
            session.add(story)
        session.commit()

    position = _domain(engine).position(context.project_id)
    reconciliation = _decision(
        position,
        "scope_extension.reconciliation",
        f"run:{context.run_id}",
    )
    assert reconciliation.category is NodeCategory.BLOCKED
    assert (
        reconciliation.reason_code
        == {
            "dependency": "STORY_DEPENDENCIES_UNREVIEWED",
            "readiness": "STORY_READINESS_INCOMPLETE",
        }[unresolved_kind]
    )
    assert position.terminal is False


def test_new_unresolved_readiness_after_reconciliation_remains_visible(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Retire exact historical facts without masking later readiness work."""
    context = _accepted_replacement(
        engine,
        monkeypatch,
        provenance_path=tmp_path / "newer-readiness-work.json",
    )
    reconciled = context.domain.transition(
        _reconcile_request(
            context,
            authority_id=context.replacement_authority_id,
            authority_fingerprint=context.replacement_authority_fingerprint,
            idempotency_key="task-13-reconcile-before-new-work",
        )
    )
    assert reconciled.ok is True
    assert context.domain.position(context.project_id).terminal is True

    with Session(engine) as session:
        story = session.exec(
            select(UserStory).where(col(UserStory.product_id) == context.project_id)
        ).one()
        story.story_points = None
        session.add(story)
        session.commit()

    restarted = _domain(engine).position(context.project_id)
    pending_review = _decision(restarted, "planning.story_readiness")
    assert pending_review.category is NodeCategory.AVAILABLE
    assert pending_review.reason_code == "STORY_READINESS_REPAIR_REQUIRED"
    assert restarted.terminal is False

    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(context.project_id)
    reversed_values: dict[str, object] = {}
    for field_name in snapshot.__class__.model_fields:
        value = getattr(snapshot, field_name)
        if field_name != "project" and isinstance(value, tuple):
            reversed_values[field_name] = tuple(reversed(value))
    reversed_snapshot = snapshot.model_copy(update=reversed_values)
    reversed_position = project_graph().evaluate(
        reversed_snapshot,
        restarted.evaluated_at,
    )
    assert reversed_position.fact_fingerprint == restarted.fact_fingerprint
    assert tuple(
        item.decision_fingerprint for item in reversed_position.decisions
    ) == tuple(item.decision_fingerprint for item in restarted.decisions)


@pytest.mark.parametrize(
    ("review_state", "expected"),
    [
        (
            "pending",
            ("vision.review", NodeCategory.WAITING, "VISION_REVIEW_REQUIRED"),
        ),
        (
            "rejected",
            (
                "vision.generate",
                NodeCategory.AVAILABLE,
                "VISION_REVISION_REQUIRED",
            ),
        ),
    ],
)
def test_newer_vision_work_after_reconciliation_is_not_masked(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    review_state: str,
    expected: tuple[str, NodeCategory, str],
) -> None:
    """Surface required or recovery routing for newer downstream rows."""
    context = _accepted_replacement(
        engine,
        monkeypatch,
        provenance_path=tmp_path / f"new-vision-{review_state}.json",
    )
    reconciled = context.domain.transition(
        _reconcile_request(
            context,
            authority_id=context.replacement_authority_id,
            authority_fingerprint=context.replacement_authority_fingerprint,
            idempotency_key=f"task-13-reconcile-before-{review_state}-vision",
        )
    )
    assert reconciled.ok is True
    assert context.domain.position(context.project_id).terminal is True

    with Session(engine) as session:
        vision_id = _append_vision_review_state(
            session,
            context=context,
            decision=review_state,
        )
        session.commit()

    position = _domain(engine).position(context.project_id)
    node_id, category, reason_code = expected
    assert vision_id > 0
    visible = _decision(position, node_id)
    assert visible.category is category
    assert visible.reason_code == reason_code
    assert visible.recommendation_kind in {
        RecommendationKind.REQUIRED,
        RecommendationKind.RECOVERY,
    }
    assert position.terminal is False


def test_pending_vision_suppresses_scope_start_until_reviewed(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Permit optional scope re-entry only after current Vision work resolves."""
    context = _accepted_replacement(
        engine,
        monkeypatch,
        provenance_path=tmp_path / "terminal-only-vision.json",
    )
    reconciled = context.domain.transition(
        _reconcile_request(
            context,
            authority_id=context.replacement_authority_id,
            authority_fingerprint=context.replacement_authority_fingerprint,
            idempotency_key="task-13-terminal-only-reconcile-vision",
        )
    )
    assert reconciled.ok is True
    terminal_start = _decision(
        context.domain.position(context.project_id),
        "scope_extension.start",
    )

    with Session(engine) as session:
        vision_id = _append_vision_review_state(
            session,
            context=context,
            decision="pending",
        )
        session.commit()

    pending = context.domain.position(context.project_id)
    visible = _decision(pending, "vision.review")
    current_attempt = _start_request_for_position(
        context,
        pending,
        fallback=terminal_start,
        idempotency_key="task-13-start-during-pending-vision",
    )
    before_runs = _discovery_run_count(engine, context.project_id)
    rejected_current = context.domain.transition(current_attempt)
    assert rejected_current.ok is False
    assert rejected_current.error is not None
    assert rejected_current.error.code in {
        WorkflowErrorCode.STALE_POSITION,
        WorkflowErrorCode.TRANSITION_NOT_AVAILABLE,
    }
    rejected_old = context.domain.transition(
        context.old_start_request.model_copy(
            update={"idempotency_key": "task-13-old-start-during-pending-vision"}
        )
    )
    assert rejected_old.ok is False
    assert rejected_old.error is not None
    assert rejected_old.error.code is WorkflowErrorCode.STALE_POSITION
    assert _discovery_run_count(engine, context.project_id) == before_runs
    assert visible.category is NodeCategory.WAITING
    assert pending.terminal is False
    assert not any(
        item.node_id == "scope_extension.start" for item in pending.decisions
    )

    with Session(engine) as session:
        vision = session.get(VisionArtifact, vision_id)
        assert vision is not None
        accepted_fingerprint = vision.content_fingerprint
    accepted = context.domain.transition(
        DecideVision(
            **_guards(context.domain, context.project_id, "vision.review"),
            idempotency_key="task-13-accept-current-vision",
            vision_artifact_id=vision_id,
            artifact_fingerprint=accepted_fingerprint,
            decision="accepted",
            rationale="Current replacement-scope Vision is accepted.",
        )
    )
    assert accepted.ok is True, accepted.error
    _assert_fresh_optional_start(context, terminal_start)


def test_unresolved_readiness_suppresses_scope_start_until_repaired(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Restore optional scope re-entry only after current Story readiness."""
    context = _accepted_replacement(
        engine,
        monkeypatch,
        provenance_path=tmp_path / "terminal-only-readiness.json",
    )
    reconciled = context.domain.transition(
        _reconcile_request(
            context,
            authority_id=context.replacement_authority_id,
            authority_fingerprint=context.replacement_authority_fingerprint,
            idempotency_key="task-13-terminal-only-reconcile-readiness",
        )
    )
    assert reconciled.ok is True
    terminal_start = _decision(
        context.domain.position(context.project_id),
        "scope_extension.start",
    )

    with Session(engine) as session:
        story = session.exec(
            select(UserStory).where(col(UserStory.product_id) == context.project_id)
        ).one()
        assert story.story_id is not None
        story_id = story.story_id
        assert story.story_points is not None
        original_story_points = story.story_points
        story.story_points = None
        session.add(story)
        session.commit()
    pending = context.domain.position(context.project_id)
    current_attempt = _start_request_for_position(
        context,
        pending,
        fallback=terminal_start,
        idempotency_key="task-13-start-during-readiness-repair",
    )
    before_runs = _discovery_run_count(engine, context.project_id)
    rejected = context.domain.transition(current_attempt)
    assert rejected.ok is False
    assert rejected.error is not None
    assert rejected.error.code in {
        WorkflowErrorCode.STALE_POSITION,
        WorkflowErrorCode.TRANSITION_NOT_AVAILABLE,
    }
    assert _discovery_run_count(engine, context.project_id) == before_runs
    assert pending.terminal is False
    assert _decision(pending, "planning.story_readiness").category is (
        NodeCategory.AVAILABLE
    )
    assert not any(
        item.node_id == "scope_extension.start" for item in pending.decisions
    )

    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.story_points = original_story_points
        session.add(story)
        session.commit()
    with Session(engine) as session:
        triage = session.exec(
            select(PostSprintTriage).where(
                col(PostSprintTriage.project_id) == context.project_id
            )
        ).one()
        sprint_id = triage.sprint_id
    correction = context.domain.transition(
        RecordPostSprintTriage(
            **_guards(
                context.domain,
                context.project_id,
                "execution.post_sprint_triage",
                f"sprint:{sprint_id}",
            ),
            idempotency_key="task-13-readiness-triage-correction",
            sprint_id=sprint_id,
            impact="none",
            canonical_payload={
                "summary": "Story readiness repair restored accepted planning facts."
            },
        )
    )
    assert correction.ok is True
    _assert_fresh_optional_start(context, terminal_start)


def test_pending_triage_suppresses_scope_start_until_recorded(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Compose execution triage into terminal-only optional re-entry."""
    context = _accepted_replacement(
        engine,
        monkeypatch,
        provenance_path=tmp_path / "terminal-only-triage.json",
    )
    reconciled = context.domain.transition(
        _reconcile_request(
            context,
            authority_id=context.replacement_authority_id,
            authority_fingerprint=context.replacement_authority_fingerprint,
            idempotency_key="task-13-terminal-only-reconcile-triage",
        )
    )
    assert reconciled.ok is True
    terminal_start = _decision(
        context.domain.position(context.project_id),
        "scope_extension.start",
    )

    with Session(engine) as session:
        triage = session.exec(
            select(PostSprintTriage).where(
                col(PostSprintTriage.project_id) == context.project_id
            )
        ).one()
        sprint_id = triage.sprint_id
        session.delete(triage)
        session.commit()

    pending = context.domain.position(context.project_id)
    triage_decision = _decision(
        pending,
        "execution.post_sprint_triage",
        f"sprint:{sprint_id}",
    )
    current_attempt = _start_request_for_position(
        context,
        pending,
        fallback=terminal_start,
        idempotency_key="task-13-start-during-pending-triage",
    )
    before_runs = _discovery_run_count(engine, context.project_id)
    rejected = context.domain.transition(current_attempt)
    assert rejected.ok is False
    assert rejected.error is not None
    assert rejected.error.code in {
        WorkflowErrorCode.STALE_POSITION,
        WorkflowErrorCode.TRANSITION_NOT_AVAILABLE,
    }
    assert _discovery_run_count(engine, context.project_id) == before_runs
    assert triage_decision.category is NodeCategory.AVAILABLE
    assert pending.terminal is False
    assert "scope_extension.start" not in pending.available_nodes

    triaged = context.domain.transition(
        RecordPostSprintTriage(
            **_guards(
                context.domain,
                context.project_id,
                "execution.post_sprint_triage",
                f"sprint:{sprint_id}",
            ),
            idempotency_key="task-13-record-current-triage",
            sprint_id=sprint_id,
            impact="none",
            canonical_payload={"summary": "Current execution is triaged."},
        )
    )
    assert triaged.ok is True
    _assert_fresh_optional_start(context, terminal_start)


def test_issue_193_old_extension_actions_are_stale_after_completed_run(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reject old advertised actions after the applied extension is closed."""
    context = _accepted_replacement(
        engine,
        monkeypatch,
        provenance_path=tmp_path / "accepted-amendment.json",
    )
    domain = context.domain
    project_id = context.project_id
    run_id = context.run_id
    replacement = context.replacement
    reconciled = domain.transition(
        _reconcile_request(
            context,
            authority_id=context.replacement_authority_id,
            authority_fingerprint=context.replacement_authority_fingerprint,
            idempotency_key="task-13-reconcile",
        )
    )
    assert reconciled.ok is True

    completed_position = domain.position(project_id)
    scope_required_or_recovery = tuple(
        item
        for item in completed_position.decisions
        if item.child_graph_id == "scope_extension"
        and item.recommendation_kind
        in {RecommendationKind.REQUIRED, RecommendationKind.RECOVERY}
    )
    assert scope_required_or_recovery == ()
    assert not any(
        item.node_id == "scope_extension.registration"
        and item.instance_key == f"run:{run_id}"
        for item in completed_position.decisions
    )
    fresh_start = _decision(completed_position, "scope_extension.start")
    assert completed_position.terminal is True, tuple(
        (item.node_id, item.category.value, item.reason_code)
        for item in completed_position.decisions
        if item.recommendation_kind
        in {RecommendationKind.REQUIRED, RecommendationKind.RECOVERY}
    )
    assert fresh_start.category is NodeCategory.AVAILABLE
    assert fresh_start.recommendation_kind is RecommendationKind.OPTIONAL_REENTRY
    assert (
        fresh_start.decision_fingerprint
        != context.advertised_start.decision_fingerprint
    )

    with Session(engine) as session:
        before_specs = len(
            session.exec(
                select(SpecRegistry).where(col(SpecRegistry.product_id) == project_id)
            ).all()
        )
        before_runs = len(
            session.exec(
                select(DiscoveryRun).where(col(DiscoveryRun.project_id) == project_id)
            ).all()
        )
        assert (
            len(
                session.exec(
                    select(ScopeExtensionRegistration).where(
                        col(ScopeExtensionRegistration.project_id) == project_id
                    )
                ).all()
            )
            == 1
        )
        assert (
            len(
                session.exec(
                    select(CompiledSpecAuthority).where(
                        col(CompiledSpecAuthority.spec_version_id)
                        == replacement.spec_version_id
                    )
                ).all()
            )
            == 1
        )

    stale_registration = domain.transition(
        context.old_registration_request.model_copy(
            update={"idempotency_key": "task-13-stale-registration"}
        )
    )
    stale_start = domain.transition(
        context.old_start_request.model_copy(
            update={"idempotency_key": "task-13-stale-start"}
        )
    )
    for result in (stale_registration, stale_start):
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is WorkflowErrorCode.STALE_POSITION

    with Session(engine) as session:
        assert (
            len(
                session.exec(
                    select(SpecRegistry).where(
                        col(SpecRegistry.product_id) == project_id
                    )
                ).all()
            )
            == before_specs
        )
        assert (
            len(
                session.exec(
                    select(DiscoveryRun).where(
                        col(DiscoveryRun.project_id) == project_id
                    )
                ).all()
            )
            == before_runs
        )

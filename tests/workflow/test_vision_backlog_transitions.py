"""Persisted Vision and Backlog graph transition tests."""

from __future__ import annotations

import ast
import inspect
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypedDict, get_args

import pytest
from pydantic import TypeAdapter
from sqlmodel import Session, col, select

import services.agent_workbench.backlog_active_reset as backlog_active_reset_module
import services.agent_workbench.backlog_phase as backlog_phase_module
import services.agent_workbench.backlog_reconciliation as backlog_reconciliation_module
import services.agent_workbench.vision_phase as vision_phase_module
from models.core import Product, Sprint, SprintStory, Team, UserStory
from models.enums import SprintStatus, StoryStatus, WorkflowEventType
from models.events import WorkflowEvent
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import (
    BacklogArtifact,
    BacklogArtifactDecision,
    BacklogAuthorityReconciliation,
    VisionArtifact,
    VisionArtifactDecision,
    WorkflowTransitionReceipt,
)
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.specs.authority_selection import pending_authority_fingerprint
from utils.spec_schemas import SpecAuthorityCompilationSuccess
from workflow.clock import FixedClock
from workflow.contracts import (
    JsonObject,
    NodeDecision,
    TransitionResult,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.definitions.product_definition import product_definition_graph
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash, canonical_json, fact_fingerprint
from workflow.requests import (
    DecideBacklog,
    DecideVision,
    ReconcileBacklog,
    RecordBacklogDraft,
    RecordVisionDraft,
    TransitionRequest,
)

if TYPE_CHECKING:
    from types import ModuleType

    from sqlalchemy.engine import Engine

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)
EXPECTED_REQUEST_VARIANT_COUNT = 30
EXPECTED_VISION_VERSION_COUNT = 2
CALLER_SESSION_FUNCTIONS: dict[ModuleType, frozenset[str]] = {
    vision_phase_module: frozenset(
        {"record_vision_draft_in_session", "record_vision_decision_in_session"}
    ),
    backlog_phase_module: frozenset(
        {
            "record_backlog_draft_in_session",
            "record_backlog_decision_in_session",
            "persist_accepted_backlog_in_session",
        }
    ),
    backlog_active_reset_module: frozenset(
        {"replay_active_backlog_reset", "reset_active_backlog_rows"}
    ),
    backlog_reconciliation_module: frozenset(
        {"reconcile_active_backlog", "reconcile_stale_backlog_in_session"}
    ),
}


class _RequestGuards(TypedDict):
    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    instance_key: str | None
    actor: str
    correlation_id: str


def _authority_artifact() -> SpecAuthorityCompilationSuccess:
    return SpecAuthorityCompilationSuccess(
        scope_themes=["Product definition"],
        invariants=[],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
    )


def _seed_accepted_authority(
    engine: Engine,
    *,
    name: str = "Task 10 Project",
) -> tuple[int, int, str]:
    artifact = _authority_artifact()
    with Session(engine) as session:
        project = Product(name=name, origin="greenfield")
        session.add(project)
        session.flush()
        assert project.product_id is not None
        spec = SpecRegistry(
            product_id=project.product_id,
            spec_hash="sha256:task-10-spec",
            content='{"scope":"task-10"}',
            status="approved",
            approved_at=EVALUATED_AT,
            approved_by="operator@example.com",
        )
        session.add(spec)
        session.flush()
        assert spec.spec_version_id is not None
        authority = CompiledSpecAuthority(
            spec_version_id=spec.spec_version_id,
            compiler_version=artifact.compiler_version,
            prompt_hash=artifact.prompt_hash,
            compiled_at=EVALUATED_AT,
            compiled_artifact_json=artifact.model_dump_json(),
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
        session.add(authority)
        session.flush()
        assert authority.authority_id is not None
        authority_fingerprint = pending_authority_fingerprint(authority)
        assert authority_fingerprint is not None
        session.add(
            SpecAuthorityAcceptance(
                product_id=project.product_id,
                spec_version_id=spec.spec_version_id,
                status="accepted",
                policy="manual",
                decided_by="operator@example.com",
                decided_at=EVALUATED_AT,
                rationale="Current authority accepted.",
                compiler_version=authority.compiler_version,
                prompt_hash=authority.prompt_hash,
                spec_hash=spec.spec_hash,
                pending_authority_id=authority.authority_id,
                authority_fingerprint=authority_fingerprint,
                review_fingerprint="sha256:review",
                terminal_decision_key="task-10-authority",
            )
        )
        session.commit()
        return project.product_id, authority.authority_id, authority_fingerprint


def _domain(engine: Engine) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=product_definition_graph(),
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def _decision(position: WorkflowPosition, node_id: str) -> NodeDecision:
    return next(item for item in position.decisions if item.node_id == node_id)


def _guards(position: WorkflowPosition, node_id: str) -> _RequestGuards:
    decision = _decision(position, node_id)
    return {
        "project_id": position.project_id,
        "graph_version": position.graph_version,
        "fact_fingerprint": position.fact_fingerprint,
        "decision_fingerprint": decision.decision_fingerprint,
        "instance_key": decision.instance_key,
        "actor": "operator@example.com",
        "correlation_id": "task-10",
    }


def _vision_content(statement: str = "Build reliable product decisions.") -> JsonObject:
    return {
        "updated_components": {
            "project_name": "Task 10 Project",
            "target_user": "Product operators",
            "problem": "Workflow state can drift",
            "product_category": "Developer tool",
            "key_benefit": "Durable decisions",
            "competitors": "Manual process",
            "differentiator": "Fact-derived routing",
        },
        "product_vision_statement": statement,
        "is_complete": True,
        "clarifying_questions": [],
    }


def _backlog_content(
    requirement: str = "Persist immutable workflow artifacts",
) -> JsonObject:
    return {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": requirement,
                "authority_ref": "REQ.task-10",
                "capability_hint": None,
                "value_driver": "Strategic",
                "justification": "Keeps workflow position restart-safe.",
                "estimated_effort": "M",
                "technical_note": None,
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }


def _record_vision(  # noqa: PLR0913
    domain: WorkflowDomain,
    *,
    project_id: int,
    authority_id: int,
    authority_fingerprint: str,
    content: JsonObject | None = None,
    supersedes_vision_artifact_id: int | None = None,
    idempotency_key: str = "record-vision",
) -> TransitionResult:
    position = domain.position(project_id)
    canonical_content = content or _vision_content()
    return domain.transition(
        RecordVisionDraft(
            **_guards(position, "vision.generate"),
            idempotency_key=idempotency_key,
            authority_id=authority_id,
            authority_fingerprint=authority_fingerprint,
            canonical_content=canonical_content,
            content_fingerprint=canonical_hash(canonical_content),
            supersedes_vision_artifact_id=supersedes_vision_artifact_id,
        )
    )


def _decide_vision(  # noqa: PLR0913
    domain: WorkflowDomain,
    *,
    project_id: int,
    artifact_id: int,
    fingerprint: str,
    decision: str = "accepted",
    idempotency_key: str = "decide-vision",
) -> TransitionResult:
    position = domain.position(project_id)
    return domain.transition(
        DecideVision.model_validate(
            {
                **_guards(position, "vision.review"),
                "idempotency_key": idempotency_key,
                "vision_artifact_id": artifact_id,
                "artifact_fingerprint": fingerprint,
                "decision": decision,
                "rationale": f"Vision {decision}.",
            }
        )
    )


def _record_backlog(  # noqa: PLR0913
    domain: WorkflowDomain,
    *,
    project_id: int,
    authority_id: int,
    authority_fingerprint: str,
    content: JsonObject | None = None,
    supersedes_backlog_artifact_id: int | None = None,
    idempotency_key: str = "record-backlog",
) -> TransitionResult:
    position = domain.position(project_id)
    canonical_content = content or _backlog_content()
    return domain.transition(
        RecordBacklogDraft(
            **_guards(position, "backlog.generate"),
            idempotency_key=idempotency_key,
            authority_id=authority_id,
            authority_fingerprint=authority_fingerprint,
            canonical_content=canonical_content,
            content_fingerprint=canonical_hash(canonical_content),
            supersedes_backlog_artifact_id=supersedes_backlog_artifact_id,
        )
    )


def _decide_backlog(  # noqa: PLR0913
    domain: WorkflowDomain,
    *,
    project_id: int,
    artifact_id: int,
    fingerprint: str,
    decision: str = "accepted",
    idempotency_key: str = "decide-backlog",
) -> TransitionResult:
    position = domain.position(project_id)
    return domain.transition(
        DecideBacklog.model_validate(
            {
                **_guards(position, "backlog.review"),
                "idempotency_key": idempotency_key,
                "backlog_artifact_id": artifact_id,
                "artifact_fingerprint": fingerprint,
                "decision": decision,
                "rationale": f"Backlog {decision}.",
            }
        )
    )


def _accept_vision(
    domain: WorkflowDomain,
    *,
    project_id: int,
    authority_id: int,
    authority_fingerprint: str,
) -> tuple[int, str]:
    recorded = _record_vision(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )
    assert recorded.ok is True
    artifact_id = recorded.output["vision_artifact_id"]
    fingerprint = recorded.output["content_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)
    accepted = _decide_vision(
        domain,
        project_id=project_id,
        artifact_id=artifact_id,
        fingerprint=fingerprint,
    )
    assert accepted.ok is True
    return artifact_id, fingerprint


def _accept_backlog(
    domain: WorkflowDomain,
    *,
    project_id: int,
    authority_id: int,
    authority_fingerprint: str,
) -> tuple[int, str]:
    recorded = _record_backlog(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )
    artifact_id = recorded.output["backlog_artifact_id"]
    fingerprint = recorded.output["content_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)
    accepted = _decide_backlog(
        domain,
        project_id=project_id,
        artifact_id=artifact_id,
        fingerprint=fingerprint,
    )
    assert accepted.ok is True
    return artifact_id, fingerprint


def _replace_accepted_authority(
    engine: Engine,
    project_id: int,
    *,
    suffix: str,
) -> tuple[int, str]:
    artifact = _authority_artifact()
    with Session(engine) as session:
        old_spec = session.exec(
            select(SpecRegistry).where(
                col(SpecRegistry.product_id) == project_id,
                col(SpecRegistry.status) == "approved",
            )
        ).one()
        old_spec.status = "superseded"
        replacement_spec = SpecRegistry(
            product_id=project_id,
            spec_hash=f"sha256:replacement-spec-{suffix}",
            content=canonical_json({"scope": suffix}),
            status="approved",
            approved_at=EVALUATED_AT + timedelta(minutes=1),
            approved_by="operator@example.com",
        )
        session.add_all([old_spec, replacement_spec])
        session.flush()
        assert replacement_spec.spec_version_id is not None
        replacement = CompiledSpecAuthority(
            spec_version_id=replacement_spec.spec_version_id,
            compiler_version=artifact.compiler_version,
            prompt_hash=artifact.prompt_hash,
            compiled_at=EVALUATED_AT + timedelta(minutes=1),
            compiled_artifact_json=artifact.model_dump_json(),
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
        session.add(replacement)
        session.flush()
        assert replacement.authority_id is not None
        fingerprint = pending_authority_fingerprint(replacement)
        assert fingerprint is not None
        session.add(
            SpecAuthorityAcceptance(
                product_id=project_id,
                spec_version_id=replacement_spec.spec_version_id,
                status="accepted",
                policy="manual",
                decided_by="operator@example.com",
                decided_at=EVALUATED_AT + timedelta(minutes=1),
                rationale="Replacement authority accepted.",
                compiler_version=replacement.compiler_version,
                prompt_hash=replacement.prompt_hash,
                spec_hash=replacement_spec.spec_hash,
                pending_authority_id=replacement.authority_id,
                authority_fingerprint=fingerprint,
                review_fingerprint=f"sha256:replacement-review-{suffix}",
                terminal_decision_key=f"task-10-replacement-{suffix}",
            )
        )
        session.commit()
        return replacement.authority_id, fingerprint


def _select_story_in_sprint(
    engine: Engine,
    *,
    project_id: int,
    story_id: int,
    suffix: str,
) -> None:
    with Session(engine) as session:
        team = Team(name=f"Task 10 Team {suffix}")
        session.add(team)
        session.flush()
        assert team.team_id is not None
        sprint = Sprint(
            product_id=project_id,
            team_id=team.team_id,
            goal="Protect selected work",
            status=SprintStatus.PLANNED,
        )
        session.add(sprint)
        session.flush()
        assert sprint.sprint_id is not None
        session.add(SprintStory(sprint_id=sprint.sprint_id, story_id=story_id))
        session.commit()


def test_closed_request_union_adds_exactly_five_product_definition_variants() -> None:
    """Keep the five Task 10 variants in the expanded closed request union."""
    variants = set(get_args(TransitionRequest.__value__))
    added = {
        RecordVisionDraft,
        DecideVision,
        RecordBacklogDraft,
        DecideBacklog,
        ReconcileBacklog,
    }

    assert added <= variants
    assert len(variants) == EXPECTED_REQUEST_VARIANT_COUNT
    assert (
        TypeAdapter(TransitionRequest)
        .validate_python(
            {
                "kind": "record_vision_draft",
                "project_id": 1,
                "graph_version": "agileforge.workflow.v1",
                "fact_fingerprint": "sha256:facts",
                "decision_fingerprint": "sha256:decision",
                "idempotency_key": "union-vision",
                "actor": "operator",
                "authority_id": 2,
                "authority_fingerprint": "sha256:authority",
                "canonical_content": _vision_content(),
                "content_fingerprint": canonical_hash(_vision_content()),
            }
        )
        .node_id
        == "vision.generate"
    )


def test_record_requests_bind_exact_authority_and_canonical_content(
    engine: Engine,
) -> None:
    """Reject record requests whose authority or content binding changed."""
    project_id, authority_id, authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine)
    position = domain.position(project_id)
    content = _vision_content()

    wrong_authority = domain.transition(
        RecordVisionDraft(
            **_guards(position, "vision.generate"),
            idempotency_key="wrong-authority",
            authority_id=authority_id,
            authority_fingerprint="sha256:wrong-authority",
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
        )
    )
    assert wrong_authority.ok is False
    assert wrong_authority.error is not None
    assert wrong_authority.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT

    fresh = domain.position(project_id)
    wrong_content = domain.transition(
        RecordVisionDraft(
            **_guards(fresh, "vision.generate"),
            idempotency_key="wrong-content",
            authority_id=authority_id,
            authority_fingerprint=authority_fingerprint,
            canonical_content=content,
            content_fingerprint="sha256:wrong-content",
        )
    )
    assert wrong_content.ok is False
    with Session(engine) as session:
        assert session.exec(select(VisionArtifact)).all() == []


def test_parallel_product_definition_branches_progress_independently(
    engine: Engine,
) -> None:
    """Persist either draft first without serializing the other branch."""
    vision_project, vision_authority, vision_authority_fingerprint = (
        _seed_accepted_authority(engine, name="Vision First")
    )
    backlog_project, backlog_authority, backlog_authority_fingerprint = (
        _seed_accepted_authority(engine, name="Backlog First")
    )
    domain = _domain(engine)

    initial_vision = domain.position(vision_project)
    initial_backlog = domain.position(backlog_project)
    assert initial_vision.available_nodes == ("vision.generate", "backlog.generate")
    assert initial_backlog.available_nodes == ("vision.generate", "backlog.generate")

    vision_result = _record_vision(
        domain,
        project_id=vision_project,
        authority_id=vision_authority,
        authority_fingerprint=vision_authority_fingerprint,
        idempotency_key="parallel-vision-first",
    )
    backlog_result = _record_backlog(
        domain,
        project_id=backlog_project,
        authority_id=backlog_authority,
        authority_fingerprint=backlog_authority_fingerprint,
        idempotency_key="parallel-backlog-first",
    )

    assert vision_result.ok is True
    assert backlog_result.ok is True
    after_vision = domain.position(vision_project)
    after_backlog = domain.position(backlog_project)
    assert "vision.review" in after_vision.waiting_nodes
    assert "backlog.generate" in after_vision.available_nodes
    assert "backlog.review" in after_backlog.waiting_nodes
    assert "vision.generate" in after_backlog.available_nodes


def test_vision_correction_appends_superseding_version_and_decisions(
    engine: Engine,
) -> None:
    """Append corrected Vision content without mutating the accepted version."""
    project_id, authority_id, authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine)
    first_id, first_fingerprint = _accept_vision(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )

    replacement = _record_vision(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
        content=_vision_content("Build auditable product decisions."),
        supersedes_vision_artifact_id=first_id,
        idempotency_key="record-vision-v2",
    )
    assert replacement.ok is True

    with Session(engine) as session:
        artifacts = session.exec(
            select(VisionArtifact).order_by(col(VisionArtifact.vision_artifact_id))
        ).all()
        decisions = session.exec(select(VisionArtifactDecision)).all()
        assert len(artifacts) == EXPECTED_VISION_VERSION_COUNT
        assert artifacts[0].content_fingerprint == first_fingerprint
        assert artifacts[1].supersedes_vision_artifact_id == first_id
        assert len(decisions) == 1
        assert decisions[0].decision == "accepted"


def test_contradictory_terminal_vision_decision_fails_closed(
    engine: Engine,
) -> None:
    """Reject an exact duplicate decision as conflict, not bad availability."""
    project_id, authority_id, authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine)
    recorded = _record_vision(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )
    artifact_id = recorded.output["vision_artifact_id"]
    fingerprint = recorded.output["content_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)
    waiting = domain.position(project_id)
    review_guards = _guards(waiting, "vision.review")

    accepted = domain.transition(
        DecideVision(
            **review_guards,
            idempotency_key="decide-vision-first",
            vision_artifact_id=artifact_id,
            artifact_fingerprint=fingerprint,
            decision="accepted",
            rationale="Accept the immutable Vision.",
        )
    )
    assert accepted.ok is True

    contradictory = DecideVision(
        **review_guards,
        idempotency_key="contradictory-vision",
        vision_artifact_id=artifact_id,
        artifact_fingerprint=fingerprint,
        decision="rejected",
        rationale="Contradiction must not append.",
    )
    result = domain.transition(contradictory)

    wrong_guard_position = domain.position(project_id)
    wrong_guard = domain.transition(
        DecideVision(
            **_guards(wrong_guard_position, "vision.generate"),
            idempotency_key="wrong-guard-vision",
            vision_artifact_id=artifact_id,
            artifact_fingerprint=fingerprint,
            decision="feedback",
            rationale="This request is deliberately guarded by the wrong node.",
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert wrong_guard.ok is False
    assert wrong_guard.error is not None
    assert wrong_guard.error.code is WorkflowErrorCode.TRANSITION_NOT_AVAILABLE
    with Session(engine) as session:
        decisions = session.exec(select(VisionArtifactDecision)).all()
        assert len(decisions) == 1
        assert decisions[0].decision == "accepted"


def test_contradictory_terminal_backlog_decision_fails_closed(
    engine: Engine,
) -> None:
    """Apply the same exact-review conflict contract to Backlog decisions."""
    project_id, authority_id, authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine)
    recorded = _record_backlog(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )
    artifact_id = recorded.output["backlog_artifact_id"]
    fingerprint = recorded.output["content_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)
    waiting = domain.position(project_id)
    review_guards = _guards(waiting, "backlog.review")

    accepted = domain.transition(
        DecideBacklog(
            **review_guards,
            idempotency_key="decide-backlog-first",
            backlog_artifact_id=artifact_id,
            artifact_fingerprint=fingerprint,
            decision="accepted",
            rationale="Accept the immutable Backlog.",
        )
    )
    assert accepted.ok is True

    contradictory = domain.transition(
        DecideBacklog(
            **review_guards,
            idempotency_key="contradictory-backlog",
            backlog_artifact_id=artifact_id,
            artifact_fingerprint=fingerprint,
            decision="rejected",
            rationale="Contradiction must not append.",
        )
    )
    wrong_guard_position = domain.position(project_id)
    wrong_guard = domain.transition(
        DecideBacklog(
            **_guards(wrong_guard_position, "backlog.generate"),
            idempotency_key="wrong-guard-backlog",
            backlog_artifact_id=artifact_id,
            artifact_fingerprint=fingerprint,
            decision="feedback",
            rationale="This request is deliberately guarded by the wrong node.",
        )
    )

    assert contradictory.ok is False
    assert contradictory.error is not None
    assert contradictory.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert wrong_guard.ok is False
    assert wrong_guard.error is not None
    assert wrong_guard.error.code is WorkflowErrorCode.TRANSITION_NOT_AVAILABLE
    with Session(engine) as session:
        decisions = session.exec(select(BacklogArtifactDecision)).all()
        assert len(decisions) == 1
        assert decisions[0].decision == "accepted"


def test_accepting_backlog_persists_artifact_decision_and_validated_stories(
    engine: Engine,
) -> None:
    """Persist accepted Backlog facts and validated active stories atomically."""
    project_id, authority_id, authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine)
    _accept_vision(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )
    recorded = _record_backlog(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )
    assert recorded.ok is True
    artifact_id = recorded.output["backlog_artifact_id"]
    fingerprint = recorded.output["content_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)

    accepted = _decide_backlog(
        domain,
        project_id=project_id,
        artifact_id=artifact_id,
        fingerprint=fingerprint,
    )

    assert accepted.ok is True
    with Session(engine) as session:
        artifact = session.get(BacklogArtifact, artifact_id)
        decisions = session.exec(select(BacklogArtifactDecision)).all()
        stories = session.exec(select(UserStory)).all()
        assert artifact is not None
        assert artifact.content_fingerprint == fingerprint
        assert len(decisions) == 1
        assert decisions[0].artifact_fingerprint == fingerprint
        assert [story.title for story in stories] == [
            "Persist immutable workflow artifacts"
        ]
        assert stories[0].story_origin == "backlog_seed"


def test_progressed_active_backlog_blocks_replacement_without_partial_decision(
    engine: Engine,
) -> None:
    """Preserve progressed stories and roll back the attempted replacement."""
    project_id, authority_id, authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine)
    _accept_vision(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )
    with Session(engine) as session:
        session.add(
            UserStory(
                product_id=project_id,
                title="Progressed story",
                status=StoryStatus.IN_PROGRESS,
                story_origin="backlog_seed",
                is_refined=False,
                is_superseded=False,
            )
        )
        session.commit()
    recorded = _record_backlog(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )
    assert recorded.ok is True
    artifact_id = recorded.output["backlog_artifact_id"]
    fingerprint = recorded.output["content_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)

    result = _decide_backlog(
        domain,
        project_id=project_id,
        artifact_id=artifact_id,
        fingerprint=fingerprint,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    with Session(engine) as session:
        assert session.exec(select(BacklogArtifactDecision)).all() == []
        stories = session.exec(select(UserStory)).all()
        assert len(stories) == 1
        assert stories[0].title == "Progressed story"
        assert stories[0].is_superseded is False


def test_authority_reconciliation_binds_replacement_and_exact_stale_artifact_ids(  # noqa: PLR0915
    engine: Engine,
) -> None:
    """Persist explicit reconciliation against exact replacement authority facts."""
    project_id, authority_id, authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine)
    vision_id, vision_fingerprint = _accept_vision(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )
    backlog_recorded = _record_backlog(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )
    backlog_id = backlog_recorded.output["backlog_artifact_id"]
    backlog_fingerprint = backlog_recorded.output["content_fingerprint"]
    assert isinstance(backlog_id, int)
    assert isinstance(backlog_fingerprint, str)
    assert (
        _decide_backlog(
            domain,
            project_id=project_id,
            artifact_id=backlog_id,
            fingerprint=backlog_fingerprint,
        ).ok
        is True
    )

    replacement_artifact = _authority_artifact()
    with Session(engine) as session:
        old_spec = session.exec(
            select(SpecRegistry).where(SpecRegistry.product_id == project_id)
        ).one()
        old_spec.status = "superseded"
        replacement_spec = SpecRegistry(
            product_id=project_id,
            spec_hash="sha256:replacement-spec",
            content='{"scope":"replacement"}',
            status="approved",
            approved_at=EVALUATED_AT + timedelta(minutes=1),
            approved_by="operator@example.com",
        )
        session.add_all([old_spec, replacement_spec])
        session.flush()
        assert replacement_spec.spec_version_id is not None
        replacement_authority = CompiledSpecAuthority(
            spec_version_id=replacement_spec.spec_version_id,
            compiler_version=replacement_artifact.compiler_version,
            prompt_hash=replacement_artifact.prompt_hash,
            compiled_at=EVALUATED_AT + timedelta(minutes=1),
            compiled_artifact_json=replacement_artifact.model_dump_json(),
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
        session.add(replacement_authority)
        session.flush()
        assert replacement_authority.authority_id is not None
        replacement_fingerprint = pending_authority_fingerprint(replacement_authority)
        assert replacement_fingerprint is not None
        session.add(
            SpecAuthorityAcceptance(
                product_id=project_id,
                spec_version_id=replacement_spec.spec_version_id,
                status="accepted",
                policy="manual",
                decided_by="operator@example.com",
                decided_at=EVALUATED_AT + timedelta(minutes=1),
                rationale="Replacement authority accepted.",
                compiler_version=replacement_authority.compiler_version,
                prompt_hash=replacement_authority.prompt_hash,
                spec_hash=replacement_spec.spec_hash,
                pending_authority_id=replacement_authority.authority_id,
                authority_fingerprint=replacement_fingerprint,
                review_fingerprint="sha256:replacement-review",
                terminal_decision_key="task-10-replacement-authority",
            )
        )
        session.commit()
        replacement_authority_id = replacement_authority.authority_id

    position = domain.position(project_id)
    reconcile_decision = _decision(position, "backlog.reconcile")
    assert reconcile_decision.category.value == "available"
    wrong = domain.transition(
        ReconcileBacklog(
            **_guards(position, "backlog.reconcile"),
            idempotency_key="reconcile-wrong-ids",
            replacement_authority_id=replacement_authority_id,
            replacement_authority_fingerprint=replacement_fingerprint,
            affected_artifact_ids=(backlog_id,),
        )
    )
    assert wrong.ok is False

    fresh = domain.position(project_id)
    reconciled = domain.transition(
        ReconcileBacklog(
            **_guards(fresh, "backlog.reconcile"),
            idempotency_key="reconcile-exact-ids",
            replacement_authority_id=replacement_authority_id,
            replacement_authority_fingerprint=replacement_fingerprint,
            affected_artifact_ids=tuple(sorted((vision_id, backlog_id))),
        )
    )

    assert reconciled.ok is True
    with Session(engine) as session:
        rows = session.exec(select(BacklogAuthorityReconciliation)).all()
        assert len(rows) == 1
        assert rows[0].replacement_authority_id == replacement_authority_id
        assert rows[0].reconciled_by == "operator@example.com"
        assert rows[0].audit_event_id is not None
        original_audit_event_id = rows[0].audit_event_id
        assert json.loads(rows[0].affected_artifact_ids_json) == sorted(
            (vision_id, backlog_id)
        )
        old_vision = session.get(VisionArtifact, vision_id)
        old_backlog = session.get(BacklogArtifact, backlog_id)
        assert old_vision is not None
        assert old_vision.content_fingerprint == vision_fingerprint
        assert old_backlog is not None
        assert old_backlog.content_fingerprint == backlog_fingerprint
        stories = session.exec(select(UserStory)).all()
        assert stories
        assert all(story.is_superseded for story in stories)
        event = session.get(WorkflowEvent, rows[0].audit_event_id)
        assert event is not None
        expected_metadata = {
            "action": "backlog_authority_reconciled",
            "backlog_authority_reconciliation_id": (
                rows[0].backlog_authority_reconciliation_id
            ),
            "reconciled_by": rows[0].reconciled_by,
            "replacement_authority_id": replacement_authority_id,
            "replacement_authority_fingerprint": replacement_fingerprint,
            "affected_artifact_ids": sorted((vision_id, backlog_id)),
            "affected_artifacts_fingerprint": rows[0].affected_artifacts_fingerprint,
        }
        assert event.event_metadata == canonical_json(expected_metadata)
        assert rows[0].audit_event_fingerprint == canonical_hash(
            {
                "event_id": event.event_id,
                "event_type": event.event_type.value,
                "project_id": event.product_id,
                "timestamp": event.timestamp,
                "metadata": expected_metadata,
            }
        )

    baseline = domain.position(project_id)
    with Session(engine) as session:
        row = session.exec(select(BacklogAuthorityReconciliation)).one()
        row.reconciled_by = "tampered@example.com"
        session.add(row)
        session.commit()
    with pytest.raises(WorkflowFactLoadError):
        domain.position(project_id)

    with Session(engine) as session:
        row = session.exec(select(BacklogAuthorityReconciliation)).one()
        row.reconciled_by = "operator@example.com"
        event = session.get(WorkflowEvent, row.audit_event_id)
        assert event is not None
        other_project = Product(name="Audit Drift Project", origin="greenfield")
        session.add(other_project)
        session.flush()
        assert other_project.product_id is not None
        wrong_project_id = other_project.product_id
        event.product_id = wrong_project_id
        session.add_all([row, event])
        session.commit()
    with pytest.raises(WorkflowFactLoadError):
        domain.position(project_id)

    with Session(engine) as session:
        row = session.exec(select(BacklogAuthorityReconciliation)).one()
        event = session.get(WorkflowEvent, row.audit_event_id)
        assert event is not None
        event.product_id = project_id
        event.event_metadata = canonical_json(
            {"action": "backlog_authority_reconciliation_tampered"}
        )
        session.add_all([row, event])
        session.commit()
    with pytest.raises(WorkflowFactLoadError):
        domain.position(project_id)

    with Session(engine) as session:
        row = session.exec(select(BacklogAuthorityReconciliation)).one()
        original_event = session.get(WorkflowEvent, row.audit_event_id)
        assert original_event is not None
        original_event.event_metadata = canonical_json(expected_metadata)
        replacement_event = WorkflowEvent(
            event_type=WorkflowEventType.BACKLOG_SAVED,
            product_id=project_id,
            timestamp=EVALUATED_AT,
            event_metadata=canonical_json(expected_metadata),
        )
        session.add_all([original_event, replacement_event])
        session.flush()
        assert replacement_event.event_id is not None
        row.audit_event_id = replacement_event.event_id
        session.add(row)
        session.commit()
    with pytest.raises(WorkflowFactLoadError):
        domain.position(project_id)

    with Session(engine) as session:
        row = session.exec(select(BacklogAuthorityReconciliation)).one()
        row.audit_event_id = original_audit_event_id
        session.add(row)
        session.commit()
    restored = domain.position(project_id)
    assert restored.fact_fingerprint == baseline.fact_fingerprint


def test_recorded_artifacts_and_decisions_survive_domain_restart(
    engine: Engine,
) -> None:
    """Derive the same position from persisted facts after a domain restart."""
    project_id, authority_id, authority_fingerprint = _seed_accepted_authority(engine)
    first_domain = _domain(engine)
    _accept_vision(
        first_domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )

    before = first_domain.position(project_id)
    after = _domain(engine).position(project_id)

    assert after.fact_fingerprint == before.fact_fingerprint
    assert "backlog.generate" in after.available_nodes


def test_repository_row_order_does_not_change_decisions_or_fingerprints(
    engine: Engine,
) -> None:
    """Canonicalize persisted decisions even when SQLite reverses raw rows."""
    project_id, authority_id, authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine)
    first_id, _first_fingerprint = _accept_vision(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )
    replacement = _record_vision(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
        content=_vision_content("Keep row order from changing workflow identity."),
        supersedes_vision_artifact_id=first_id,
        idempotency_key="record-row-order-vision",
    )
    replacement_id = replacement.output["vision_artifact_id"]
    replacement_fingerprint = replacement.output["content_fingerprint"]
    assert isinstance(replacement_id, int)
    assert isinstance(replacement_fingerprint, str)
    assert (
        _decide_vision(
            domain,
            project_id=project_id,
            artifact_id=replacement_id,
            fingerprint=replacement_fingerprint,
            idempotency_key="decide-row-order-vision",
        ).ok
        is True
    )

    with Session(engine) as session:
        baseline = WorkflowFactRepository(session).load(project_id)
        raw_before = session.exec(
            select(VisionArtifactDecision.vision_artifact_decision_id)
        ).all()
        session.connection().exec_driver_sql("PRAGMA reverse_unordered_selects = ON")
        raw_after = session.exec(
            select(VisionArtifactDecision.vision_artifact_decision_id)
        ).all()
        reversed_snapshot = WorkflowFactRepository(session).load(project_id)

    assert len(raw_before) > 1
    assert raw_after == list(reversed(raw_before))
    assert tuple(item.decision_id for item in reversed_snapshot.review_decisions) == (
        tuple(item.decision_id for item in baseline.review_decisions)
    )
    assert fact_fingerprint(reversed_snapshot) == fact_fingerprint(baseline)
    baseline_position = product_definition_graph().evaluate(baseline, EVALUATED_AT)
    reversed_position = product_definition_graph().evaluate(
        reversed_snapshot,
        EVALUATED_AT,
    )
    assert tuple(
        (item.node_id, item.instance_key, item.decision_fingerprint)
        for item in reversed_position.decisions
    ) == tuple(
        (item.node_id, item.instance_key, item.decision_fingerprint)
        for item in baseline_position.decisions
    )


def test_extracted_services_do_not_own_caller_session_lifecycle() -> None:
    """Keep all extracted low-level mutations free of Session ownership."""
    forbidden_methods = {"close", "commit", "rollback"}
    for module, expected_names in CALLER_SESSION_FUNCTIONS.items():
        tree = ast.parse(inspect.getsource(module))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name in expected_names
        }
        assert set(functions) == set(expected_names)
        for function in functions.values():
            calls = tuple(
                node for node in ast.walk(function) if isinstance(node, ast.Call)
            )
            assert not any(
                isinstance(call.func, ast.Name) and call.func.id == "Session"
                for call in calls
            )
            assert not any(
                isinstance(call.func, ast.Attribute)
                and call.func.attr in forbidden_methods
                for call in calls
            )


def test_post_flush_handler_failure_rolls_back_then_retry_and_replay_once(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roll back flushed business/audit/receipt rows before retry and replay."""
    project_id, authority_id, authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine)
    recorded = _record_vision(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )
    artifact_id = recorded.output["vision_artifact_id"]
    fingerprint = recorded.output["content_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)
    waiting = domain.position(project_id)
    request = DecideVision(
        **_guards(waiting, "vision.review"),
        idempotency_key="rollback-retry-vision",
        vision_artifact_id=artifact_id,
        artifact_fingerprint=fingerprint,
        decision="accepted",
        rationale="Exercise atomic rollback after handler flush.",
    )

    def fail_after_handler_flush(
        _session: Session,
        _receipt: WorkflowTransitionReceipt,
        _result: TransitionResult,
        _evaluated_at: datetime,
    ) -> None:
        message = "injected post-handler flush failure"
        raise RuntimeError(message)

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(
            WorkflowDomain,
            "_complete_receipt",
            staticmethod(fail_after_handler_flush),
        )
        with pytest.raises(RuntimeError, match="post-handler flush"):
            domain.transition(request)

    with Session(engine) as session:
        assert session.exec(select(VisionArtifactDecision)).all() == []
        assert (
            session.exec(
                select(WorkflowEvent).where(
                    col(WorkflowEvent.event_type) == WorkflowEventType.VISION_SAVED
                )
            ).all()
            == []
        )
        project = session.get(Product, project_id)
        assert project is not None
        assert project.vision is None
        receipts = session.exec(select(WorkflowTransitionReceipt)).all()
        assert [item.request_kind for item in receipts] == ["record_vision_draft"]

    retried = domain.transition(request)
    replayed = domain.transition(request)

    assert retried.ok is True
    assert retried.replayed is False
    assert replayed.ok is True
    assert replayed.replayed is True
    with Session(engine) as session:
        assert len(session.exec(select(VisionArtifactDecision)).all()) == 1
        assert (
            len(
                session.exec(
                    select(WorkflowEvent).where(
                        col(WorkflowEvent.event_type) == WorkflowEventType.VISION_SAVED
                    )
                ).all()
            )
            == 1
        )
        decision_receipts = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.request_kind) == "decide_vision"
            )
        ).all()
        assert len(decision_receipts) == 1


def test_sprint_selected_story_blocks_active_backlog_replacement(
    engine: Engine,
) -> None:
    """Treat sprint selection as progressed work during Backlog acceptance."""
    project_id, authority_id, authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine)
    with Session(engine) as session:
        story = UserStory(
            product_id=project_id,
            title="Selected seed story",
            status=StoryStatus.TO_DO,
            story_origin="backlog_seed",
            is_refined=False,
            is_superseded=False,
        )
        session.add(story)
        session.flush()
        assert story.story_id is not None
        story_id = story.story_id
        session.commit()
    _select_story_in_sprint(
        engine,
        project_id=project_id,
        story_id=story_id,
        suffix="replacement",
    )
    recorded = _record_backlog(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )
    artifact_id = recorded.output["backlog_artifact_id"]
    fingerprint = recorded.output["content_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)

    result = _decide_backlog(
        domain,
        project_id=project_id,
        artifact_id=artifact_id,
        fingerprint=fingerprint,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    with Session(engine) as session:
        persisted = session.get(UserStory, story_id)
        assert persisted is not None
        assert persisted.is_superseded is False
        assert session.exec(select(BacklogArtifactDecision)).all() == []


def test_sprint_selected_story_blocks_authority_reconciliation(
    engine: Engine,
) -> None:
    """Preserve a selected active story when replacement authority is reconciled."""
    project_id, authority_id, authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine)
    _accept_vision(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )
    _backlog_id, _backlog_fingerprint = _accept_backlog(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )
    with Session(engine) as session:
        story = session.exec(
            select(UserStory).where(col(UserStory.is_superseded).is_(False))
        ).one()
        assert story.story_id is not None
        story_id = story.story_id
    _select_story_in_sprint(
        engine,
        project_id=project_id,
        story_id=story_id,
        suffix="reconciliation",
    )
    replacement_id, replacement_fingerprint = _replace_accepted_authority(
        engine,
        project_id,
        suffix="selected-story",
    )
    position = domain.position(project_id)
    decision = _decision(position, "backlog.reconcile")
    affected_ids = tuple(
        sorted(
            int(item.fact_id)
            for item in decision.fact_references
            if item.fact_type in {"vision", "backlog"}
        )
    )

    result = domain.transition(
        ReconcileBacklog(
            **_guards(position, "backlog.reconcile"),
            idempotency_key="selected-story-reconciliation",
            replacement_authority_id=replacement_id,
            replacement_authority_fingerprint=replacement_fingerprint,
            affected_artifact_ids=affected_ids,
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    with Session(engine) as session:
        persisted = session.get(UserStory, story_id)
        assert persisted is not None
        assert persisted.is_superseded is False
        assert session.exec(select(BacklogAuthorityReconciliation)).all() == []


def test_persisted_authority_and_artifact_fingerprint_tampering_fails_closed(
    engine: Engine,
) -> None:
    """Reject authority and immutable artifact fingerprint drift on reload."""
    domain = _domain(engine)

    authority_project, _authority_id, _authority_fingerprint = _seed_accepted_authority(
        engine, name="Tampered Authority"
    )
    with Session(engine) as session:
        acceptance = session.exec(
            select(SpecAuthorityAcceptance).where(
                col(SpecAuthorityAcceptance.product_id) == authority_project
            )
        ).one()
        acceptance.authority_fingerprint = "sha256:tampered-authority"
        session.add(acceptance)
        session.commit()
    with pytest.raises(WorkflowFactLoadError):
        domain.position(authority_project)

    vision_project, vision_authority, vision_authority_fingerprint = (
        _seed_accepted_authority(engine, name="Tampered Vision Artifact")
    )
    vision_recorded = _record_vision(
        domain,
        project_id=vision_project,
        authority_id=vision_authority,
        authority_fingerprint=vision_authority_fingerprint,
        idempotency_key="record-tampered-vision",
    )
    vision_id = vision_recorded.output["vision_artifact_id"]
    assert isinstance(vision_id, int)
    with Session(engine) as session:
        vision = session.get(VisionArtifact, vision_id)
        assert vision is not None
        vision.authority_fingerprint = "sha256:tampered-vision-authority"
        session.add(vision)
        session.commit()
    with pytest.raises(WorkflowFactLoadError):
        domain.position(vision_project)

    backlog_project, backlog_authority, backlog_authority_fingerprint = (
        _seed_accepted_authority(engine, name="Tampered Backlog Artifact")
    )
    backlog_recorded = _record_backlog(
        domain,
        project_id=backlog_project,
        authority_id=backlog_authority,
        authority_fingerprint=backlog_authority_fingerprint,
        idempotency_key="record-tampered-backlog",
    )
    backlog_id = backlog_recorded.output["backlog_artifact_id"]
    assert isinstance(backlog_id, int)
    with Session(engine) as session:
        backlog = session.get(BacklogArtifact, backlog_id)
        assert backlog is not None
        backlog.content_fingerprint = "sha256:tampered-backlog-content"
        session.add(backlog)
        session.commit()
    with pytest.raises(WorkflowFactLoadError):
        domain.position(backlog_project)

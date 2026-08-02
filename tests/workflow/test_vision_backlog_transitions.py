"""Persisted Vision and Backlog graph transition tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypedDict, get_args

from pydantic import TypeAdapter
from sqlmodel import Session, col, select

from models.core import Product, UserStory
from models.enums import StoryStatus, WorkflowEventType
from models.events import WorkflowEvent
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import (
    BacklogArtifact,
    BacklogArtifactDecision,
    BacklogAuthorityReconciliation,
    VisionArtifact,
    VisionArtifactDecision,
)
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
from workflow.fingerprints import canonical_hash
from workflow.requests import (
    DecideBacklog,
    DecideVision,
    ReconcileBacklog,
    RecordBacklogDraft,
    RecordVisionDraft,
    TransitionRequest,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)
EXPECTED_REQUEST_VARIANT_COUNT = 21
EXPECTED_VISION_VERSION_COUNT = 2


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


def test_closed_request_union_adds_exactly_five_product_definition_variants() -> None:
    """Keep the closed request union limited to the five Task 10 variants."""
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
    """Reject a second terminal decision for one Vision artifact."""
    project_id, authority_id, authority_fingerprint = _seed_accepted_authority(engine)
    domain = _domain(engine)
    artifact_id, fingerprint = _accept_vision(
        domain,
        project_id=project_id,
        authority_id=authority_id,
        authority_fingerprint=authority_fingerprint,
    )
    position = domain.position(project_id)

    contradictory = DecideVision(
        **_guards(position, "vision.generate"),
        idempotency_key="contradictory-vision",
        vision_artifact_id=artifact_id,
        artifact_fingerprint=fingerprint,
        decision="rejected",
        rationale="Contradiction must not append.",
    )
    result = domain.transition(contradictory)

    assert result.ok is False
    with Session(engine) as session:
        decisions = session.exec(select(VisionArtifactDecision)).all()
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
        events = session.exec(
            select(WorkflowEvent).where(
                WorkflowEvent.event_type == WorkflowEventType.BACKLOG_SAVED
            )
        ).all()
        assert any(
            json.loads(event.event_metadata or "{}").get("action")
            == "backlog_authority_reconciled"
            for event in events
        )


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

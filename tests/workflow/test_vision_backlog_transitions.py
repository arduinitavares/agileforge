"""Durable Backlog Goal/Specification lineage tests."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlmodel import Session, select

from models.core import Project, UserStory
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from models.product_definition import (
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from models.workflow import BacklogArtifact, BacklogArtifactDecision
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from tests.workflow.test_vision_interview_transitions import (
    _domain as _vision_domain,
)
from tests.workflow.test_vision_interview_transitions import (
    _record as _record_vision,
)
from tests.workflow.test_vision_interview_transitions import (
    _RecordRequest as _VisionRecordRequest,
)
from tests.workflow.test_vision_interview_transitions import (
    _review_vision,
    _VisionReview,
)
from tests.workflow.test_vision_interview_transitions import (
    _start as _start_vision,
)
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.requests import BeginVisionRevision
from workflow.requests.product_definition import RecordBacklogDraft

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from tests.workflow.lifecycle_fixtures import PersistedSpecificationLineage
    from workflow.contracts import JsonObject, JsonValue

EVALUATED_AT = datetime(2026, 8, 5, 12, tzinfo=UTC)


def _backlog_content(
    requirement: str = "Persist exact delivery lineage",
) -> JsonObject:
    return {
        "backlog_items": [
            {
                "backlog_item_id": "PBI-000001",
                "priority": 1,
                "requirement": requirement,
                "spec_item_ids": ["GOAL.delivery", "REQ.delivery"],
                "value_driver": "Strategic",
                "justification": "Keeps delivery decisions restart-safe.",
                "estimated_effort": "M",
                "technical_note": None,
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }


def _specification_content(
    summary: str = "Persist immutable planning artifacts.",
) -> str:
    return canonical_json(
        {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": "SPEC.task-7-delivery",
            "title": "Task 7 delivery contract",
            "summary": summary,
            "problem_statement": "Planning drafts need exact durable lineage.",
            "items": [
                {
                    "id": "GOAL.delivery",
                    "type": "GOAL",
                    "title": "Immutable delivery",
                    "statement": "Planning review uses immutable artifacts.",
                },
                {
                    "id": "REQ.delivery",
                    "type": "REQ",
                    "title": "Exact planning lineage",
                    "statement": "Persist exact accepted Specification lineage.",
                    "level": "MUST",
                    "verification": "acceptance-test",
                    "acceptance": [
                        "The persisted artifact retains exact parent identities."
                    ],
                },
            ],
            "relations": [],
            "controlled_terms": [],
            "external_references": [],
        }
    )


def _vision_content(statement: str = "Build reliable product decisions.") -> JsonObject:
    return {
        "updated_components": {
            "project_name": "Task 10 Project",
            "target_user": "Project operators",
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


def _seed_project_specification(
    session: Session,
) -> PersistedSpecificationLineage:
    project = Project(name="Backlog lineage")
    session.add(project)
    session.commit()
    assert project.project_id is not None
    return seed_accepted_specification(
        session,
        project_id=project.project_id,
        content=_specification_content(),
        recorded_at=EVALUATED_AT - timedelta(minutes=1),
    )


def _record_backlog_draft_in_session(  # noqa: PLR0913
    session: Session,
    *,
    project_id: int,
    spec_version_id: int,
    spec_hash: str,
    product_goal_artifact_id: int,
    product_goal_fingerprint: str,
    canonical_content: JsonObject,
    content_fingerprint: str,
    supersedes_backlog_artifact_id: int | None,
    artifact_id: int,
    actor: str,
    recorded_at: datetime,
) -> BacklogArtifact:
    """Lazy Task 7 persistence seam; Task 5 still exercises request contracts."""
    from services.agent_workbench.backlog_phase import (  # noqa: PLC0415
        record_backlog_draft_in_session,
    )

    return record_backlog_draft_in_session(
        session,
        project_id=project_id,
        spec_version_id=spec_version_id,
        spec_hash=spec_hash,
        product_goal_artifact_id=product_goal_artifact_id,
        product_goal_fingerprint=product_goal_fingerprint,
        canonical_content=canonical_content,
        content_fingerprint=content_fingerprint,
        supersedes_backlog_artifact_id=supersedes_backlog_artifact_id,
        artifact_id=artifact_id,
        actor=actor,
        recorded_at=recorded_at,
    )


def test_vision_bootstrap_accepts_project_without_repository_attachment(
    engine: Engine,
) -> None:
    """Grounded bootstrap persists Project evidence when no repository is bound."""
    with Session(engine) as session:
        project = Project(name="Repository-free Vision")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    domain = _vision_domain(engine)
    start, attempt = _start_vision(domain, project_id, "repository-free-bootstrap")

    result = _record_vision(
        engine,
        domain,
        start,
        attempt,
        request=_VisionRecordRequest(
            complete=False,
            key="repository-free-bootstrap-record",
        ),
    )

    assert result.ok
    with Session(engine) as session:
        snapshot = session.exec(
            select(VisionEvidenceSnapshot).where(
                VisionEvidenceSnapshot.project_id == project_id
            )
        ).one()
    assert snapshot.repository_binding_id is None


def test_vision_revision_reopens_with_grounded_clarification_reason(
    engine: Engine,
) -> None:
    """An incomplete revision accepts ordinary text under the grounded reason."""
    with Session(engine) as session:
        project = Project(name="Vision revision clarification")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    domain = _vision_domain(engine)
    initial_start, initial_attempt = _start_vision(
        domain,
        project_id,
        "revision-clarification-initial",
    )
    initial = _record_vision(
        engine,
        domain,
        initial_start,
        initial_attempt,
        request=_VisionRecordRequest(
            complete=True,
            key="revision-clarification-initial-record",
        ),
    )
    vision_id = initial.output["vision_artifact_id"]
    vision_fingerprint = initial.output["vision_fingerprint"]
    assert isinstance(vision_id, int)
    assert isinstance(vision_fingerprint, str)
    assert _review_vision(
        domain,
        project_id,
        _VisionReview(
            artifact_id=vision_id,
            fingerprint=vision_fingerprint,
            decision="accepted",
            rationale="Accept initial Vision.",
            idempotency_key="revision-clarification-accept",
        ),
    ).ok
    revision_position = domain.position(project_id)
    revision = next(
        item
        for item in revision_position.decisions
        if item.node_id == "vision.revision.start"
    )
    assert domain.transition(
        BeginVisionRevision(
            project_id=project_id,
            graph_version=revision_position.graph_version,
            fact_fingerprint=revision_position.fact_fingerprint,
            decision_fingerprint=revision.decision_fingerprint,
            idempotency_key="revision-clarification-open",
            actor="operator@example.com",
            source_vision_artifact_id=vision_id,
            source_vision_fingerprint=vision_fingerprint,
            reason="Clarify the revised target user.",
        )
    ).ok
    revision_start, revision_attempt = _start_vision(
        domain,
        project_id,
        "revision-clarification-start",
        node_id="vision.bootstrap",
        operation="revision",
    )
    assert _record_vision(
        engine,
        domain,
        revision_start,
        revision_attempt,
        request=_VisionRecordRequest(
            complete=False,
            key="revision-clarification-record",
            operation="revision",
        ),
    ).ok

    clarification = next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "vision.interview"
    )

    assert clarification.reason_code == "VISION_REVISION_CLARIFICATION_REQUIRED"
    assert [item.name for item in clarification.required_inputs] == ["user_text"]
    with Session(engine) as session:
        turns = session.exec(
            select(VisionInterviewTurn).where(
                VisionInterviewTurn.project_id == project_id
            )
        ).all()
        for turn in turns:
            if turn.revision_intent_id is not None:
                turn.revision_intent_id = None
                session.add(turn)
        session.flush()
        for intent in session.exec(
            select(VisionRevisionIntent).where(
                VisionRevisionIntent.project_id == project_id
            )
        ).all():
            session.delete(intent)
        session.commit()


def test_backlog_row_persists_exact_goal_and_specification_lineage(
    engine: Engine,
) -> None:
    """A stored Backlog carries both durable upstream identities."""
    content = _backlog_content()
    with Session(engine) as session:
        lineage = _seed_project_specification(session)
        spec_version_id = lineage.spec.spec_version_id
        spec_hash = lineage.spec.spec_hash
        assert spec_version_id is not None
        row = _record_backlog_draft_in_session(
            session,
            project_id=lineage.spec.project_id,
            spec_version_id=spec_version_id,
            spec_hash=spec_hash,
            product_goal_artifact_id=lineage.product_goal_artifact_id,
            product_goal_fingerprint=lineage.product_goal_fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
            supersedes_backlog_artifact_id=None,
            artifact_id=101,
            actor="operator@example.com",
            recorded_at=EVALUATED_AT,
        )
        session.commit()

        stored = session.get(BacklogArtifact, row.backlog_artifact_id)

    assert stored is not None
    assert stored.spec_version_id == spec_version_id
    assert stored.spec_hash == spec_hash
    assert stored.product_goal_artifact_id == lineage.product_goal_artifact_id
    assert stored.product_goal_fingerprint == lineage.product_goal_fingerprint


def test_backlog_replacement_rejects_wrong_goal_before_persistence(
    engine: Engine,
) -> None:
    """A wrong Goal identity cannot enter an existing Backlog chain."""
    content = _backlog_content()
    with Session(engine) as session:
        lineage = _seed_project_specification(session)
        spec_version_id = lineage.spec.spec_version_id
        assert spec_version_id is not None
        parent = _record_backlog_draft_in_session(
            session,
            project_id=lineage.spec.project_id,
            spec_version_id=spec_version_id,
            spec_hash=lineage.spec.spec_hash,
            product_goal_artifact_id=lineage.product_goal_artifact_id,
            product_goal_fingerprint=lineage.product_goal_fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
            supersedes_backlog_artifact_id=None,
            artifact_id=101,
            actor="operator@example.com",
            recorded_at=EVALUATED_AT,
        )
        assert parent.backlog_artifact_id is not None

        with pytest.raises(ValueError, match="Specification's Product Goal"):
            _record_backlog_draft_in_session(
                session,
                project_id=lineage.spec.project_id,
                spec_version_id=spec_version_id,
                spec_hash=lineage.spec.spec_hash,
                product_goal_artifact_id=lineage.product_goal_artifact_id + 1,
                product_goal_fingerprint="sha256:goal-replacement",
                canonical_content=content,
                content_fingerprint=canonical_hash(content),
                supersedes_backlog_artifact_id=parent.backlog_artifact_id,
                artifact_id=102,
                actor="operator@example.com",
                recorded_at=EVALUATED_AT,
            )

        rows = session.exec(select(BacklogArtifact)).all()
        assert [item.backlog_artifact_id for item in rows] == [101]


@pytest.mark.parametrize(
    ("field", "invalid_value", "message"),
    [
        ("backlog_item_id", "PBI-000002", "host-minted"),
        ("backlog_item_id", "PBI-999999", "host-minted"),
        ("spec_item_ids", ["REQ.unknown"], "unknown Specification item ID"),
        (
            "spec_item_ids",
            ["REQ.delivery", "GOAL.delivery"],
            "unique and sorted",
        ),
    ],
)
def test_backlog_revalidates_host_owned_items_before_persistence(
    engine: Engine,
    field: str,
    invalid_value: JsonValue,
    message: str,
) -> None:
    """Persistence rejects host-looking IDs/evidence that Task 6 did not mint."""
    content = _backlog_content()
    items = content["backlog_items"]
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item[field] = invalid_value
    with Session(engine) as session:
        lineage = _seed_project_specification(session)
        spec_version_id = lineage.spec.spec_version_id
        assert spec_version_id is not None

        with pytest.raises(ValueError, match=message):
            _record_backlog_draft_in_session(
                session,
                project_id=lineage.spec.project_id,
                spec_version_id=spec_version_id,
                spec_hash=lineage.spec.spec_hash,
                product_goal_artifact_id=lineage.product_goal_artifact_id,
                product_goal_fingerprint=lineage.product_goal_fingerprint,
                canonical_content=content,
                content_fingerprint=canonical_hash(content),
                supersedes_backlog_artifact_id=None,
                artifact_id=101,
                actor="operator@example.com",
                recorded_at=EVALUATED_AT,
            )

        assert session.exec(select(BacklogArtifact)).all() == []


@pytest.mark.parametrize(
    "mutation",
    [
        "incomplete",
        "empty",
        "wrong_hash",
        "extra_field",
        "duplicate_ids",
        "is_complete_int",
    ],
)
def test_backlog_rejects_noncanonical_or_incomplete_content_without_rows(
    engine: Engine,
    mutation: str,
) -> None:
    """Malformed host output never reaches artifact or decision persistence."""
    content = copy.deepcopy(_backlog_content())
    fingerprint = canonical_hash(content)
    if mutation == "incomplete":
        content["is_complete"] = False
        fingerprint = canonical_hash(content)
    elif mutation == "empty":
        content["backlog_items"] = []
        fingerprint = canonical_hash(content)
    elif mutation == "wrong_hash":
        fingerprint = "sha256:" + "0" * 64
    elif mutation == "extra_field":
        content["provider_metadata"] = "not canonical host output"
        fingerprint = canonical_hash(content)
    elif mutation == "duplicate_ids":
        items = content["backlog_items"]
        assert isinstance(items, list)
        items.append(copy.deepcopy(items[0]))
        fingerprint = canonical_hash(content)
    else:
        content["is_complete"] = 1
        fingerprint = canonical_hash(content)

    with Session(engine) as session:
        lineage = _seed_project_specification(session)
        spec_version_id = lineage.spec.spec_version_id
        assert spec_version_id is not None
        with pytest.raises((ValidationError, ValueError)):
            _record_backlog_draft_in_session(
                session,
                project_id=lineage.spec.project_id,
                spec_version_id=spec_version_id,
                spec_hash=lineage.spec.spec_hash,
                product_goal_artifact_id=lineage.product_goal_artifact_id,
                product_goal_fingerprint=lineage.product_goal_fingerprint,
                canonical_content=content,
                content_fingerprint=fingerprint,
                supersedes_backlog_artifact_id=None,
                artifact_id=101,
                actor="operator@example.com",
                recorded_at=EVALUATED_AT,
            )
        assert session.exec(select(BacklogArtifact)).all() == []
        assert session.exec(select(BacklogArtifactDecision)).all() == []


def test_backlog_decisions_are_append_only_and_create_zero_stories(
    engine: Engine,
) -> None:
    """Acceptance records one decision and never materializes placeholder Stories."""
    from services.agent_workbench.backlog_phase import (  # noqa: PLC0415
        record_backlog_decision_in_session,
    )

    content = _backlog_content()
    with Session(engine) as session:
        lineage = _seed_project_specification(session)
        spec_version_id = lineage.spec.spec_version_id
        assert spec_version_id is not None
        artifact = _record_backlog_draft_in_session(
            session,
            project_id=lineage.spec.project_id,
            spec_version_id=spec_version_id,
            spec_hash=lineage.spec.spec_hash,
            product_goal_artifact_id=lineage.product_goal_artifact_id,
            product_goal_fingerprint=lineage.product_goal_fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
            supersedes_backlog_artifact_id=None,
            artifact_id=101,
            actor="operator@example.com",
            recorded_at=EVALUATED_AT,
        )

        decision = record_backlog_decision_in_session(
            session,
            artifact=artifact,
            decision="accepted",
            rationale="Exact immutable content is ready.",
            reviewer="operator@example.com",
            idempotency_key="accept-backlog-101",
            decided_at=EVALUATED_AT + timedelta(seconds=1),
        )
        session.commit()

        assert decision.decision == "accepted"
        assert len(session.exec(select(BacklogArtifactDecision)).all()) == 1
        assert session.exec(select(UserStory)).all() == []
        stored = session.get(BacklogArtifact, 101)
        assert stored is not None
        assert stored.canonical_content_json == canonical_json(content)
        assert stored.content_fingerprint == canonical_hash(content)


def test_backlog_decision_rejects_formatting_only_stored_corruption(
    engine: Engine,
) -> None:
    """A decision never accepts or rewrites noncanonical stored Backlog bytes."""
    from services.agent_workbench.backlog_phase import (  # noqa: PLC0415
        record_backlog_decision_in_session,
    )

    content = _backlog_content()
    corrupted = json.dumps(content, indent=2, sort_keys=True)
    assert corrupted != canonical_json(content)
    assert canonical_hash(json.loads(corrupted)) == canonical_hash(content)

    with Session(engine) as session:
        lineage = _seed_project_specification(session)
        spec_version_id = lineage.spec.spec_version_id
        assert spec_version_id is not None
        artifact = _record_backlog_draft_in_session(
            session,
            project_id=lineage.spec.project_id,
            spec_version_id=spec_version_id,
            spec_hash=lineage.spec.spec_hash,
            product_goal_artifact_id=lineage.product_goal_artifact_id,
            product_goal_fingerprint=lineage.product_goal_fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
            supersedes_backlog_artifact_id=None,
            artifact_id=101,
            actor="operator@example.com",
            recorded_at=EVALUATED_AT,
        )
        artifact.canonical_content_json = corrupted
        session.add(artifact)
        session.commit()

        with pytest.raises(ValueError, match="canonical"):
            record_backlog_decision_in_session(
                session,
                artifact=artifact,
                decision="accepted",
                rationale="Formatting corruption must fail closed.",
                reviewer="operator@example.com",
                idempotency_key="reject-noncanonical-backlog",
                decided_at=EVALUATED_AT + timedelta(seconds=1),
            )

        stored = session.get(BacklogArtifact, 101)
        assert stored is not None
        assert stored.canonical_content_json == corrupted
        assert session.exec(select(BacklogArtifactDecision)).all() == []


def test_backlog_a_feedback_b_accepted_c_is_append_only_and_current(
    engine: Engine,
) -> None:
    """A stays immutable through feedback B; accepted C becomes the sole leaf."""
    from services.agent_workbench.backlog_phase import (  # noqa: PLC0415
        _backlog_lineage_nodes,
        record_backlog_decision_in_session,
    )
    from services.planning_lineage import (  # noqa: PLC0415
        select_current_accepted_artifact,
    )

    with Session(engine) as session:
        lineage = _seed_project_specification(session)
        spec_version_id = lineage.spec.spec_version_id
        assert spec_version_id is not None
        key = (
            lineage.spec.project_id,
            lineage.product_goal_artifact_id,
            lineage.product_goal_fingerprint,
            spec_version_id,
            lineage.spec.spec_hash,
        )
        artifacts: list[BacklogArtifact] = []
        contents: list[JsonObject] = []
        for index, (label, decision) in enumerate(
            (
                ("Accepted A", "accepted"),
                ("Feedback B", "feedback"),
                ("Accepted C", "accepted"),
            ),
            start=1,
        ):
            content = _backlog_content(label)
            artifact = _record_backlog_draft_in_session(
                session,
                project_id=lineage.spec.project_id,
                spec_version_id=spec_version_id,
                spec_hash=lineage.spec.spec_hash,
                product_goal_artifact_id=lineage.product_goal_artifact_id,
                product_goal_fingerprint=lineage.product_goal_fingerprint,
                canonical_content=content,
                content_fingerprint=canonical_hash(content),
                supersedes_backlog_artifact_id=(
                    None if not artifacts else artifacts[-1].backlog_artifact_id
                ),
                artifact_id=100 + index,
                actor="operator@example.com",
                recorded_at=EVALUATED_AT + timedelta(seconds=index),
            )
            record_backlog_decision_in_session(
                session,
                artifact=artifact,
                decision=decision,
                rationale=f"Review {label}.",
                reviewer="operator@example.com",
                idempotency_key=f"backlog-{index}-{decision}",
                decided_at=EVALUATED_AT + timedelta(seconds=index, milliseconds=1),
            )
            artifacts.append(artifact)
            contents.append(content)
            if decision == "feedback":
                assert (
                    select_current_accepted_artifact(
                        _backlog_lineage_nodes(
                            session,
                            project_id=lineage.spec.project_id,
                        ),
                        chain_key=key,
                    ).artifact_id
                    == artifacts[0].backlog_artifact_id
                )
        session.commit()

        assert [artifact.version_number for artifact in artifacts] == [1, 2, 3]
        assert (
            select_current_accepted_artifact(
                _backlog_lineage_nodes(
                    session,
                    project_id=lineage.spec.project_id,
                ),
                chain_key=key,
            ).artifact_id
            == artifacts[-1].backlog_artifact_id
        )
        first = session.get(BacklogArtifact, artifacts[0].backlog_artifact_id)
        assert first is not None
        assert first.canonical_content_json == canonical_json(contents[0])
        assert first.content_fingerprint == canonical_hash(contents[0])
        assert session.exec(select(UserStory)).all() == []
        assert (
            session.exec(
                select(WorkflowEvent).where(
                    WorkflowEvent.event_type == WorkflowEventType.BACKLOG_SAVED
                )
            ).all()
            == []
        )


def test_backlog_flush_rolls_back_artifact_and_decision(engine: Engine) -> None:
    """Caller rollback removes every flushed Backlog row atomically."""
    from services.agent_workbench.backlog_phase import (  # noqa: PLC0415
        record_backlog_decision_in_session,
    )

    with Session(engine) as session:
        lineage = _seed_project_specification(session)
        spec_version_id = lineage.spec.spec_version_id
        assert spec_version_id is not None
        content = _backlog_content()
        artifact = _record_backlog_draft_in_session(
            session,
            project_id=lineage.spec.project_id,
            spec_version_id=spec_version_id,
            spec_hash=lineage.spec.spec_hash,
            product_goal_artifact_id=lineage.product_goal_artifact_id,
            product_goal_fingerprint=lineage.product_goal_fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
            supersedes_backlog_artifact_id=None,
            artifact_id=101,
            actor="operator@example.com",
            recorded_at=EVALUATED_AT,
        )
        record_backlog_decision_in_session(
            session,
            artifact=artifact,
            decision="accepted",
            rationale="Flushed then forced to roll back.",
            reviewer="operator@example.com",
            idempotency_key="backlog-rollback",
            decided_at=EVALUATED_AT,
        )
        session.rollback()

    with Session(engine) as session:
        assert session.exec(select(BacklogArtifact)).all() == []
        assert session.exec(select(BacklogArtifactDecision)).all() == []


def test_new_specification_starts_independent_backlog_lineage(
    engine: Engine,
) -> None:
    """Reject cross-Spec parents and preserve the version-1 historical roots."""
    from services.planning_lineage import (  # noqa: PLC0415
        ArtifactLineageNode,
        PlanningLineageError,
        next_artifact_version,
        validate_artifact_lineage,
    )

    with Session(engine) as session:
        first = _seed_project_specification(session)
        first_spec_version_id = first.spec.spec_version_id
        assert first_spec_version_id is not None
        specification = seed_accepted_specification(
            session,
            project_id=first.spec.project_id,
            content=_specification_content("Persist amended immutable artifacts."),
            recorded_at=EVALUATED_AT + timedelta(minutes=1),
        )
        replacement_spec_version_id = specification.spec.spec_version_id
        assert replacement_spec_version_id is not None
        project_id = first.spec.project_id
        old_key = (
            project_id,
            first.product_goal_artifact_id,
            first.product_goal_fingerprint,
            first_spec_version_id,
            first.spec.spec_hash,
        )
        new_key = (
            project_id,
            specification.product_goal_artifact_id,
            specification.product_goal_fingerprint,
            replacement_spec_version_id,
            specification.spec.spec_hash,
        )
    old_root = ArtifactLineageNode(
        artifact_id=101,
        chain_key=old_key,
        version_number=1,
        decision="accepted",
    )
    cross_key_child = ArtifactLineageNode(
        artifact_id=102,
        chain_key=new_key,
        version_number=2,
        supersedes_artifact_id=old_root.artifact_id,
        decision="accepted",
    )

    with pytest.raises(PlanningLineageError, match="CROSS_KEY_PARENT"):
        validate_artifact_lineage((old_root, cross_key_child))

    assert next_artifact_version((), chain_key=new_key, supersedes_id=None) == 1
    new_root = ArtifactLineageNode(
        artifact_id=102,
        chain_key=new_key,
        version_number=1,
        decision="accepted",
    )
    validate_artifact_lineage((old_root, new_root))
    assert old_root.chain_key == old_key
    assert old_root.version_number == 1
    assert new_root.supersedes_artifact_id is None


def test_record_request_requires_exact_goal_identity_and_fingerprint() -> None:
    """Callers cannot create a Goal-less Backlog transition request."""
    with pytest.raises(ValidationError):
        RecordBacklogDraft.model_validate(
            {
                "project_id": 1,
                "graph_version": "agileforge.workflow.v2",
                "fact_fingerprint": "sha256:facts",
                "decision_fingerprint": "sha256:decision",
                "idempotency_key": "request",
                "actor": "operator@example.com",
                "spec_version_id": 2,
                "spec_hash": "sha256:spec",
                "canonical_content": _backlog_content(),
                "content_fingerprint": canonical_hash(_backlog_content()),
            }
        )

"""Durable product-definition read projections."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from pydantic import TypeAdapter
from sqlmodel import Session, select

from models.core import Project, UserStory
from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
    SpecificationCandidate,
    SpecificationDecision,
    SpecificationSource,
    VisionArtifact,
    VisionArtifactDecision,
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from models.repository import RepositoryBinding, repository_binding_fingerprint
from models.specs import SpecRegistry
from models.workflow import BacklogArtifact, RoadmapArtifact, WorkflowNodeAttempt
from repositories.workflow import WorkflowFactRepository
from services.contracts.specification_authoring import (
    SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
    SPECIFICATION_STRUCTURER_PROMPT_VERSION,
    SPECIFICATION_VISION_SOURCE_ID,
    AcceptedProductGoalContext,
    AcceptedVisionContext,
    BaseSpecificationContext,
    RegisteredRepositoryEvidence,
    RegisteredSpecificationSource,
    SpecificationStructuringContextCapture,
    SpecificationStructuringDocument,
    SpecificationStructuringInput,
    specification_structuring_fact_fingerprint,
    specification_structuring_input_fingerprint,
)
from services.contracts.specification_source import (
    SPECIFICATION_SOURCE_CONTEXT_ID,
    SPECIFICATION_SOURCE_PRIMARY_ID,
    SpecificationContextCapture,
    SpecificationRepositoryRevision,
    SpecificationSourceBundle,
    SpecificationSourceDocument,
    source_bundle_fingerprint,
    specification_source_adr_id,
)
from services.read_projections import DurableReadProjectionService
from services.specs.candidate_contract import (
    CandidateBuildInput,
    CandidateKind,
    CandidateSourceKind,
    CandidateSourceManifestEntry,
    build_candidate_envelope,
    canonical_candidate_json,
    render_candidate_review_markdown,
)
from utils.agileforge_spec_profile_v2 import SpecificationPayload
from workflow.contracts import (
    GRAPH_VERSION,
    JsonObject,
    JsonValue,
    NodeCategory,
    WorkflowPosition,
)
from workflow.definitions.product_discovery import select_product_definition_state
from workflow.definitions.product_goal import _goal_interview_rule, _goal_review_rule
from workflow.definitions.root import ROOT_GRAPH
from workflow.facts import (
    BacklogItemFact,
    PhaseArtifactFact,
    PlanningArtifactFact,
    ProjectFact,
    WorkflowFactSnapshot,
)
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    product_goal_artifact_fingerprint,
    product_goal_interview_output_fingerprint,
    vision_interview_output_fingerprint,
    workflow_node_attempt_fingerprint,
)
from workflow.graph import RuleCategory

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.engine import Engine


NOW = datetime(2026, 8, 5, 14, tzinfo=UTC)
GOLD_SPECIFICATION_ITEM_COUNT = 37
_JSON_OBJECT = TypeAdapter(JsonObject)


def _story_pending_snapshot(
    *,
    phase_artifacts: tuple[PhaseArtifactFact, ...],
    backlog_items: tuple[BacklogItemFact, ...],
    planning_artifacts: tuple[PlanningArtifactFact, ...],
) -> WorkflowFactSnapshot:
    """Attach Story projection facts to one otherwise accepted source lineage."""
    from tests.workflow.test_direct_specification_lineage import (  # noqa: PLC0415
        _accepted_snapshot,
    )

    base = _accepted_snapshot()
    return base.model_copy(
        update={
            "project": ProjectFact(
                project_id=71,
                name="Story pending projection",
                created_at=NOW,
            ),
            "phase_artifacts": phase_artifacts,
            "backlog_items": backlog_items,
            "planning_artifacts": planning_artifacts,
        }
    )


def _story_artifact(
    artifact_id: int,
    *,
    backlog_artifact_id: int,
    status: Literal["pending_review", "accepted", "rejected", "feedback"],
    supersedes_artifact_id: int | None = None,
    version_number: int = 1,
) -> PlanningArtifactFact:
    """Build one exact Story lineage node for a current Backlog item."""
    return PlanningArtifactFact(
        artifact_type="story",
        artifact_id=artifact_id,
        artifact_fingerprint=f"sha256:story-{artifact_id}",
        version_number=version_number,
        source_fingerprint=f"sha256:roadmap-{backlog_artifact_id}",
        backlog_artifact_id=backlog_artifact_id,
        backlog_artifact_fingerprint=f"sha256:backlog-{backlog_artifact_id}",
        backlog_item_id="PBI-000001",
        story_item_ids=(f"US-{artifact_id}",),
        supersedes_artifact_id=supersedes_artifact_id,
        status=status,
    )


def _backlog_item(backlog_artifact_id: int, *, spec_item_id: str) -> BacklogItemFact:
    """Build one item whose parent identity is visible in public projection data."""
    return BacklogItemFact(
        backlog_item_id="PBI-000001",
        backlog_artifact_id=backlog_artifact_id,
        backlog_artifact_fingerprint=f"sha256:backlog-{backlog_artifact_id}",
        item_fingerprint=f"sha256:item-{backlog_artifact_id}",
        spec_item_ids=(spec_item_id,),
        priority=backlog_artifact_id - 100,
    )


@pytest.mark.parametrize(
    "status",
    ["pending_review", "feedback", "rejected"],
)
def test_story_pending_exposes_valid_unaccepted_story_leaf(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    status: Literal["pending_review", "feedback", "rejected"],
) -> None:
    """A valid first Story review leaf is pending coverage, not corrupt facts."""
    from tests.workflow.test_direct_specification_lineage import (  # noqa: PLC0415
        _chain_backlog,
    )

    snapshot = _story_pending_snapshot(
        phase_artifacts=(_chain_backlog(101, "accepted", None),),
        backlog_items=(_backlog_item(101, spec_item_id="REQ.001"),),
        planning_artifacts=(
            _story_artifact(301, backlog_artifact_id=101, status=status),
        ),
    )
    projection = DurableReadProjectionService(engine=engine)
    monkeypatch.setattr(projection, "_snapshot", lambda _project_id: snapshot)

    result = projection.story_pending(project_id=71)

    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    items = data["items"]
    assert isinstance(items, list)
    assert items == [
        {
            "backlog_item_id": "PBI-000001",
            "backlog_artifact_id": 101,
            "requirement": "",
            "spec_item_ids": ["REQ.001"],
            "priority": 1,
            "status": status,
            "story_artifact_id": 301,
            "story_item_ids": ["US-301"],
        }
    ]
    assert data["count"] == 1
    assert data["pending_count"] == 1
    assert all("story_ids" not in item for item in items if isinstance(item, dict))


@pytest.mark.parametrize(
    "descendant_status",
    ["feedback", "rejected"],
)
def test_story_pending_keeps_accepted_story_current_across_review_descendants(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    descendant_status: Literal["feedback", "rejected"],
) -> None:
    """Feedback or rejection cannot displace the existing accepted coverage."""
    from tests.workflow.test_direct_specification_lineage import (  # noqa: PLC0415
        _chain_backlog,
    )

    accepted_artifact_id = 301
    snapshot = _story_pending_snapshot(
        phase_artifacts=(_chain_backlog(101, "accepted", None),),
        backlog_items=(_backlog_item(101, spec_item_id="REQ.001"),),
        planning_artifacts=(
            _story_artifact(
                accepted_artifact_id,
                backlog_artifact_id=101,
                status="accepted",
            ),
            _story_artifact(
                302,
                backlog_artifact_id=101,
                status=descendant_status,
                supersedes_artifact_id=301,
                version_number=2,
            ),
        ),
    )
    projection = DurableReadProjectionService(engine=engine)
    monkeypatch.setattr(projection, "_snapshot", lambda _project_id: snapshot)

    result = projection.story_pending(project_id=71)

    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    items = data["items"]
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    assert item["status"] == "accepted"
    assert item["story_artifact_id"] == accepted_artifact_id
    assert data["pending_count"] == 0


def test_story_pending_selects_only_the_current_accepted_backlog_root(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical Backlog reuse never leaks an equal item ID into the public list."""
    from tests.workflow.test_direct_specification_lineage import (  # noqa: PLC0415
        _chain_backlog,
    )

    snapshot = _story_pending_snapshot(
        phase_artifacts=(
            _chain_backlog(101, "superseded", None),
            _chain_backlog(102, "accepted", 101),
        ),
        backlog_items=(
            _backlog_item(101, spec_item_id="REQ.HISTORICAL"),
            _backlog_item(102, spec_item_id="REQ.CURRENT"),
        ),
        planning_artifacts=(
            _story_artifact(301, backlog_artifact_id=101, status="accepted"),
            _story_artifact(401, backlog_artifact_id=102, status="accepted"),
        ),
    )
    projection = DurableReadProjectionService(engine=engine)
    monkeypatch.setattr(projection, "_snapshot", lambda _project_id: snapshot)

    result = projection.story_pending(project_id=71)

    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    assert data["items"] == [
        {
            "backlog_item_id": "PBI-000001",
            "backlog_artifact_id": 102,
            "requirement": "",
            "spec_item_ids": ["REQ.CURRENT"],
            "priority": 2,
            "status": "accepted",
            "story_artifact_id": 401,
            "story_item_ids": ["US-401"],
        }
    ]
    assert data["count"] == 1
    assert data["pending_count"] == 0


def test_story_pending_projects_requirement_summary_from_accepted_backlog_artifact(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The story_pending projection includes requirement text from Backlog."""
    from tests.workflow.test_direct_specification_lineage import (  # noqa: PLC0415
        _chain_backlog,
    )

    project_id, backlog_id, backlog_fingerprint, _spec_version_id = (
        _seed_task_7_backlog(engine)
    )

    snapshot = _story_pending_snapshot(
        phase_artifacts=(
            _chain_backlog(backlog_id, "accepted", None).model_copy(
                update={"artifact_fingerprint": backlog_fingerprint}
            ),
        ),
        backlog_items=(
            BacklogItemFact(
                backlog_item_id="PBI-000001",
                backlog_artifact_id=backlog_id,
                backlog_artifact_fingerprint=backlog_fingerprint,
                item_fingerprint="sha256:item-1",
                spec_item_ids=("REQ.001",),
                priority=1,
            ),
        ),
        planning_artifacts=(),
    )
    projection = DurableReadProjectionService(engine=engine)
    monkeypatch.setattr(projection, "_snapshot", lambda _project_id: snapshot)

    result = projection.story_pending(project_id=project_id)

    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    items = data["items"]
    assert isinstance(items, list)
    assert len(items) == 1
    first_item = items[0]
    assert isinstance(first_item, dict)
    assert first_item["backlog_item_id"] == "PBI-000001"
    assert first_item["requirement"] == "Persist exact delivery lineage"


def test_story_pending_fails_closed_on_invalid_backlog_canonical_content(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed with structured projection error when Backlog content is invalid."""
    from tests.workflow.test_direct_specification_lineage import (  # noqa: PLC0415
        _chain_backlog,
    )

    project_id, backlog_id, backlog_fingerprint, _spec_version_id = (
        _seed_task_7_backlog(engine)
    )

    with Session(engine) as session:
        artifact = session.get(BacklogArtifact, backlog_id)
        assert artifact is not None
        artifact.canonical_content_json = "{invalid json"
        session.add(artifact)
        session.commit()

    snapshot = _story_pending_snapshot(
        phase_artifacts=(
            _chain_backlog(backlog_id, "accepted", None).model_copy(
                update={"artifact_fingerprint": backlog_fingerprint}
            ),
        ),
        backlog_items=(
            BacklogItemFact(
                backlog_item_id="PBI-000001",
                backlog_artifact_id=backlog_id,
                backlog_artifact_fingerprint=backlog_fingerprint,
                item_fingerprint="sha256:item-1",
                spec_item_ids=("REQ.001",),
                priority=1,
            ),
        ),
        planning_artifacts=(),
    )
    projection = DurableReadProjectionService(engine=engine)
    monkeypatch.setattr(projection, "_snapshot", lambda _project_id: snapshot)

    result = projection.story_pending(project_id=project_id)

    assert result["ok"] is False
    errors = result["errors"]
    assert isinstance(errors, list)
    error = errors[0]
    assert isinstance(error, dict)
    assert error["code"] == "PROJECT_FACTS_UNAVAILABLE"
    details = error["details"]
    assert isinstance(details, dict)
    assert details["reason"] == "BACKLOG_CONTENT_INVALID"
    assert error["message"] == "Stored Backlog artifact canonical content is invalid."


def test_story_pending_fails_closed_on_conflicting_current_backlog_lineage(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not mix Story coverage when the accepted Backlog root is ambiguous."""
    from tests.workflow.test_direct_specification_lineage import (  # noqa: PLC0415
        _chain_backlog,
    )

    snapshot = _story_pending_snapshot(
        phase_artifacts=(
            _chain_backlog(101, "accepted", None),
            _chain_backlog(102, "accepted", None).model_copy(
                update={"version_number": 1}
            ),
        ),
        backlog_items=(_backlog_item(101, spec_item_id="REQ.001"),),
        planning_artifacts=(
            _story_artifact(301, backlog_artifact_id=101, status="accepted"),
        ),
    )
    projection = DurableReadProjectionService(engine=engine)
    monkeypatch.setattr(projection, "_snapshot", lambda _project_id: snapshot)

    result = projection.story_pending(project_id=71)

    assert result["ok"] is False
    errors = result["errors"]
    assert isinstance(errors, list)
    error = errors[0]
    assert isinstance(error, dict)
    assert error["code"] == "PROJECT_FACTS_UNAVAILABLE"


def _accepted_story_for_show(engine: Engine) -> int:
    """Persist one accepted Story row for public criteria-read regressions."""
    from tests.workflow.test_planning_transitions import (  # noqa: PLC0415
        _domain,
        _record_and_accept_roadmap,
        _record_and_accept_story,
        _seed_accepted_backlog,
    )

    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    return _record_and_accept_story(engine, domain, project_id)[1]


def test_story_show_returns_exact_parsed_canonical_acceptance_criteria(
    engine: Engine,
) -> None:
    """Expose a persisted canonical Unicode/newline criteria list unchanged."""
    story_id = _accepted_story_for_show(engine)
    expected_criteria = ["First line\nsecond line", "- Unicode ✓", "Third criterion"]
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.acceptance_criteria_json = canonical_json(expected_criteria)
        session.add(story)
        session.commit()

    result = DurableReadProjectionService(engine=engine).story_show(story_id=story_id)

    assert result["ok"] is True
    data = _data(result)
    criteria = data["acceptance_criteria"]
    assert isinstance(criteria, list)
    assert criteria == expected_criteria


@pytest.mark.parametrize(
    "stored_criteria",
    [
        '["unterminated"',
        "[]",
        '["   "]',
        '["criterion", 1]',
        '[ "criterion" ]',
        '["✓"]',
    ],
)
def test_story_show_rejects_invalid_persisted_acceptance_criteria(
    engine: Engine,
    stored_criteria: str,
) -> None:
    """Never normalize corrupt immutable criteria into a successful public read."""
    story_id = _accepted_story_for_show(engine)
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.acceptance_criteria_json = stored_criteria
        session.add(story)
        session.commit()

    result = DurableReadProjectionService(engine=engine).story_show(story_id=story_id)

    assert result["ok"] is False
    assert result["data"] == {"story_id": story_id}
    errors = result["errors"]
    assert isinstance(errors, list)
    error = errors[0]
    assert isinstance(error, dict)
    assert error == {
        "code": "ACCEPTANCE_CRITERIA_INVALID",
        "message": "Stored Story acceptance criteria are invalid.",
        "details": {"story_id": story_id},
    }


def _vision_output_fingerprint(
    components: Mapping[str, object],
    statement: str,
    is_complete: bool,
    questions: Sequence[Mapping[str, object]],
) -> str:
    """Build a valid complete Vision turn fingerprint for direct fixtures."""
    return vision_interview_output_fingerprint(
        components,
        statement,
        is_complete,
        questions,
        {"component_basis": (), "assumptions": (), "conflicts": ()},
    )


def test_public_product_definition_selection_retains_projection_state(
    engine: Engine,
) -> None:
    """Read projections share one stable selection interface with graph rules."""
    seeded = _seed_lineage(engine)
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)

    selection = select_product_definition_state(snapshot)

    assert selection.specification_candidate is not None
    assert selection.accepted_spec is None
    assert not selection.has_conflict


def _add_vision_evidence_snapshot(
    session: Session,
    project_id: int,
    attempt_id: int,
    *,
    key: str,
) -> int:
    evidence_payload = {
        "schema_version": "agileforge.vision-evidence.v1",
        "items": [
            {
                "evidence_id": "project:metadata",
                "kind": "project_metadata",
                "relative_path": None,
                "content_fingerprint": canonical_hash(
                    {"project_id": project_id, "key": key}
                ),
                "trust": "operator_provided",
                "content": {"project_id": project_id, "key": key},
                "truncated": False,
            }
        ],
        "warnings": [],
    }
    snapshot = VisionEvidenceSnapshot(
        project_id=project_id,
        repository_binding_id=None,
        workflow_node_attempt_id=attempt_id,
        evidence_json=canonical_json(
            {
                **evidence_payload,
                "evidence_fingerprint": canonical_hash(evidence_payload),
            }
        ),
        evidence_fingerprint=canonical_hash(evidence_payload),
        warnings_json="[]",
        created_at=NOW,
    )
    session.add(snapshot)
    session.flush()
    assert snapshot.vision_evidence_snapshot_id is not None
    return snapshot.vision_evidence_snapshot_id


def _registered_document(
    *,
    source_id: str,
    relative_path: str,
    content: bytes,
) -> SpecificationSourceDocument:
    """Build one byte-exact registered document for projection tests."""
    return SpecificationSourceDocument(
        source_id=source_id,
        relative_path=relative_path,
        content_base64=base64.b64encode(content).decode("ascii"),
        byte_length=len(content),
        content_fingerprint="sha256:" + hashlib.sha256(content).hexdigest(),
    )


def _structuring_document(
    document: SpecificationSourceDocument,
) -> SpecificationStructuringDocument:
    """Expose exact registered UTF-8 bytes to the structuring contract."""
    return SpecificationStructuringDocument(
        source_id=document.source_id,
        relative_path=document.relative_path,
        text=base64.b64decode(document.content_base64, validate=True).decode("utf-8"),
        byte_length=document.byte_length,
        content_fingerprint=document.content_fingerprint,
    )


def _seed_lineage(  # noqa: PLR0915
    engine: Engine,
    *,
    goal_number: int = 1,
    specification_payload_override: SpecificationPayload | None = None,
) -> dict[str, object]:
    """Seed one valid durable Vision, Goal, and typed candidate chain."""
    with Session(engine) as session:
        project = Project(
            name="Projection contract",
            vision="mutable cache must not be read",
            spec_file_path="/must/not/be/read.json",
        )
        session.add(project)
        session.flush()
        assert project.project_id is not None
        attempt = WorkflowNodeAttempt(
            project_id=project.project_id,
            node_id="vision.bootstrap",
            instance_key=None,
            graph_version=GRAPH_VERSION,
            fact_fingerprint=canonical_hash({"facts": goal_number}),
            business_fact_fingerprint=canonical_hash({"business": goal_number}),
            decision_fingerprint=canonical_hash({"decision": goal_number}),
            normalized_input_json="{}",
            input_fingerprint=canonical_hash({"input": goal_number}),
            model_id="fake/product-definition",
            execution_settings_json="{}",
            idempotency_key=f"attempt-{goal_number}",
            actor="operator",
            correlation_id=None,
            started_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
            attempt_fingerprint=canonical_hash({"attempt": goal_number}),
        )
        session.add(attempt)
        session.flush()
        assert attempt.workflow_node_attempt_id is not None
        snapshot_id = _add_vision_evidence_snapshot(
            session,
            project.project_id,
            attempt.workflow_node_attempt_id,
            key=f"lineage-{goal_number}",
        )

        vision_components = {"purpose": "durable reads"}
        vision_turn = VisionInterviewTurn(
            project_id=project.project_id,
            operation="bootstrap",
            turn_number=1,
            revision_intent_id=None,
            vision_evidence_snapshot_id=snapshot_id,
            prior_turn_id=None,
            user_text=None,
            components_json=canonical_json(vision_components),
            vision_statement="A durable Vision.",
            is_complete=True,
            clarifying_questions_json="[]",
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            output_fingerprint=_vision_output_fingerprint(
                vision_components,
                "A durable Vision.",
                True,
                [],
            ),
            workflow_node_attempt_id=attempt.workflow_node_attempt_id,
            attempt_fingerprint=attempt.attempt_fingerprint,
            recorded_at=NOW,
        )
        session.add(vision_turn)
        session.flush()
        assert vision_turn.vision_interview_turn_id is not None
        vision = VisionArtifact(
            project_id=project.project_id,
            version_number=1,
            components_json=canonical_json(vision_components),
            statement="A durable Vision.",
            content_fingerprint=canonical_hash(
                {"components": vision_components, "statement": "A durable Vision."}
            ),
            vision_evidence_snapshot_id=snapshot_id,
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            supersedes_vision_artifact_id=None,
            source_interview_turn_id=vision_turn.vision_interview_turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=1),
        )
        session.add(vision)
        session.flush()
        assert vision.vision_artifact_id is not None
        session.add(
            VisionArtifactDecision(
                project_id=project.project_id,
                vision_artifact_id=vision.vision_artifact_id,
                artifact_fingerprint=vision.content_fingerprint,
                decision="accepted",
                rationale="Reviewed.",
                reviewer="operator",
                idempotency_key=f"vision-{goal_number}",
                decided_at=NOW + timedelta(seconds=2),
            )
        )

        goal_components = {
            "valuable_future_state": "Reliable decisions",
            "beneficiary": "Operators",
            "value": "Confidence",
            "success_signals": ["Measured outcomes"],
            "boundaries": ["No implementation"],
        }
        goal_statement = f"Goal {goal_number}: reliable decisions."
        goal_turn = ProductGoalInterviewTurn(
            project_id=project.project_id,
            vision_artifact_id=vision.vision_artifact_id,
            vision_fingerprint=vision.content_fingerprint,
            goal_number=goal_number,
            revision_number=1,
            prior_turn_id=None,
            user_text="Define goal",
            components_json=canonical_json(goal_components),
            goal_statement=goal_statement,
            is_complete=True,
            clarifying_questions_json="[]",
            output_fingerprint=product_goal_interview_output_fingerprint(
                goal_components, goal_statement, True, ()
            ),
            workflow_node_attempt_id=attempt.workflow_node_attempt_id,
            attempt_fingerprint=attempt.attempt_fingerprint,
            recorded_at=NOW + timedelta(seconds=3),
        )
        session.add(goal_turn)
        session.flush()
        assert goal_turn.product_goal_interview_turn_id is not None
        goal = ProductGoalArtifact(
            project_id=project.project_id,
            vision_artifact_id=vision.vision_artifact_id,
            vision_fingerprint=vision.content_fingerprint,
            goal_number=goal_number,
            revision_number=1,
            statement=goal_statement,
            content_fingerprint=product_goal_artifact_fingerprint(
                goal_components, goal_statement
            ),
            supersedes_product_goal_artifact_id=None,
            source_interview_turn_id=goal_turn.product_goal_interview_turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=4),
        )
        session.add(goal)
        session.flush()
        assert goal.product_goal_artifact_id is not None
        session.add(
            ProductGoalArtifactDecision(
                project_id=project.project_id,
                product_goal_artifact_id=goal.product_goal_artifact_id,
                artifact_fingerprint=goal.content_fingerprint,
                decision="accepted",
                rationale="Reviewed.",
                reviewer="operator",
                idempotency_key=f"goal-{goal_number}",
                decided_at=NOW + timedelta(seconds=5),
            )
        )

        repository_status_fingerprint = canonical_hash(
            {"projection_repository": goal_number}
        )
        repository_binding = RepositoryBinding(
            project_id=project.project_id,
            worktree_path="/projection/repository",
            common_git_dir="/projection/repository/.git",
            head_sha="a" * 40,
            branch_name="dev/projection",
            detached_head=False,
            dirty=False,
            status_fingerprint=repository_status_fingerprint,
            status_entries_json="[]",
            remotes_json="[]",
            warnings_json="[]",
            probe_version="agileforge.repository-probe.v1",
            inspected_at=NOW + timedelta(seconds=5),
            supersedes_repository_binding_id=None,
            recorded_by="operator",
        )
        session.add(repository_binding)
        session.flush()
        assert repository_binding.repository_binding_id is not None
        project.active_repository_binding_id = repository_binding.repository_binding_id
        session.add(project)
        source_document = _registered_document(
            source_id=SPECIFICATION_SOURCE_PRIMARY_ID,
            relative_path="SPECIFICATION.md",
            content=b"REGISTERED_TO_SPEC_SOURCE_MUST_NOT_LEAK\n",
        )
        context_document = _registered_document(
            source_id=SPECIFICATION_SOURCE_CONTEXT_ID,
            relative_path="CONTEXT.md",
            content=b"REGISTERED_CONTEXT_MUST_NOT_LEAK\n",
        )
        adr_document = _registered_document(
            source_id=specification_source_adr_id("docs/adr/0001-projection.md"),
            relative_path="docs/adr/0001-projection.md",
            content=b"REGISTERED_ADR_MUST_NOT_LEAK\n",
        )
        source_bundle = SpecificationSourceBundle(
            source=source_document,
            context=SpecificationContextCapture(
                state="present",
                document=context_document,
            ),
            adrs=(adr_document,),
            repository_revision=SpecificationRepositoryRevision(
                head_sha=repository_binding.head_sha,
                branch_name=repository_binding.branch_name,
                detached_head=repository_binding.detached_head,
                dirty=repository_binding.dirty,
                status_fingerprint=repository_binding.status_fingerprint,
            ),
            accepted_vision_fingerprint=vision.content_fingerprint,
            accepted_product_goal_fingerprint=goal.content_fingerprint,
        )
        source = SpecificationSource(
            project_id=project.project_id,
            source_bundle_json=canonical_json(source_bundle.model_dump(mode="json")),
            source_fingerprint=source_bundle_fingerprint(source_bundle),
            repository_binding_id=repository_binding.repository_binding_id,
            repository_head_sha=repository_binding.head_sha,
            repository_dirty=repository_binding.dirty,
            repository_status_fingerprint=repository_binding.status_fingerprint,
            vision_artifact_id=vision.vision_artifact_id,
            vision_fingerprint=vision.content_fingerprint,
            product_goal_artifact_id=goal.product_goal_artifact_id,
            product_goal_fingerprint=goal.content_fingerprint,
            supersedes_specification_source_id=None,
            supersedes_source_fingerprint=None,
            registered_by="operator",
            registered_at=NOW + timedelta(seconds=6),
        )
        session.add(source)
        session.flush()
        assert source.specification_source_id is not None

        specification_attempt = WorkflowNodeAttempt(
            project_id=project.project_id,
            node_id="specification.structure",
            instance_key=None,
            graph_version=GRAPH_VERSION,
            fact_fingerprint=canonical_hash({"specification": "facts"}),
            business_fact_fingerprint=canonical_hash({"specification": "business"}),
            decision_fingerprint=canonical_hash({"specification": "decision"}),
            normalized_input_json="{}",
            input_fingerprint=canonical_hash({"specification": "input"}),
            model_id="fake/product-definition",
            execution_settings_json="{}",
            idempotency_key=f"specification-attempt-{goal_number}",
            actor="operator",
            correlation_id=f"specification-{goal_number}",
            started_at=NOW + timedelta(seconds=6),
            lease_expires_at=NOW + timedelta(minutes=1),
            attempt_fingerprint=canonical_hash(
                {"specification": "attempt", "goal_number": goal_number}
            ),
        )
        session.add(specification_attempt)
        session.flush()
        assert specification_attempt.workflow_node_attempt_id is not None
        specification_payload = SpecificationPayload.model_validate(
            {
                "schema_version": "agileforge.spec.v2",
                "artifact_id": f"SPEC.projection-{goal_number}",
                "title": "Durable specification",
                "summary": "Project durable typed specification review data.",
                "problem_statement": "Operators need exact durable review packets.",
                "items": [
                    {
                        "id": f"GOAL.projection-{goal_number}",
                        "type": "GOAL",
                        "title": "Durable review",
                        "statement": "Expose the exact persisted candidate.",
                        "acceptance": ["The review packet is deterministic."],
                        "source_notes": [
                            {
                                "source_id": SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
                                "kind": "interview",
                                "text": "Accepted Product Goal source.",
                                "external_ref_id": "EXT.issue-199",
                            }
                        ],
                    },
                    {
                        "id": f"REQ.projection-{goal_number}",
                        "type": "REQ",
                        "title": "Exact candidate",
                        "statement": "The packet MUST expose exact v2 bytes.",
                        "level": "MUST",
                        "verification": "system-test",
                        "acceptance": ["Payload and envelope fingerprints match."],
                    },
                ],
                "relations": [
                    {
                        "from": f"REQ.projection-{goal_number}",
                        "type": "satisfies",
                        "to": f"GOAL.projection-{goal_number}",
                    }
                ],
                "controlled_terms": [],
                "external_references": [
                    {
                        "id": "EXT.issue-199",
                        "title": "Issue 199",
                        "url": "https://github.com/arduinitavares/agileforge/issues/199",
                        "summary": "Approved hard-break contract.",
                    }
                ],
            }
        )
        if specification_payload_override is not None:
            specification_payload = specification_payload_override
        source_manifest = (
            CandidateSourceManifestEntry(
                source_id=SPECIFICATION_VISION_SOURCE_ID,
                kind=CandidateSourceKind.VISION,
                fingerprint=vision.content_fingerprint,
            ),
            CandidateSourceManifestEntry(
                source_id=SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
                kind=CandidateSourceKind.PRODUCT_GOAL,
                fingerprint=goal.content_fingerprint,
            ),
            CandidateSourceManifestEntry(
                source_id=source_document.source_id,
                kind=CandidateSourceKind.EXTERNAL,
                fingerprint=source_document.content_fingerprint,
            ),
            CandidateSourceManifestEntry(
                source_id=context_document.source_id,
                kind=CandidateSourceKind.REPOSITORY,
                fingerprint=context_document.content_fingerprint,
            ),
            CandidateSourceManifestEntry(
                source_id=adr_document.source_id,
                kind=CandidateSourceKind.REPOSITORY,
                fingerprint=adr_document.content_fingerprint,
            ),
        )
        structuring_input = SpecificationStructuringInput(
            project_id=project.project_id,
            project_name=project.name,
            operation="initial",
            accepted_vision=AcceptedVisionContext(
                artifact_id=vision.vision_artifact_id,
                fingerprint=vision.content_fingerprint,
                statement=vision.statement,
                components=_JSON_OBJECT.validate_python(vision_components),
            ),
            accepted_product_goal=AcceptedProductGoalContext(
                artifact_id=goal.product_goal_artifact_id,
                fingerprint=goal.content_fingerprint,
                statement=goal.statement,
            ),
            registered_source=RegisteredSpecificationSource(
                specification_source_id=source.specification_source_id,
                source_fingerprint=source.source_fingerprint,
                producer_capability=source_bundle.producer_capability,
                preparation_capability=source_bundle.preparation_capability,
                source=_structuring_document(source_document),
                context=SpecificationStructuringContextCapture(
                    state="present",
                    document=_structuring_document(context_document),
                ),
                adrs=(_structuring_document(adr_document),),
                repository_revision=source_bundle.repository_revision,
                repository_evidence=RegisteredRepositoryEvidence(
                    repository_binding_id=repository_binding.repository_binding_id,
                    binding_fingerprint=repository_binding_fingerprint(
                        repository_binding
                    ),
                    head_sha=repository_binding.head_sha,
                    branch_name=repository_binding.branch_name,
                    detached_head=repository_binding.detached_head,
                    dirty=repository_binding.dirty,
                    status_fingerprint=repository_binding.status_fingerprint,
                    status_entries=(),
                    remotes=(),
                    warnings=(),
                    probe_version=repository_binding.probe_version,
                ),
                accepted_vision_fingerprint=vision.content_fingerprint,
                accepted_product_goal_fingerprint=goal.content_fingerprint,
            ),
            source_manifest=source_manifest,
        )
        normalized_input = structuring_input.model_dump(mode="json")
        specification_attempt.normalized_input_json = canonical_json(normalized_input)
        specification_attempt.input_fingerprint = canonical_hash(normalized_input)
        specification_attempt.attempt_fingerprint = workflow_node_attempt_fingerprint(
            {
                "attempt_id": specification_attempt.workflow_node_attempt_id,
                "project_id": specification_attempt.project_id,
                "node_id": specification_attempt.node_id,
                "instance_key": specification_attempt.instance_key,
                "graph_version": specification_attempt.graph_version,
                "fact_fingerprint": specification_attempt.fact_fingerprint,
                "business_fact_fingerprint": (
                    specification_attempt.business_fact_fingerprint
                ),
                "decision_fingerprint": specification_attempt.decision_fingerprint,
                "normalized_input": normalized_input,
                "input_fingerprint": specification_attempt.input_fingerprint,
                "model_id": specification_attempt.model_id,
                "execution_settings": {},
                "idempotency_key": specification_attempt.idempotency_key,
                "actor": specification_attempt.actor,
                "correlation_id": specification_attempt.correlation_id,
                "started_at": specification_attempt.started_at,
                "lease_expires_at": specification_attempt.lease_expires_at,
            }
        )
        session.add(specification_attempt)
        session.flush()
        envelope = build_candidate_envelope(
            payload=specification_payload,
            metadata=CandidateBuildInput(
                candidate_kind=CandidateKind.INITIAL,
                accepted_vision_id=vision.vision_artifact_id,
                accepted_vision_fingerprint=vision.content_fingerprint,
                accepted_product_goal_id=goal.product_goal_artifact_id,
                accepted_product_goal_fingerprint=goal.content_fingerprint,
                registered_source_fingerprint=source.source_fingerprint,
                source_producer_capability=source_bundle.producer_capability,
                source_preparation_capability=(source_bundle.preparation_capability),
                source_manifest=source_manifest,
                accepted_fact_fingerprint=specification_structuring_fact_fingerprint(
                    structuring_input
                ),
                producer_input_fingerprint=(
                    specification_structuring_input_fingerprint(structuring_input)
                ),
                producer_capability="specification-structurer",
                producer_version="1.0.0",
                model_id=specification_attempt.model_id,
                model_configuration_fingerprint=canonical_hash(
                    {"model": specification_attempt.model_id}
                ),
                prompt_version=SPECIFICATION_STRUCTURER_PROMPT_VERSION,
                prompt_fingerprint=canonical_hash({"prompt": "specification-v2"}),
                workflow_node_attempt_id=(
                    specification_attempt.workflow_node_attempt_id
                ),
                attempt_fingerprint=specification_attempt.attempt_fingerprint,
                correlation_id=f"specification-{goal_number}",
                produced_at=NOW + timedelta(seconds=7),
            ),
        )
        candidate = SpecificationCandidate(
            project_id=project.project_id,
            candidate_kind="initial",
            specification_source_id=source.specification_source_id,
            specification_source_fingerprint=source.source_fingerprint,
            vision_artifact_id=vision.vision_artifact_id,
            vision_fingerprint=vision.content_fingerprint,
            product_goal_artifact_id=goal.product_goal_artifact_id,
            product_goal_fingerprint=goal.content_fingerprint,
            base_spec_version_id=None,
            base_spec_hash=None,
            canonical_envelope_json=canonical_candidate_json(
                specification_payload, envelope
            ),
            payload_fingerprint=envelope.payload_fingerprint,
            source_manifest_fingerprint=envelope.source_manifest_fingerprint,
            producer_input_fingerprint=envelope.producer_input_fingerprint,
            rendered_view_fingerprint=envelope.review_view_fingerprint,
            candidate_fingerprint=envelope.candidate_fingerprint,
            workflow_node_attempt_id=specification_attempt.workflow_node_attempt_id,
            attempt_fingerprint=specification_attempt.attempt_fingerprint,
            supersedes_specification_candidate_id=None,
            supersedes_candidate_fingerprint=None,
            recorded_by="operator",
            recorded_at=NOW + timedelta(seconds=7),
        )
        session.add(candidate)
        session.flush()
        assert candidate.specification_candidate_id is not None
        result = {
            "project_id": project.project_id,
            "vision_id": vision.vision_artifact_id,
            "vision_fingerprint": vision.content_fingerprint,
            "goal_id": goal.product_goal_artifact_id,
            "goal_fingerprint": goal.content_fingerprint,
            "goal_statement": goal.statement,
            "source_id": source.specification_source_id,
            "source_fingerprint": source.source_fingerprint,
            "source_document_fingerprint": source_document.content_fingerprint,
            "context_document_fingerprint": context_document.content_fingerprint,
            "adr_document_fingerprint": adr_document.content_fingerprint,
            "repository_binding_id": repository_binding.repository_binding_id,
            "repository_status_fingerprint": (repository_binding.status_fingerprint),
            "candidate_id": candidate.specification_candidate_id,
            "candidate_fingerprint": candidate.candidate_fingerprint,
            "payload_fingerprint": candidate.payload_fingerprint,
            "source_manifest_fingerprint": candidate.source_manifest_fingerprint,
            "producer_input_fingerprint": candidate.producer_input_fingerprint,
            "rendered_view_fingerprint": candidate.rendered_view_fingerprint,
            "specification_payload": specification_payload.model_dump(mode="json"),
            "rendered_markdown": render_candidate_review_markdown(
                specification_payload, envelope
            ),
            "source_manifest": [
                item.model_dump(mode="json") for item in envelope.source_manifest
            ],
            "accepted_fact_fingerprint": envelope.accepted_fact_fingerprint,
            "attempt_id": attempt.workflow_node_attempt_id,
            "attempt_fingerprint": attempt.attempt_fingerprint,
            "specification_attempt_id": (
                specification_attempt.workflow_node_attempt_id
            ),
            "specification_attempt_fingerprint": (
                specification_attempt.attempt_fingerprint
            ),
        }
        session.commit()
        return result


def _json_object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return _JSON_OBJECT.validate_python(value)


def _data(result: JsonObject) -> JsonObject:
    assert result["ok"] is True
    return _json_object(result["data"])


def _error_code(result: JsonObject) -> str:
    assert result["ok"] is False
    errors = result["errors"]
    assert isinstance(errors, list)
    assert errors
    code = _json_object(errors[0])["code"]
    assert isinstance(code, str)
    return code


def _stored_iso(value: datetime) -> str:
    """Match SQLite's durable naive-datetime representation."""
    return value.replace(tzinfo=None).isoformat()


def _assert_complete_candidate_projection(
    candidate: JsonObject,
    seeded: dict[str, object],
    *,
    decision_state: str,
) -> None:
    """Assert one packet exposes the full immutable v2 review contract."""
    assert set(candidate) == {
        "specification_candidate_id",
        "envelope_version",
        "candidate_kind",
        "canonical_payload",
        "rendered_markdown",
        "vision_artifact_id",
        "vision_fingerprint",
        "product_goal_artifact_id",
        "product_goal_fingerprint",
        "base_spec_version_id",
        "base_spec_hash",
        "specification_source_id",
        "registered_source_fingerprint",
        "source_producer_capability",
        "source_preparation_capability",
        "source_manifest",
        "source_manifest_fingerprint",
        "accepted_fact_fingerprint",
        "producer_input_fingerprint",
        "producer_capability",
        "producer_version",
        "model_id",
        "model_configuration_fingerprint",
        "prompt_fingerprint",
        "prompt_version",
        "workflow_node_attempt_id",
        "attempt_fingerprint",
        "correlation_id",
        "produced_at",
        "payload_fingerprint",
        "profile_version",
        "renderer_version",
        "rendered_view_fingerprint",
        "amendment_diff",
        "candidate_fingerprint",
        "supersedes_specification_candidate_id",
        "supersedes_candidate_fingerprint",
        "decision_state",
    }
    assert candidate["canonical_payload"] == seeded["specification_payload"]
    assert candidate["rendered_markdown"] == seeded["rendered_markdown"]
    assert candidate["vision_artifact_id"] == seeded["vision_id"]
    assert candidate["vision_fingerprint"] == seeded["vision_fingerprint"]
    assert candidate["product_goal_artifact_id"] == seeded["goal_id"]
    assert candidate["product_goal_fingerprint"] == seeded["goal_fingerprint"]
    assert candidate["specification_source_id"] == seeded["source_id"]
    assert candidate["registered_source_fingerprint"] == seeded["source_fingerprint"]
    assert candidate["source_producer_capability"] == "to-spec"
    assert candidate["source_preparation_capability"] == "grill-with-docs"
    assert candidate["producer_capability"] == "specification-structurer"
    assert candidate["prompt_version"] == SPECIFICATION_STRUCTURER_PROMPT_VERSION
    assert candidate["source_manifest"] == seeded["source_manifest"]
    assert (
        candidate["source_manifest_fingerprint"]
        == seeded["source_manifest_fingerprint"]
    )
    assert candidate["accepted_fact_fingerprint"] == seeded["accepted_fact_fingerprint"]
    assert (
        candidate["producer_input_fingerprint"] == seeded["producer_input_fingerprint"]
    )
    assert candidate["workflow_node_attempt_id"] == seeded["specification_attempt_id"]
    assert (
        candidate["attempt_fingerprint"] == seeded["specification_attempt_fingerprint"]
    )
    assert candidate["payload_fingerprint"] == seeded["payload_fingerprint"]
    assert candidate["rendered_view_fingerprint"] == seeded["rendered_view_fingerprint"]
    assert candidate["candidate_fingerprint"] == seeded["candidate_fingerprint"]
    assert candidate["base_spec_version_id"] is None
    assert candidate["base_spec_hash"] is None
    assert candidate["amendment_diff"] is None
    assert candidate["supersedes_specification_candidate_id"] is None
    assert candidate["supersedes_candidate_fingerprint"] is None
    assert candidate["decision_state"] == decision_state
    assert "content_ref" not in candidate
    assert "raw_input" not in candidate


def _seeded_int(seeded: dict[str, object], key: str) -> int:
    value = seeded[key]
    assert isinstance(value, int)
    return value


def _seed_interview_project(engine: Engine) -> dict[str, object]:
    """Seed one Project and durable attempt used by direct fact fixtures."""
    with Session(engine) as session:
        project = Project(
            name="Interview read contract",
            description="Durable human review state",
        )
        session.add(project)
        session.flush()
        assert project.project_id is not None
        attempt = WorkflowNodeAttempt(
            project_id=project.project_id,
            node_id="vision.interview",
            instance_key=None,
            graph_version=GRAPH_VERSION,
            fact_fingerprint="sha256:interview-facts",
            business_fact_fingerprint="sha256:interview-business",
            decision_fingerprint="sha256:interview-decision",
            normalized_input_json="{}",
            input_fingerprint="sha256:interview-input",
            model_id="fake/interview-read-contract",
            execution_settings_json="{}",
            idempotency_key="interview-read-attempt",
            actor="operator",
            correlation_id=None,
            started_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
            attempt_fingerprint="sha256:interview-attempt",
        )
        session.add(attempt)
        session.flush()
        assert attempt.workflow_node_attempt_id is not None
        snapshot_id = _add_vision_evidence_snapshot(
            session,
            project.project_id,
            attempt.workflow_node_attempt_id,
            key="interview-read",
        )
        result = {
            "project_id": project.project_id,
            "attempt_id": attempt.workflow_node_attempt_id,
            "attempt_fingerprint": attempt.attempt_fingerprint,
            "vision_evidence_snapshot_id": snapshot_id,
        }
        session.commit()
        return result


def _vision_components(*, complete: bool) -> JsonObject:
    return {
        "project_name": "AgileForge",
        "target_user": "Product teams",
        "problem": "Workflow state is hard to review",
        "product_category": "Product delivery system",
        "key_benefit": "Durable review context",
        "competitors": "Mutable chat history" if complete else None,
        "differentiator": "Immutable workflow facts" if complete else None,
    }


def _goal_components(*, complete: bool) -> JsonObject:
    return {
        "valuable_future_state": "Every review uses durable facts",
        "beneficiary": "Product operators",
        "value": "Reliable decisions",
        "success_signals": ["Exact candidates are reviewable"],
        "boundaries": ["No mutable cache reads"] if complete else [],
    }


def _vision_question_payload(questions: tuple[str, ...]) -> list[JsonObject]:
    return [
        {
            "question_id": f"q{index + 1}",
            "text": question,
            "affected_components": ["competitors"],
            "conflict_ids": [],
        }
        for index, question in enumerate(questions)
    ]


def _vision_display_material(
    components: JsonObject,
    statement: str,
    questions: tuple[str, ...] = (),
) -> JsonObject:
    """Return the display-safe Vision shape expected from direct fixtures."""
    return {
        "statement": statement,
        "components": [
            {"name": name, "value": value, "source_kinds": []}
            for name, value in components.items()
        ],
        "assumptions": [],
        "conflicts": [],
        "questions": [
            {
                "question_id": f"q{index + 1}",
                "text": question,
                "affected_components": ["competitors"],
            }
            for index, question in enumerate(questions)
        ],
    }


@dataclass(frozen=True)
class _VisionTurnSeed:
    """Input values for one direct Vision turn fixture."""

    components: JsonObject
    statement: str
    is_complete: bool
    questions: tuple[str, ...]
    turn_number: int
    prior_turn_id: int | None
    recorded_at: datetime


@dataclass(frozen=True)
class _GoalTurnSeed:
    """Input values for one direct Product Goal turn fixture."""

    components: JsonObject
    statement: str
    is_complete: bool
    questions: tuple[str, ...]
    goal_number: int
    revision_number: int
    prior_turn_id: int | None
    recorded_at: datetime


def _add_vision_turn(
    engine: Engine,
    seeded: dict[str, object],
    turn_seed: _VisionTurnSeed,
) -> int:
    project_id = seeded["project_id"]
    attempt_id = seeded["attempt_id"]
    attempt_fingerprint = seeded["attempt_fingerprint"]
    snapshot_id = seeded["vision_evidence_snapshot_id"]
    assert isinstance(project_id, int)
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    assert isinstance(snapshot_id, int)
    questions = _vision_question_payload(turn_seed.questions)
    operation = "bootstrap" if turn_seed.prior_turn_id is None else "clarification"
    with Session(engine) as session:
        turn = VisionInterviewTurn(
            project_id=project_id,
            operation=operation,
            turn_number=turn_seed.turn_number,
            revision_intent_id=None,
            vision_evidence_snapshot_id=snapshot_id,
            prior_turn_id=turn_seed.prior_turn_id,
            user_text=(
                None
                if operation == "bootstrap"
                else f"Vision answer {turn_seed.turn_number}"
            ),
            components_json=canonical_json(turn_seed.components),
            vision_statement=turn_seed.statement,
            is_complete=turn_seed.is_complete,
            clarifying_questions_json=canonical_json(questions),
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            output_fingerprint=_vision_output_fingerprint(
                turn_seed.components,
                turn_seed.statement,
                turn_seed.is_complete,
                questions,
            ),
            workflow_node_attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            recorded_at=turn_seed.recorded_at,
        )
        session.add(turn)
        session.commit()
        session.refresh(turn)
        assert turn.vision_interview_turn_id is not None
        return turn.vision_interview_turn_id


def _seed_vision_candidate(
    engine: Engine,
    *,
    decision: str | None = None,
) -> dict[str, object]:
    seeded = _seed_interview_project(engine)
    components = _vision_components(complete=True)
    statement = "Product teams review exact durable workflow state."
    turn_id = _add_vision_turn(
        engine,
        seeded,
        _VisionTurnSeed(
            components=components,
            statement=statement,
            is_complete=True,
            questions=(),
            turn_number=1,
            prior_turn_id=None,
            recorded_at=NOW,
        ),
    )
    project_id = seeded["project_id"]
    snapshot_id = seeded["vision_evidence_snapshot_id"]
    assert isinstance(project_id, int)
    assert isinstance(snapshot_id, int)
    with Session(engine) as session:
        artifact = VisionArtifact(
            project_id=project_id,
            version_number=1,
            components_json=canonical_json(components),
            statement=statement,
            content_fingerprint=canonical_hash(
                {"components": components, "statement": statement}
            ),
            vision_evidence_snapshot_id=snapshot_id,
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            supersedes_vision_artifact_id=None,
            source_interview_turn_id=turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=1),
        )
        session.add(artifact)
        session.flush()
        assert artifact.vision_artifact_id is not None
        if decision is not None:
            session.add(
                VisionArtifactDecision(
                    project_id=project_id,
                    vision_artifact_id=artifact.vision_artifact_id,
                    artifact_fingerprint=artifact.content_fingerprint,
                    decision=decision,
                    rationale=f"Vision {decision} rationale.",
                    reviewer="vision-reviewer",
                    idempotency_key=f"vision-{decision}",
                    decided_at=NOW + timedelta(seconds=2),
                )
            )
        seeded.update(
            vision_id=artifact.vision_artifact_id,
            vision_fingerprint=artifact.content_fingerprint,
            vision_statement=artifact.statement,
            vision_components=components,
            vision_turn_id=turn_id,
        )
        session.commit()
    return seeded


def _add_goal_turn(
    engine: Engine,
    seeded: dict[str, object],
    turn_seed: _GoalTurnSeed,
) -> int:
    project_id = seeded["project_id"]
    vision_id = seeded["vision_id"]
    vision_fingerprint = seeded["vision_fingerprint"]
    attempt_id = seeded["attempt_id"]
    attempt_fingerprint = seeded["attempt_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(vision_id, int)
    assert isinstance(vision_fingerprint, str)
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    with Session(engine) as session:
        turn = ProductGoalInterviewTurn(
            project_id=project_id,
            vision_artifact_id=vision_id,
            vision_fingerprint=vision_fingerprint,
            goal_number=turn_seed.goal_number,
            revision_number=turn_seed.revision_number,
            prior_turn_id=turn_seed.prior_turn_id,
            user_text=(
                f"Goal answer {turn_seed.goal_number}.{turn_seed.revision_number}"
            ),
            components_json=canonical_json(turn_seed.components),
            goal_statement=turn_seed.statement,
            is_complete=turn_seed.is_complete,
            clarifying_questions_json=canonical_json(list(turn_seed.questions)),
            output_fingerprint=product_goal_interview_output_fingerprint(
                turn_seed.components,
                turn_seed.statement,
                turn_seed.is_complete,
                turn_seed.questions,
            ),
            workflow_node_attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            recorded_at=turn_seed.recorded_at,
        )
        session.add(turn)
        session.commit()
        session.refresh(turn)
        assert turn.product_goal_interview_turn_id is not None
        return turn.product_goal_interview_turn_id


def _seed_goal_candidate(
    engine: Engine,
    *,
    decision: str | None = None,
) -> dict[str, object]:
    seeded = _seed_vision_candidate(engine, decision="accepted")
    components = _goal_components(complete=True)
    statement = "Make every product-definition review durable."
    turn_id = _add_goal_turn(
        engine,
        seeded,
        _GoalTurnSeed(
            components=components,
            statement=statement,
            is_complete=True,
            questions=(),
            goal_number=1,
            revision_number=1,
            prior_turn_id=None,
            recorded_at=NOW + timedelta(seconds=3),
        ),
    )
    project_id = seeded["project_id"]
    vision_id = seeded["vision_id"]
    vision_fingerprint = seeded["vision_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(vision_id, int)
    assert isinstance(vision_fingerprint, str)
    with Session(engine) as session:
        artifact = ProductGoalArtifact(
            project_id=project_id,
            vision_artifact_id=vision_id,
            vision_fingerprint=vision_fingerprint,
            goal_number=1,
            revision_number=1,
            statement=statement,
            content_fingerprint=product_goal_artifact_fingerprint(
                components, statement
            ),
            supersedes_product_goal_artifact_id=None,
            source_interview_turn_id=turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=4),
        )
        session.add(artifact)
        session.flush()
        assert artifact.product_goal_artifact_id is not None
        if decision is not None:
            session.add(
                ProductGoalArtifactDecision(
                    project_id=project_id,
                    product_goal_artifact_id=artifact.product_goal_artifact_id,
                    artifact_fingerprint=artifact.content_fingerprint,
                    decision=decision,
                    rationale=f"Goal {decision} rationale.",
                    reviewer="goal-reviewer",
                    idempotency_key=f"goal-{decision}",
                    decided_at=NOW + timedelta(seconds=5),
                )
            )
        seeded.update(
            goal_id=artifact.product_goal_artifact_id,
            goal_fingerprint=artifact.content_fingerprint,
            goal_statement=artifact.statement,
            goal_components=components,
            goal_turn_id=turn_id,
        )
        session.commit()
    return seeded


def _seed_superseded_vision_with_stale_open_intent(
    engine: Engine,
) -> dict[str, object]:
    """Persist an open intent on Vision A after accepted Vision B supersedes it."""
    seeded = _seed_vision_candidate(engine, decision="accepted")
    project_id = _seeded_int(seeded, "project_id")
    vision_id = _seeded_int(seeded, "vision_id")
    vision_fingerprint = seeded["vision_fingerprint"]
    attempt_id = _seeded_int(seeded, "attempt_id")
    snapshot_id = _seeded_int(seeded, "vision_evidence_snapshot_id")
    attempt_fingerprint = seeded["attempt_fingerprint"]
    assert isinstance(vision_fingerprint, str)
    assert isinstance(attempt_fingerprint, str)
    with Session(engine) as session:
        stale_intent = VisionRevisionIntent(
            project_id=project_id,
            source_vision_artifact_id=vision_id,
            source_vision_fingerprint=vision_fingerprint,
            reason="Keep an obsolete revision interview open.",
            initiated_by="operator",
            initiated_at=NOW + timedelta(seconds=3),
        )
        replacement_intent = VisionRevisionIntent(
            project_id=project_id,
            source_vision_artifact_id=vision_id,
            source_vision_fingerprint=vision_fingerprint,
            reason="Create the selected replacement Vision.",
            initiated_by="operator",
            initiated_at=NOW + timedelta(seconds=4),
        )
        session.add(stale_intent)
        session.add(replacement_intent)
        session.flush()
        assert stale_intent.vision_revision_intent_id is not None
        assert replacement_intent.vision_revision_intent_id is not None

        source_snapshot = session.get(VisionEvidenceSnapshot, snapshot_id)
        assert source_snapshot is not None
        stale_snapshot = VisionEvidenceSnapshot(
            project_id=project_id,
            repository_binding_id=source_snapshot.repository_binding_id,
            workflow_node_attempt_id=attempt_id,
            evidence_json=source_snapshot.evidence_json,
            evidence_fingerprint=source_snapshot.evidence_fingerprint,
            warnings_json=source_snapshot.warnings_json,
            created_at=NOW + timedelta(seconds=4),
        )
        replacement_snapshot = VisionEvidenceSnapshot(
            project_id=project_id,
            repository_binding_id=source_snapshot.repository_binding_id,
            workflow_node_attempt_id=attempt_id,
            evidence_json=source_snapshot.evidence_json,
            evidence_fingerprint=source_snapshot.evidence_fingerprint,
            warnings_json=source_snapshot.warnings_json,
            created_at=NOW + timedelta(seconds=4),
        )
        session.add(stale_snapshot)
        session.add(replacement_snapshot)
        session.flush()
        assert stale_snapshot.vision_evidence_snapshot_id is not None
        assert replacement_snapshot.vision_evidence_snapshot_id is not None

        stale_components = _vision_components(complete=False)
        stale_statement = "An obsolete Vision revision interview."
        stale_questions = [
            {
                "question_id": "stale-q1",
                "prompt": "What should the obsolete revision emphasize?",
            }
        ]
        stale_turn = VisionInterviewTurn(
            project_id=project_id,
            operation="revision",
            turn_number=1,
            revision_intent_id=stale_intent.vision_revision_intent_id,
            vision_evidence_snapshot_id=(stale_snapshot.vision_evidence_snapshot_id),
            prior_turn_id=None,
            user_text="Continue revising Vision A.",
            components_json=canonical_json(stale_components),
            vision_statement=stale_statement,
            is_complete=False,
            clarifying_questions_json=canonical_json(stale_questions),
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            output_fingerprint=_vision_output_fingerprint(
                stale_components,
                stale_statement,
                False,
                stale_questions,
            ),
            workflow_node_attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            recorded_at=NOW + timedelta(seconds=5),
        )
        replacement_components = _vision_components(complete=True)
        replacement_statement = "Product teams trust the selected durable Vision."
        replacement_turn = VisionInterviewTurn(
            project_id=project_id,
            operation="revision",
            turn_number=1,
            revision_intent_id=replacement_intent.vision_revision_intent_id,
            vision_evidence_snapshot_id=(
                replacement_snapshot.vision_evidence_snapshot_id
            ),
            prior_turn_id=None,
            user_text="Complete the selected replacement Vision.",
            components_json=canonical_json(replacement_components),
            vision_statement=replacement_statement,
            is_complete=True,
            clarifying_questions_json="[]",
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            output_fingerprint=_vision_output_fingerprint(
                replacement_components,
                replacement_statement,
                True,
                [],
            ),
            workflow_node_attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            recorded_at=NOW + timedelta(seconds=6),
        )
        session.add(stale_turn)
        session.add(replacement_turn)
        session.flush()
        assert stale_turn.vision_interview_turn_id is not None
        assert replacement_turn.vision_interview_turn_id is not None

        replacement = VisionArtifact(
            project_id=project_id,
            version_number=2,
            components_json=canonical_json(replacement_components),
            statement=replacement_statement,
            content_fingerprint=canonical_hash(
                {
                    "components": replacement_components,
                    "statement": replacement_statement,
                }
            ),
            vision_evidence_snapshot_id=(
                replacement_snapshot.vision_evidence_snapshot_id
            ),
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            supersedes_vision_artifact_id=vision_id,
            source_interview_turn_id=replacement_turn.vision_interview_turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=7),
        )
        session.add(replacement)
        session.flush()
        assert replacement.vision_artifact_id is not None
        replacement_decision = VisionArtifactDecision(
            project_id=project_id,
            vision_artifact_id=replacement.vision_artifact_id,
            artifact_fingerprint=replacement.content_fingerprint,
            decision="accepted",
            rationale="This is the current accepted Vision.",
            reviewer="vision-reviewer",
            idempotency_key="vision-replacement-accepted",
            decided_at=NOW + timedelta(seconds=8),
        )
        session.add(replacement_decision)
        session.flush()
        assert replacement_decision.vision_artifact_decision_id is not None
        session.commit()
        seeded.update(
            stale_vision_intent_id=stale_intent.vision_revision_intent_id,
            replacement_vision_intent_id=(replacement_intent.vision_revision_intent_id),
            stale_vision_turn_id=stale_turn.vision_interview_turn_id,
            replacement_vision_turn_id=replacement_turn.vision_interview_turn_id,
            current_vision_id=replacement.vision_artifact_id,
            current_vision_decision_id=(
                replacement_decision.vision_artifact_decision_id
            ),
        )
    return seeded


def _remove_superseded_vision_fixture(
    engine: Engine,
    seeded: dict[str, object],
) -> None:
    """Delete the cyclic intent/artifact fixture before SQLite drops tables."""
    with Session(engine) as session:
        for model, key in (
            (VisionArtifactDecision, "current_vision_decision_id"),
            (VisionArtifact, "current_vision_id"),
            (VisionInterviewTurn, "stale_vision_turn_id"),
            (VisionInterviewTurn, "replacement_vision_turn_id"),
            (VisionRevisionIntent, "stale_vision_intent_id"),
            (VisionRevisionIntent, "replacement_vision_intent_id"),
        ):
            row = session.get(model, _seeded_int(seeded, key))
            assert row is not None
            session.delete(row)
            session.flush()
        session.commit()


def _add_detached_goal_revision(
    engine: Engine,
    seeded: dict[str, object],
) -> int:
    """Persist Goal 1 revision 2 without its required revision 1 parent."""
    components = _goal_components(complete=True)
    statement = "Make detached revisions impossible to review."
    turn_id = _add_goal_turn(
        engine,
        seeded,
        _GoalTurnSeed(
            components=components,
            statement=statement,
            is_complete=True,
            questions=(),
            goal_number=1,
            revision_number=2,
            prior_turn_id=None,
            recorded_at=NOW + timedelta(seconds=9),
        ),
    )
    project_id = _seeded_int(seeded, "project_id")
    vision_id = _seeded_int(seeded, "vision_id")
    vision_fingerprint = seeded["vision_fingerprint"]
    assert isinstance(vision_fingerprint, str)
    with Session(engine) as session:
        detached = ProductGoalArtifact(
            project_id=project_id,
            vision_artifact_id=vision_id,
            vision_fingerprint=vision_fingerprint,
            goal_number=1,
            revision_number=2,
            statement=statement,
            content_fingerprint=product_goal_artifact_fingerprint(
                components, statement
            ),
            supersedes_product_goal_artifact_id=None,
            source_interview_turn_id=turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=10),
        )
        session.add(detached)
        session.commit()
        session.refresh(detached)
        assert detached.product_goal_artifact_id is not None
        return detached.product_goal_artifact_id


def _root_position(engine: Engine, project_id: int) -> WorkflowPosition:
    """Evaluate the root graph from the same durable snapshot as projections."""
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    return ROOT_GRAPH.evaluate(snapshot, NOW)


def test_new_project_has_empty_durable_interview_reads(engine: Engine) -> None:
    """A Project with no interview facts exposes an empty, stable contract."""
    with Session(engine) as session:
        project = Project(name="Empty interview state")
        session.add(project)
        session.commit()
        session.refresh(project)
        assert project.project_id is not None
        project_id = project.project_id

    reads = DurableReadProjectionService(engine=engine)

    assert _data(reads.vision_status(project_id=project_id)) == {
        "bootstrap_available": True,
        "current": None,
        "draft": None,
        "transcript": [],
        "candidate": None,
        "review": None,
        "stale_reason": "VISION_NOT_ACCEPTED",
    }
    assert _data(reads.product_goal_status(project_id=project_id)) == {
        "accepted_vision": None,
        "active": None,
        "transcript": [],
        "latest_questions": [],
        "candidate": None,
        "review": None,
        "outcome": None,
        "stale_reason": "GOAL_NOT_ACTIVE",
    }


def test_incomplete_vision_turn_exposes_exact_transcript_and_questions(
    engine: Engine,
) -> None:
    """The read contract preserves one incomplete Vision turn verbatim."""
    seeded = _seed_interview_project(engine)
    components = _vision_components(complete=False)
    questions = ("What alternatives do product teams use today?",)
    statement = "Product teams need durable workflow review."
    _add_vision_turn(
        engine,
        seeded,
        _VisionTurnSeed(
            components=components,
            statement=statement,
            is_complete=False,
            questions=questions,
            turn_number=1,
            prior_turn_id=None,
            recorded_at=NOW,
        ),
    )
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).vision_status(project_id=project_id)
    )

    assert data["bootstrap_available"] is False
    assert data["transcript"] == []
    assert data["draft"] == _vision_display_material(
        components,
        statement,
        questions,
    )
    assert data["candidate"] is None
    assert data["review"] is None


def test_pending_vision_exposes_exact_candidate_and_pending_review(
    engine: Engine,
) -> None:
    """A complete Vision turn projects the immutable candidate it created."""
    seeded = _seed_vision_candidate(engine)
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).vision_status(project_id=project_id)
    )
    vision_components = _JSON_OBJECT.validate_python(seeded["vision_components"])

    assert data["candidate"] == {
        **_vision_display_material(
            vision_components,
            str(seeded["vision_statement"]),
        ),
        "review_fingerprint": seeded["vision_fingerprint"],
    }
    assert data["review"] == {"state": "pending", "rationale": None}
    assert data["draft"] is None
    assert data["transcript"] == []


def test_vision_feedback_keeps_reviewed_candidate_separate_from_revision_chain(
    engine: Engine,
) -> None:
    """Feedback context remains exact while only new turns are current."""
    seeded = _seed_vision_candidate(engine, decision="feedback")
    components = _vision_components(complete=False)
    questions = ("Which differentiator should the revision emphasize?",)
    revision_statement = "Product teams need a sharper durable workflow Vision."
    _add_vision_turn(
        engine,
        seeded,
        _VisionTurnSeed(
            components=components,
            statement=revision_statement,
            is_complete=False,
            questions=questions,
            turn_number=2,
            prior_turn_id=_seeded_int(seeded, "vision_turn_id"),
            recorded_at=NOW + timedelta(seconds=3),
        ),
    )
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).vision_status(project_id=project_id)
    )

    candidate = _json_object(data["candidate"])
    assert candidate["review_fingerprint"] == seeded["vision_fingerprint"]
    assert data["review"] == {
        "state": "feedback",
        "rationale": "Vision feedback rationale.",
    }
    assert data["transcript"] == [{"user_text": "Vision answer 2"}]
    assert data["draft"] == _vision_display_material(
        components,
        revision_statement,
        questions,
    )


def _mutate_backlog_fact_content(content: JsonObject, corruption: str) -> None:
    items = content["backlog_items"]
    assert isinstance(items, list)
    first_item = items[0]
    assert isinstance(first_item, dict)
    if corruption == "is_complete_int":
        content["is_complete"] = 1
    elif corruption == "priority_bool":
        first_item["priority"] = True
    elif corruption == "backlog_item_id_bool":
        first_item["backlog_item_id"] = True
    elif corruption == "incomplete":
        content["is_complete"] = False
    elif corruption == "empty":
        content["backlog_items"] = []
    elif corruption == "clarifying_question":
        content["clarifying_questions"] = ["Which requirement is authoritative?"]
    elif corruption == "skipped_backlog_item_id":
        first_item["backlog_item_id"] = "PBI-000002"
    elif corruption == "duplicate_backlog_item_id":
        items.append(
            {
                **first_item,
                "priority": 2,
                "requirement": "Persist a second exact planning artifact.",
            }
        )
    elif corruption == "unknown_spec_item_id":
        first_item["spec_item_ids"] = ["REQ.unknown"]
    else:
        assert corruption == "noncanonical_spec_item_ids"
        first_item["spec_item_ids"] = ["REQ.delivery", "GOAL.delivery"]


@pytest.mark.parametrize(
    "review_decision",
    ["feedback", "rejected"],
)
def test_nonaccepted_vision_review_reopens_ordinary_language_clarification(
    engine: Engine,
    review_decision: str,
) -> None:
    """Feedback and rejection return to the human response node, never bootstrap."""
    seeded = _seed_vision_candidate(engine, decision=review_decision)
    project_id = _seeded_int(seeded, "project_id")

    position = _root_position(engine, project_id)
    clarification = next(
        item for item in position.decisions if item.node_id == "vision.interview"
    )

    assert clarification.category is NodeCategory.AVAILABLE
    assert clarification.request_kind == "record_vision_interview_turn"
    assert [item.name for item in clarification.required_inputs] == ["user_text"]
    assert "goal.interview" not in position.available_nodes


def test_accepted_vision_has_current_artifact_and_no_pending_candidate(
    engine: Engine,
) -> None:
    """Acceptance promotes current Vision while preserving terminal review."""
    seeded = _seed_vision_candidate(engine, decision="accepted")
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).vision_status(project_id=project_id)
    )

    assert data["current"] == {
        "statement": seeded["vision_statement"],
    }
    assert data["candidate"] is None
    assert data["transcript"] == []
    assert data["draft"] is None
    assert data["review"] == {
        "state": "accepted",
        "rationale": "Vision accepted rationale.",
    }
    assert data["stale_reason"] is None


def test_superseded_vision_open_intent_fails_closed_without_transcript(
    engine: Engine,
) -> None:
    """An intent on Vision A cannot remain current after accepted Vision B."""
    seeded = _seed_superseded_vision_with_stale_open_intent(engine)
    project_id = _seeded_int(seeded, "project_id")
    try:
        data = _data(
            DurableReadProjectionService(engine=engine).vision_status(
                project_id=project_id
            )
        )

        assert data == {
            "bootstrap_available": False,
            "current": None,
            "draft": None,
            "transcript": [],
            "candidate": None,
            "review": None,
            "stale_reason": "VISION_FACT_CONFLICT",
        }
    finally:
        _remove_superseded_vision_fixture(engine, seeded)


def test_superseded_vision_open_intent_invalidates_graph_recommendations(
    engine: Engine,
) -> None:
    """The root graph never recommends an interview for a superseded source."""
    seeded = _seed_superseded_vision_with_stale_open_intent(engine)
    project_id = _seeded_int(seeded, "project_id")
    try:
        position = _root_position(engine, project_id)
        decisions = {item.node_id: item for item in position.decisions}

        assert decisions["vision.interview"].category is NodeCategory.INVALID
        assert decisions["vision.interview"].reason_code == "WORKFLOW_FACT_CONFLICT"
        assert decisions["vision.revision.start"].category is NodeCategory.INVALID
        assert "vision.interview" not in position.available_nodes
    finally:
        _remove_superseded_vision_fixture(engine, seeded)


def test_incomplete_goal_exposes_accepted_vision_transcript_and_questions(
    engine: Engine,
) -> None:
    """Goal interview reads include immutable accepted Vision context."""
    seeded = _seed_vision_candidate(engine, decision="accepted")
    components = _goal_components(complete=False)
    questions = ("What is outside this Product Goal?",)
    statement = "Make product-definition reviews durable."
    turn_id = _add_goal_turn(
        engine,
        seeded,
        _GoalTurnSeed(
            components=components,
            statement=statement,
            is_complete=False,
            questions=questions,
            goal_number=1,
            revision_number=1,
            prior_turn_id=None,
            recorded_at=NOW + timedelta(seconds=3),
        ),
    )
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).product_goal_status(
            project_id=project_id
        )
    )

    assert data["accepted_vision"] == {
        "vision_artifact_id": seeded["vision_id"],
        "fingerprint": seeded["vision_fingerprint"],
        "statement": seeded["vision_statement"],
    }
    assert data["transcript"] == [
        {
            "product_goal_interview_turn_id": turn_id,
            "vision_artifact_id": seeded["vision_id"],
            "vision_fingerprint": seeded["vision_fingerprint"],
            "goal_number": 1,
            "revision_number": 1,
            "prior_turn_id": None,
            "user_text": "Goal answer 1.1",
            "statement": statement,
            "components": components,
            "is_complete": False,
            "clarifying_questions": list(questions),
            "output_fingerprint": product_goal_interview_output_fingerprint(
                components, statement, False, questions
            ),
            "recorded_at": _stored_iso(NOW + timedelta(seconds=3)),
        }
    ]
    assert data["latest_questions"] == list(questions)
    assert data["candidate"] is None
    assert data["review"] is None


def test_pending_goal_exposes_exact_candidate_and_pending_review(
    engine: Engine,
) -> None:
    """A complete Goal turn projects its exact immutable candidate."""
    seeded = _seed_goal_candidate(engine)
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).product_goal_status(
            project_id=project_id
        )
    )

    assert data["candidate"] == {
        "product_goal_artifact_id": seeded["goal_id"],
        "vision_artifact_id": seeded["vision_id"],
        "vision_fingerprint": seeded["vision_fingerprint"],
        "goal_number": 1,
        "revision_number": 1,
        "fingerprint": seeded["goal_fingerprint"],
        "statement": seeded["goal_statement"],
        "components": seeded["goal_components"],
        "supersedes_product_goal_artifact_id": None,
        "source_interview_turn_id": seeded["goal_turn_id"],
        "created_by": "operator",
        "created_at": _stored_iso(NOW + timedelta(seconds=4)),
    }
    assert data["review"] == {"state": "pending"}
    transcript = data["transcript"]
    assert isinstance(transcript, list)
    assert len(transcript) == 1
    assert data["latest_questions"] == []


def test_goal_feedback_keeps_candidate_separate_from_revision_chain(
    engine: Engine,
) -> None:
    """A rejected Goal remains review context while revision turns advance."""
    seeded = _seed_goal_candidate(engine, decision="feedback")
    components = _goal_components(complete=False)
    questions = ("Which boundary should the revision add?",)
    revision_turn_id = _add_goal_turn(
        engine,
        seeded,
        _GoalTurnSeed(
            components=components,
            statement="Make durable reviews narrower and measurable.",
            is_complete=False,
            questions=questions,
            goal_number=1,
            revision_number=2,
            prior_turn_id=None,
            recorded_at=NOW + timedelta(seconds=6),
        ),
    )
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).product_goal_status(
            project_id=project_id
        )
    )

    candidate = _json_object(data["candidate"])
    assert candidate["product_goal_artifact_id"] == seeded["goal_id"]
    assert data["review"] == {
        "state": "feedback",
        "product_goal_artifact_decision_id": 1,
        "decision": "feedback",
        "rationale": "Goal feedback rationale.",
        "reviewer": "goal-reviewer",
        "decided_at": _stored_iso(NOW + timedelta(seconds=5)),
    }
    transcript = data["transcript"]
    assert isinstance(transcript, list)
    assert [
        _json_object(item)["product_goal_interview_turn_id"] for item in transcript
    ] == [revision_turn_id]
    assert data["latest_questions"] == list(questions)


def test_resolved_goal_followed_by_new_interview_excludes_old_candidate(
    engine: Engine,
) -> None:
    """The next Goal transcript does not revive the resolved Goal candidate."""
    seeded = _seed_goal_candidate(engine, decision="accepted")
    project_id = seeded["project_id"]
    goal_id = seeded["goal_id"]
    goal_fingerprint = seeded["goal_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(goal_id, int)
    assert isinstance(goal_fingerprint, str)
    with Session(engine) as session:
        session.add(
            ProductGoalOutcome(
                project_id=project_id,
                product_goal_artifact_id=goal_id,
                artifact_fingerprint=goal_fingerprint,
                outcome="fulfilled",
                rationale="The first durable review Goal was fulfilled.",
                decided_by="goal-owner",
                idempotency_key="goal-one-fulfilled",
                decided_at=NOW + timedelta(seconds=6),
            )
        )
        session.commit()
    components = _goal_components(complete=False)
    questions = ("What should the next measurable outcome be?",)
    new_turn_id = _add_goal_turn(
        engine,
        seeded,
        _GoalTurnSeed(
            components=components,
            statement="Define the next durable product outcome.",
            is_complete=False,
            questions=questions,
            goal_number=2,
            revision_number=1,
            prior_turn_id=None,
            recorded_at=NOW + timedelta(seconds=7),
        ),
    )

    data = _data(
        DurableReadProjectionService(engine=engine).product_goal_status(
            project_id=project_id
        )
    )

    assert data["active"] is None
    assert data["candidate"] is None
    assert data["review"] is None
    transcript = data["transcript"]
    assert isinstance(transcript, list)
    assert [
        _json_object(item)["product_goal_interview_turn_id"] for item in transcript
    ] == [new_turn_id]
    outcome = _json_object(data["outcome"])
    assert outcome["product_goal_artifact_id"] == goal_id
    assert data["stale_reason"] == "GOAL_RESOLVED"


@pytest.mark.parametrize("prior_state", ["feedback", "resolved"])
def test_detached_goal_revision_fails_closed_in_projection(
    engine: Engine,
    prior_state: str,
) -> None:
    """Revision 2 without its exact revision 1 parent is never current."""
    seeded = _seed_goal_candidate(
        engine,
        decision="feedback" if prior_state == "feedback" else "accepted",
    )
    if prior_state == "resolved":
        _resolve_goal(engine, seeded)
    _add_detached_goal_revision(engine, seeded)
    project_id = _seeded_int(seeded, "project_id")

    data = _data(
        DurableReadProjectionService(engine=engine).product_goal_status(
            project_id=project_id
        )
    )

    assert data == {
        "accepted_vision": {
            "vision_artifact_id": seeded["vision_id"],
            "fingerprint": seeded["vision_fingerprint"],
            "statement": seeded["vision_statement"],
        },
        "active": None,
        "transcript": [],
        "latest_questions": [],
        "candidate": None,
        "review": None,
        "outcome": None,
        "stale_reason": "PRODUCT_GOAL_FACT_CONFLICT",
    }


@pytest.mark.parametrize("prior_state", ["feedback", "resolved"])
def test_detached_goal_revision_invalidates_graph_review(
    engine: Engine,
    prior_state: str,
) -> None:
    """Detached Goal revisions cannot become graph-current review candidates."""
    seeded = _seed_goal_candidate(
        engine,
        decision="feedback" if prior_state == "feedback" else "accepted",
    )
    if prior_state == "resolved":
        _resolve_goal(engine, seeded)
    detached_id = _add_detached_goal_revision(engine, seeded)
    project_id = _seeded_int(seeded, "project_id")
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)

    interview = _goal_interview_rule(snapshot, NOW)[0]
    review = _goal_review_rule(snapshot, NOW)[0]

    assert interview.category is RuleCategory.INVALID
    assert interview.reason_code == "WORKFLOW_FACT_CONFLICT"
    assert review.category is RuleCategory.INVALID
    assert all(
        reference.fact_id != str(detached_id) for reference in review.fact_references
    )


def test_ambiguous_vision_leaf_fails_closed_with_typed_stale_reason(
    engine: Engine,
) -> None:
    """Two immutable Vision leaves never degrade to latest-row selection."""
    seeded = _seed_vision_candidate(engine, decision="accepted")
    components = _vision_components(complete=True)
    statement = "A conflicting durable Vision leaf."
    turn_id = _add_vision_turn(
        engine,
        seeded,
        _VisionTurnSeed(
            components=components,
            statement=statement,
            is_complete=True,
            questions=(),
            turn_number=2,
            prior_turn_id=_seeded_int(seeded, "vision_turn_id"),
            recorded_at=NOW + timedelta(seconds=3),
        ),
    )
    project_id = seeded["project_id"]
    snapshot_id = seeded["vision_evidence_snapshot_id"]
    assert isinstance(project_id, int)
    assert isinstance(snapshot_id, int)
    with Session(engine) as session:
        session.add(
            VisionArtifact(
                project_id=project_id,
                version_number=2,
                components_json=canonical_json(components),
                statement=statement,
                content_fingerprint=canonical_hash(
                    {"components": components, "statement": statement}
                ),
                vision_evidence_snapshot_id=snapshot_id,
                component_basis_json="[]",
                assumptions_json="[]",
                conflicts_json="[]",
                supersedes_vision_artifact_id=None,
                source_interview_turn_id=turn_id,
                created_by="operator",
                created_at=NOW + timedelta(seconds=4),
            )
        )
        session.commit()

    data = _data(
        DurableReadProjectionService(engine=engine).vision_status(project_id=project_id)
    )

    assert data == {
        "bootstrap_available": False,
        "current": None,
        "draft": None,
        "transcript": [],
        "candidate": None,
        "review": None,
        "stale_reason": "VISION_FACT_CONFLICT",
    }


def _resolve_goal(engine: Engine, seeded: dict[str, object]) -> None:
    """Record the exact terminal outcome for the fixture's accepted first Goal."""
    project_id = seeded["project_id"]
    goal_id = seeded["goal_id"]
    goal_fingerprint = seeded["goal_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(goal_id, int)
    assert isinstance(goal_fingerprint, str)
    with Session(engine) as session:
        session.add(
            ProductGoalOutcome(
                project_id=project_id,
                product_goal_artifact_id=goal_id,
                artifact_fingerprint=goal_fingerprint,
                outcome="fulfilled",
                rationale="Observable success signals were reached.",
                decided_by="operator",
                idempotency_key="goal-fulfilled",
                decided_at=NOW + timedelta(seconds=8),
            )
        )
        session.commit()


def _accept_next_goal(engine: Engine, seeded: dict[str, object]) -> None:
    """Add the next accepted Goal under the unchanged accepted Vision."""
    project_id = seeded["project_id"]
    vision_id = seeded["vision_id"]
    vision_fingerprint = seeded["vision_fingerprint"]
    attempt_id = seeded["attempt_id"]
    attempt_fingerprint = seeded["attempt_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(vision_id, int)
    assert isinstance(vision_fingerprint, str)
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    components = {
        "valuable_future_state": "Transparent decisions",
        "beneficiary": "Operators",
        "value": "Trust",
        "success_signals": ["Auditable outcomes"],
        "boundaries": ["No implementation"],
    }
    statement = "Goal 2: transparent decisions."
    with Session(engine) as session:
        turn = ProductGoalInterviewTurn(
            project_id=project_id,
            vision_artifact_id=vision_id,
            vision_fingerprint=vision_fingerprint,
            goal_number=2,
            revision_number=1,
            prior_turn_id=None,
            user_text="Define the next goal",
            components_json=canonical_json(components),
            goal_statement=statement,
            is_complete=True,
            clarifying_questions_json="[]",
            output_fingerprint=product_goal_interview_output_fingerprint(
                components, statement, True, ()
            ),
            workflow_node_attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            recorded_at=NOW + timedelta(seconds=9),
        )
        session.add(turn)
        session.flush()
        assert turn.product_goal_interview_turn_id is not None
        goal = ProductGoalArtifact(
            project_id=project_id,
            vision_artifact_id=vision_id,
            vision_fingerprint=vision_fingerprint,
            goal_number=2,
            revision_number=1,
            statement=statement,
            content_fingerprint=product_goal_artifact_fingerprint(
                components, statement
            ),
            supersedes_product_goal_artifact_id=None,
            source_interview_turn_id=turn.product_goal_interview_turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=10),
        )
        session.add(goal)
        session.flush()
        assert goal.product_goal_artifact_id is not None
        session.add(
            ProductGoalArtifactDecision(
                project_id=project_id,
                product_goal_artifact_id=goal.product_goal_artifact_id,
                artifact_fingerprint=goal.content_fingerprint,
                decision="accepted",
                rationale="Reviewed.",
                reviewer="operator",
                idempotency_key="goal-2-accepted",
                decided_at=NOW + timedelta(seconds=11),
            )
        )
        session.commit()


def test_durable_projections_expose_current_human_content_and_pending_review(  # noqa: PLR0915
    engine: Engine,
) -> None:
    """Pending status and review expose one complete exact v2 candidate packet."""
    seeded = _seed_lineage(engine)
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)
    vision_id = seeded["vision_id"]
    vision_fingerprint = seeded["vision_fingerprint"]
    goal_id = seeded["goal_id"]
    goal_fingerprint = seeded["goal_fingerprint"]
    goal_statement = seeded["goal_statement"]
    assert isinstance(vision_id, int)
    assert isinstance(vision_fingerprint, str)
    assert isinstance(goal_id, int)
    assert isinstance(goal_fingerprint, str)
    assert isinstance(goal_statement, str)

    reads = DurableReadProjectionService(engine=engine)
    vision_data = _data(reads.vision_status(project_id=project_id))
    goal_data = _data(reads.product_goal_status(project_id=project_id))
    specification_data = _data(reads.specification_status(project_id=project_id))
    review_data = _data(reads.specification_review(project_id=project_id))

    assert vision_data["current"] == {
        "statement": "A durable Vision.",
    }
    active_goal = _json_object(goal_data["active"])
    assert active_goal["product_goal_artifact_id"] == goal_id
    assert active_goal["statement"] == goal_statement
    candidate_data = _json_object(specification_data["candidate"])
    _assert_complete_candidate_projection(
        candidate_data,
        seeded,
        decision_state="pending",
    )
    assert specification_data["schema_version"] == (
        "agileforge.specification_review.v2"
    )
    source_data = _json_object(specification_data["source"])
    assert source_data["specification_source_id"] == seeded["source_id"]
    assert source_data["source_fingerprint"] == seeded["source_fingerprint"]
    assert source_data["producer_capability"] == "to-spec"
    assert source_data["preparation_capability"] == "grill-with-docs"
    source_document = _json_object(source_data["source"])
    assert source_document == {
        "source_id": SPECIFICATION_SOURCE_PRIMARY_ID,
        "relative_path": "SPECIFICATION.md",
        "byte_length": len(b"REGISTERED_TO_SPEC_SOURCE_MUST_NOT_LEAK\n"),
        "content_fingerprint": seeded["source_document_fingerprint"],
    }
    context = _json_object(source_data["context"])
    assert context["state"] == "present"
    context_document = _json_object(context["document"])
    assert (
        context_document["content_fingerprint"]
        == seeded["context_document_fingerprint"]
    )
    adrs = source_data["adrs"]
    assert isinstance(adrs, list)
    assert (
        _json_object(adrs[0])["content_fingerprint"]
        == seeded["adr_document_fingerprint"]
    )
    repository = _json_object(source_data["repository"])
    assert repository["repository_binding_id"] == seeded["repository_binding_id"]
    assert repository["status_fingerprint"] == seeded["repository_status_fingerprint"]
    serialized_source = json.dumps(source_data)
    assert "content_base64" not in serialized_source
    assert "REGISTERED_TO_SPEC_SOURCE_MUST_NOT_LEAK" not in serialized_source
    assert "REGISTERED_CONTEXT_MUST_NOT_LEAK" not in serialized_source
    assert "REGISTERED_ADR_MUST_NOT_LEAK" not in serialized_source
    assert specification_data["current"] is None
    assert review_data["review"] == {"state": "pending"}
    assert review_data["candidate"] == candidate_data
    assert review_data["source"] == source_data
    assert review_data["schema_version"] == "agileforge.specification_review.v2"


def test_registered_source_status_precedes_structured_candidate(
    engine: Engine,
) -> None:
    """A registered source is visible as digest metadata before structuring."""
    seeded = _seed_lineage(engine)
    project_id = _seeded_int(seeded, "project_id")
    candidate_id = _seeded_int(seeded, "candidate_id")
    with Session(engine) as session:
        candidate = session.get(SpecificationCandidate, candidate_id)
        assert candidate is not None
        session.delete(candidate)
        session.commit()

    status = _data(
        DurableReadProjectionService(engine=engine).specification_status(
            project_id=project_id
        )
    )

    source = _json_object(status["source"])
    assert source["source_fingerprint"] == seeded["source_fingerprint"]
    assert status["candidate"] is None
    assert status["review"] is None
    assert status["stale_reason"] == "SPECIFICATION_NOT_STRUCTURED"
    assert "content_base64" not in json.dumps(source)


def test_successor_source_retains_current_accepted_spec_before_amendment(
    engine: Engine,
) -> None:
    """Registering amendment bytes does not hide the still-current accepted base."""
    seeded = _seed_lineage(engine)
    project_id = _seeded_int(seeded, "project_id")
    candidate_id = _seeded_int(seeded, "candidate_id")
    source_id = _seeded_int(seeded, "source_id")
    with Session(engine) as session:
        candidate = session.get(SpecificationCandidate, candidate_id)
        source = session.get(SpecificationSource, source_id)
        assert candidate is not None
        assert source is not None
        payload_fingerprint = candidate.payload_fingerprint
        decision = SpecificationDecision(
            project_id=project_id,
            specification_candidate_id=candidate_id,
            candidate_fingerprint=candidate.candidate_fingerprint,
            decision="accepted",
            rationale="Approved base.",
            reviewer="operator",
            idempotency_key="accepted-base-before-successor-source",
            decided_at=NOW + timedelta(seconds=8),
        )
        session.add(decision)
        session.flush()
        assert decision.specification_decision_id is not None
        registry = SpecRegistry(
            project_id=project_id,
            spec_hash=candidate.payload_fingerprint,
            status="approved",
            approved_at=NOW + timedelta(seconds=8),
            approved_by="operator",
            source_specification_decision_id=(decision.specification_decision_id),
            source_specification_candidate_id=candidate_id,
            source_specification_candidate_fingerprint=(
                candidate.candidate_fingerprint
            ),
            source_vision_artifact_id=candidate.vision_artifact_id,
            source_vision_fingerprint=candidate.vision_fingerprint,
            source_product_goal_artifact_id=candidate.product_goal_artifact_id,
            source_product_goal_fingerprint=candidate.product_goal_fingerprint,
        )
        session.add(registry)
        successor = SpecificationSource(
            project_id=project_id,
            source_bundle_json=source.source_bundle_json,
            source_fingerprint=source.source_fingerprint,
            repository_binding_id=source.repository_binding_id,
            repository_head_sha=source.repository_head_sha,
            repository_dirty=source.repository_dirty,
            repository_status_fingerprint=source.repository_status_fingerprint,
            vision_artifact_id=source.vision_artifact_id,
            vision_fingerprint=source.vision_fingerprint,
            product_goal_artifact_id=source.product_goal_artifact_id,
            product_goal_fingerprint=source.product_goal_fingerprint,
            supersedes_specification_source_id=source_id,
            supersedes_source_fingerprint=source.source_fingerprint,
            registered_by="operator",
            registered_at=NOW + timedelta(seconds=9),
        )
        session.add(successor)
        session.commit()
        session.refresh(registry)
        session.refresh(successor)
        registry_id = registry.spec_version_id
        successor_id = successor.specification_source_id

    status = _data(
        DurableReadProjectionService(engine=engine).specification_status(
            project_id=project_id
        )
    )

    source_data = _json_object(status["source"])
    current = _json_object(status["current"])
    assert source_data["specification_source_id"] == successor_id
    assert current["spec_version_id"] == registry_id
    assert current["spec_hash"] == payload_fingerprint
    accepted_candidate = _json_object(current["candidate"])
    assert accepted_candidate["specification_candidate_id"] == candidate_id
    assert status["candidate"] is None
    assert status["review"] is None
    assert status["stale_reason"] == "SPECIFICATION_NOT_STRUCTURED"


def test_pending_amendment_review_projects_the_exact_amendment_candidate(  # noqa: PLR0915
    engine: Engine,
) -> None:
    """An approved base cannot replace its pending amendment review packet."""
    seeded = _seed_lineage(engine)
    project_id = _seeded_int(seeded, "project_id")
    initial_candidate_id = _seeded_int(seeded, "candidate_id")
    source_id = _seeded_int(seeded, "source_id")
    base_payload = SpecificationPayload.model_validate(seeded["specification_payload"])
    amendment_payload = base_payload.model_copy(
        update={"summary": "Project the exact pending amendment for review."}
    )

    with Session(engine) as session:
        initial_candidate = session.get(
            SpecificationCandidate,
            initial_candidate_id,
        )
        original_source = session.get(SpecificationSource, source_id)
        assert initial_candidate is not None
        assert original_source is not None
        base_decision = SpecificationDecision(
            project_id=project_id,
            specification_candidate_id=initial_candidate_id,
            candidate_fingerprint=initial_candidate.candidate_fingerprint,
            decision="accepted",
            rationale="Approved base.",
            reviewer="operator",
            idempotency_key="approve-base-before-amendment",
            decided_at=NOW + timedelta(seconds=8),
        )
        session.add(base_decision)
        session.flush()
        assert base_decision.specification_decision_id is not None
        base_registry = SpecRegistry(
            project_id=project_id,
            spec_hash=initial_candidate.payload_fingerprint,
            status="approved",
            approved_at=NOW + timedelta(seconds=8),
            approved_by="operator",
            source_specification_decision_id=(base_decision.specification_decision_id),
            source_specification_candidate_id=initial_candidate_id,
            source_specification_candidate_fingerprint=(
                initial_candidate.candidate_fingerprint
            ),
            source_vision_artifact_id=initial_candidate.vision_artifact_id,
            source_vision_fingerprint=initial_candidate.vision_fingerprint,
            source_product_goal_artifact_id=(
                initial_candidate.product_goal_artifact_id
            ),
            source_product_goal_fingerprint=(
                initial_candidate.product_goal_fingerprint
            ),
        )
        session.add(base_registry)
        session.flush()
        assert base_registry.spec_version_id is not None
        base_spec_version_id = base_registry.spec_version_id
        base_spec_hash = base_registry.spec_hash

        original_bundle = SpecificationSourceBundle.model_validate_json(
            original_source.source_bundle_json
        )
        amendment_document = _registered_document(
            source_id=SPECIFICATION_SOURCE_PRIMARY_ID,
            relative_path="SPECIFICATION.md",
            content=b"EXACT_PENDING_AMENDMENT_SOURCE\n",
        )
        amendment_bundle = SpecificationSourceBundle(
            source=amendment_document,
            context=original_bundle.context,
            adrs=original_bundle.adrs,
            repository_revision=original_bundle.repository_revision,
            accepted_vision_fingerprint=(original_bundle.accepted_vision_fingerprint),
            accepted_product_goal_fingerprint=(
                original_bundle.accepted_product_goal_fingerprint
            ),
        )
        amendment_source = SpecificationSource(
            project_id=project_id,
            source_bundle_json=canonical_json(amendment_bundle.model_dump(mode="json")),
            source_fingerprint=source_bundle_fingerprint(amendment_bundle),
            repository_binding_id=original_source.repository_binding_id,
            repository_head_sha=original_source.repository_head_sha,
            repository_dirty=original_source.repository_dirty,
            repository_status_fingerprint=(
                original_source.repository_status_fingerprint
            ),
            vision_artifact_id=original_source.vision_artifact_id,
            vision_fingerprint=original_source.vision_fingerprint,
            product_goal_artifact_id=original_source.product_goal_artifact_id,
            product_goal_fingerprint=original_source.product_goal_fingerprint,
            supersedes_specification_source_id=source_id,
            supersedes_source_fingerprint=original_source.source_fingerprint,
            registered_by="operator",
            registered_at=NOW + timedelta(seconds=9),
        )
        session.add(amendment_source)
        session.flush()
        assert amendment_source.specification_source_id is not None

        initial_attempt = session.get(
            WorkflowNodeAttempt,
            initial_candidate.workflow_node_attempt_id,
        )
        assert initial_attempt is not None
        initial_structuring_input = SpecificationStructuringInput.model_validate_json(
            initial_attempt.normalized_input_json
        )
        stored_manifest = seeded["source_manifest"]
        assert isinstance(stored_manifest, list)
        original_manifest = tuple(
            CandidateSourceManifestEntry.model_validate(item)
            for item in stored_manifest
        )
        amendment_manifest = tuple(
            CandidateSourceManifestEntry(
                source_id=item.source_id,
                kind=item.kind,
                fingerprint=(
                    amendment_document.content_fingerprint
                    if item.source_id == SPECIFICATION_SOURCE_PRIMARY_ID
                    else item.fingerprint
                ),
                warnings=item.warnings,
            )
            for item in original_manifest
        )
        amendment_structuring_input = SpecificationStructuringInput(
            project_id=project_id,
            project_name=initial_structuring_input.project_name,
            operation="amendment",
            accepted_vision=initial_structuring_input.accepted_vision,
            accepted_product_goal=(initial_structuring_input.accepted_product_goal),
            registered_source=RegisteredSpecificationSource(
                specification_source_id=(amendment_source.specification_source_id),
                source_fingerprint=amendment_source.source_fingerprint,
                producer_capability=amendment_bundle.producer_capability,
                preparation_capability=amendment_bundle.preparation_capability,
                source=_structuring_document(amendment_document),
                context=initial_structuring_input.registered_source.context,
                adrs=initial_structuring_input.registered_source.adrs,
                repository_revision=amendment_bundle.repository_revision,
                repository_evidence=(
                    initial_structuring_input.registered_source.repository_evidence
                ),
                accepted_vision_fingerprint=(
                    amendment_bundle.accepted_vision_fingerprint
                ),
                accepted_product_goal_fingerprint=(
                    amendment_bundle.accepted_product_goal_fingerprint
                ),
            ),
            source_manifest=amendment_manifest,
            base_specification=BaseSpecificationContext(
                spec_version_id=base_registry.spec_version_id,
                payload_fingerprint=base_registry.spec_hash,
                payload=base_payload,
            ),
        )
        normalized_input = amendment_structuring_input.model_dump(mode="json")
        amendment_attempt = WorkflowNodeAttempt(
            project_id=project_id,
            node_id="specification.structure",
            instance_key=str(base_registry.spec_version_id),
            graph_version=GRAPH_VERSION,
            fact_fingerprint=canonical_hash({"amendment": "facts"}),
            business_fact_fingerprint=canonical_hash({"amendment": "business"}),
            decision_fingerprint=canonical_hash({"amendment": "decision"}),
            normalized_input_json=canonical_json(normalized_input),
            input_fingerprint=canonical_hash(normalized_input),
            model_id="fake/product-definition",
            execution_settings_json="{}",
            idempotency_key="pending-amendment-attempt",
            actor="operator",
            correlation_id="pending-amendment",
            started_at=NOW + timedelta(seconds=10),
            lease_expires_at=NOW + timedelta(minutes=1),
            attempt_fingerprint=canonical_hash({"amendment": "pending"}),
        )
        session.add(amendment_attempt)
        session.flush()
        assert amendment_attempt.workflow_node_attempt_id is not None
        amendment_attempt.attempt_fingerprint = workflow_node_attempt_fingerprint(
            {
                "attempt_id": amendment_attempt.workflow_node_attempt_id,
                "project_id": amendment_attempt.project_id,
                "node_id": amendment_attempt.node_id,
                "instance_key": amendment_attempt.instance_key,
                "graph_version": amendment_attempt.graph_version,
                "fact_fingerprint": amendment_attempt.fact_fingerprint,
                "business_fact_fingerprint": (
                    amendment_attempt.business_fact_fingerprint
                ),
                "decision_fingerprint": amendment_attempt.decision_fingerprint,
                "normalized_input": normalized_input,
                "input_fingerprint": amendment_attempt.input_fingerprint,
                "model_id": amendment_attempt.model_id,
                "execution_settings": {},
                "idempotency_key": amendment_attempt.idempotency_key,
                "actor": amendment_attempt.actor,
                "correlation_id": amendment_attempt.correlation_id,
                "started_at": amendment_attempt.started_at,
                "lease_expires_at": amendment_attempt.lease_expires_at,
            }
        )
        session.add(amendment_attempt)
        session.flush()
        amendment_envelope = build_candidate_envelope(
            payload=amendment_payload,
            metadata=CandidateBuildInput(
                candidate_kind=CandidateKind.AMENDMENT,
                accepted_vision_id=initial_candidate.vision_artifact_id,
                accepted_vision_fingerprint=initial_candidate.vision_fingerprint,
                accepted_product_goal_id=(initial_candidate.product_goal_artifact_id),
                accepted_product_goal_fingerprint=(
                    initial_candidate.product_goal_fingerprint
                ),
                registered_source_fingerprint=(amendment_source.source_fingerprint),
                source_producer_capability="to-spec",
                source_preparation_capability="grill-with-docs",
                source_manifest=amendment_manifest,
                accepted_fact_fingerprint=(
                    specification_structuring_fact_fingerprint(
                        amendment_structuring_input
                    )
                ),
                producer_input_fingerprint=(
                    specification_structuring_input_fingerprint(
                        amendment_structuring_input
                    )
                ),
                producer_capability="specification-structurer",
                producer_version="1.0.0",
                model_id=amendment_attempt.model_id,
                model_configuration_fingerprint=canonical_hash(
                    {"model": amendment_attempt.model_id}
                ),
                prompt_version=SPECIFICATION_STRUCTURER_PROMPT_VERSION,
                prompt_fingerprint=canonical_hash({"prompt": "specification-v2"}),
                workflow_node_attempt_id=(amendment_attempt.workflow_node_attempt_id),
                attempt_fingerprint=amendment_attempt.attempt_fingerprint,
                correlation_id="pending-amendment",
                produced_at=NOW + timedelta(seconds=11),
                base_payload=base_payload,
                base_specification_id=base_registry.spec_version_id,
                base_payload_fingerprint=base_registry.spec_hash,
            ),
        )
        amendment_candidate = SpecificationCandidate(
            project_id=project_id,
            candidate_kind="amendment",
            specification_source_id=amendment_source.specification_source_id,
            specification_source_fingerprint=amendment_source.source_fingerprint,
            vision_artifact_id=initial_candidate.vision_artifact_id,
            vision_fingerprint=initial_candidate.vision_fingerprint,
            product_goal_artifact_id=initial_candidate.product_goal_artifact_id,
            product_goal_fingerprint=initial_candidate.product_goal_fingerprint,
            base_spec_version_id=base_registry.spec_version_id,
            base_spec_hash=base_registry.spec_hash,
            canonical_envelope_json=canonical_candidate_json(
                amendment_payload,
                amendment_envelope,
            ),
            payload_fingerprint=amendment_envelope.payload_fingerprint,
            source_manifest_fingerprint=(
                amendment_envelope.source_manifest_fingerprint
            ),
            producer_input_fingerprint=(amendment_envelope.producer_input_fingerprint),
            rendered_view_fingerprint=(amendment_envelope.review_view_fingerprint),
            candidate_fingerprint=amendment_envelope.candidate_fingerprint,
            workflow_node_attempt_id=amendment_attempt.workflow_node_attempt_id,
            attempt_fingerprint=amendment_attempt.attempt_fingerprint,
            supersedes_specification_candidate_id=initial_candidate_id,
            supersedes_candidate_fingerprint=(initial_candidate.candidate_fingerprint),
            recorded_by="operator",
            recorded_at=NOW + timedelta(seconds=11),
        )
        session.add(amendment_candidate)
        session.flush()
        assert amendment_candidate.specification_candidate_id is not None
        amendment_candidate_id = amendment_candidate.specification_candidate_id
        session.commit()

    review = _data(
        DurableReadProjectionService(engine=engine).specification_review(
            project_id=project_id
        )
    )
    status = _data(
        DurableReadProjectionService(engine=engine).specification_status(
            project_id=project_id
        )
    )

    candidate = _json_object(review["candidate"])
    assert status["candidate"] == review["candidate"]
    assert status["review"] == {"state": "pending"}
    assert status["current"] is None
    assert review["review"] == {"state": "pending"}
    assert candidate["specification_candidate_id"] == amendment_candidate_id
    assert candidate["candidate_kind"] == "amendment"
    assert candidate["canonical_payload"] == amendment_payload.model_dump(mode="json")
    assert candidate["rendered_markdown"] == render_candidate_review_markdown(
        amendment_payload,
        amendment_envelope,
    )
    assert candidate["base_spec_version_id"] == base_spec_version_id
    assert candidate["base_spec_hash"] == base_spec_hash
    assert candidate["decision_state"] == "pending"
    amendment_diff = _json_object(candidate["amendment_diff"])
    assert amendment_diff["changed_fields"] == ["summary"]


@pytest.mark.parametrize(
    ("decision", "expect_registry"),
    [("feedback", False), ("rejected", False), ("accepted", True)],
)
def test_specification_projections_expose_terminal_review_and_registry_content(
    engine: Engine,
    decision: str,
    expect_registry: bool,
) -> None:
    """Terminal reads resolve exact candidate bytes through the registry row."""
    seeded = _seed_lineage(engine)
    project_id = seeded["project_id"]
    candidate_id = seeded["candidate_id"]
    candidate_fingerprint = seeded["candidate_fingerprint"]
    payload_fingerprint = seeded["payload_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(candidate_id, int)
    assert isinstance(candidate_fingerprint, str)
    assert isinstance(payload_fingerprint, str)
    with Session(engine) as session:
        terminal_decision = SpecificationDecision(
            project_id=project_id,
            specification_candidate_id=candidate_id,
            candidate_fingerprint=candidate_fingerprint,
            decision=decision,
            rationale="Ready.",
            reviewer="operator",
            idempotency_key="spec-accepted",
            decided_at=NOW + timedelta(seconds=8),
        )
        session.add(terminal_decision)
        session.flush()
        assert terminal_decision.specification_decision_id is not None
        registry: SpecRegistry | None = None
        if expect_registry:
            registry = SpecRegistry(
                project_id=project_id,
                spec_hash=payload_fingerprint,
                status="approved",
                source_specification_decision_id=(
                    terminal_decision.specification_decision_id
                ),
                source_specification_candidate_id=candidate_id,
                source_specification_candidate_fingerprint=candidate_fingerprint,
                source_vision_artifact_id=seeded["vision_id"],
                source_vision_fingerprint=seeded["vision_fingerprint"],
                source_product_goal_artifact_id=seeded["goal_id"],
                source_product_goal_fingerprint=seeded["goal_fingerprint"],
            )
            session.add(registry)
        session.commit()
        if registry is not None:
            session.refresh(registry)

    reads = DurableReadProjectionService(engine=engine)
    status = _data(reads.specification_status(project_id=project_id))
    review = _data(reads.specification_review(project_id=project_id))

    current = status["current"]
    if registry is None:
        assert current is None
        assert status["stale_reason"] == "SPECIFICATION_NOT_APPROVED"
    else:
        current = _json_object(current)
        assert current["spec_version_id"] == registry.spec_version_id
        assert current["spec_hash"] == payload_fingerprint
        assert current["source_specification_candidate_id"] == candidate_id
        assert (
            current["source_specification_candidate_fingerprint"]
            == candidate_fingerprint
        )
    candidate_projection = _json_object(review["candidate"])
    _assert_complete_candidate_projection(
        candidate_projection,
        seeded,
        decision_state=decision,
    )
    assert review["review"] == {
        "state": decision,
        "specification_decision_id": 1,
        "decision": decision,
        "rationale": "Ready.",
        "reviewer": "operator",
    }


def test_obsolete_product_discovery_selection_service_is_removed() -> None:
    """The deleted Discovery gate has no standalone selection service."""
    assert importlib.util.find_spec("services.product_discovery_selection") is None


def test_resolved_goal_and_next_goal_leave_old_product_definition_non_current(
    engine: Engine,
) -> None:
    """A later Goal keeps the old pending candidate as an exact review target."""
    seeded = _seed_lineage(engine)
    project_id = seeded["project_id"]
    goal_id = seeded["goal_id"]
    goal_fingerprint = seeded["goal_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(goal_id, int)
    assert isinstance(goal_fingerprint, str)
    _resolve_goal(engine, seeded)

    reads = DurableReadProjectionService(engine=engine)
    resolved = _data(reads.product_goal_status(project_id=project_id))
    assert resolved == {
        "accepted_vision": {
            "vision_artifact_id": seeded["vision_id"],
            "fingerprint": seeded["vision_fingerprint"],
            "statement": "A durable Vision.",
        },
        "active": None,
        "transcript": [],
        "latest_questions": [],
        "candidate": None,
        "review": None,
        "outcome": {
            "product_goal_artifact_id": goal_id,
            "fingerprint": goal_fingerprint,
            "statement": "Goal 1: reliable decisions.",
            "goal_number": 1,
            "revision_number": 1,
            "outcome": "fulfilled",
            "rationale": "Observable success signals were reached.",
            "decided_by": "operator",
        },
        "stale_reason": "GOAL_RESOLVED",
    }

    _accept_next_goal(engine, seeded)

    vision = _data(reads.vision_status(project_id=project_id))
    active = _data(reads.product_goal_status(project_id=project_id))
    specification = _data(reads.specification_status(project_id=project_id))
    review = _data(reads.specification_review(project_id=project_id))
    assert vision["current"] is not None
    active_goal = _json_object(active["active"])
    assert active_goal["statement"] == "Goal 2: transparent decisions."
    assert active_goal["product_goal_artifact_id"] != goal_id
    assert specification["schema_version"] == "agileforge.specification_review.v2"
    assert specification["source"] is None
    assert specification["current"] is None
    assert specification["review"] == {"state": "pending"}
    assert specification["stale_reason"] == "SPECIFICATION_NOT_APPROVED"
    candidate = _json_object(specification["candidate"])
    _assert_complete_candidate_projection(
        candidate,
        seeded,
        decision_state="pending",
    )
    assert review == {
        "schema_version": "agileforge.specification_review.v2",
        "source": None,
        "candidate": candidate,
        "review": {"state": "pending"},
        "stale_reason": None,
    }


def test_malformed_durable_projection_data_returns_typed_error(engine: Engine) -> None:
    """Loader validation failures are reported as typed reads instead of crashes."""
    seeded = _seed_lineage(engine)
    project_id = seeded["project_id"]
    candidate_id = seeded["candidate_id"]
    assert isinstance(project_id, int)
    assert isinstance(candidate_id, int)
    with Session(engine) as session:
        candidate = session.get(SpecificationCandidate, candidate_id)
        assert candidate is not None
        candidate.canonical_envelope_json = "not-json"
        session.add(candidate)
        session.commit()

    result = DurableReadProjectionService(engine=engine).specification_status(
        project_id=project_id
    )

    assert _error_code(result) == "PROJECT_FACTS_UNAVAILABLE"


def _seed_task_7_backlog(engine: Engine) -> tuple[int, int, str, int]:
    from services.agent_workbench.backlog_phase import (  # noqa: PLC0415
        record_backlog_draft_in_session,
    )
    from tests.workflow.test_vision_backlog_transitions import (  # noqa: PLC0415
        EVALUATED_AT,
        _backlog_content,
        _seed_project_specification,
    )

    with Session(engine) as session:
        lineage = _seed_project_specification(session)
        project_id = lineage.spec.project_id
        spec_version_id = lineage.spec.spec_version_id
        assert spec_version_id is not None
        content = _backlog_content()
        backlog = record_backlog_draft_in_session(
            session,
            project_id=project_id,
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
        session.commit()
        return (
            project_id,
            int(backlog.backlog_artifact_id or 0),
            backlog.content_fingerprint,
            spec_version_id,
        )


def _seed_task_7_roadmap(engine: Engine) -> tuple[int, int]:
    from services.agent_workbench.backlog_phase import (  # noqa: PLC0415
        record_backlog_decision_in_session,
    )
    from services.agent_workbench.roadmap_phase import (  # noqa: PLC0415
        RecordRoadmapDraftInput,
        record_roadmap_draft_in_session,
    )
    from tests.workflow.test_planning_transitions import (  # noqa: PLC0415
        _roadmap_content,
    )
    from tests.workflow.test_vision_backlog_transitions import (  # noqa: PLC0415
        EVALUATED_AT,
    )

    project_id, backlog_id, backlog_fingerprint, _spec_version_id = (
        _seed_task_7_backlog(engine)
    )
    with Session(engine) as session:
        backlog = session.get(BacklogArtifact, backlog_id)
        assert backlog is not None
        record_backlog_decision_in_session(
            session,
            artifact=backlog,
            decision="accepted",
            rationale="Accept projection parent.",
            reviewer="operator@example.com",
            idempotency_key="accept-projection-parent",
            decided_at=EVALUATED_AT + timedelta(seconds=1),
        )
        content = _roadmap_content()
        roadmap = record_roadmap_draft_in_session(
            session,
            inputs=RecordRoadmapDraftInput(
                project_id=project_id,
                backlog_artifact_id=backlog_id,
                backlog_artifact_fingerprint=backlog_fingerprint,
                canonical_content=content,
                content_fingerprint=canonical_hash(content),
                supersedes_roadmap_artifact_id=None,
                actor="operator@example.com",
                recorded_at=EVALUATED_AT + timedelta(seconds=2),
            ),
        )
        session.commit()
        return project_id, int(roadmap.roadmap_artifact_id or 0)


def test_backlog_and_roadmap_reviews_render_exact_pinned_specification_evidence(
    engine: Engine,
) -> None:
    """Review packets expose canonical candidates with only their cited evidence."""
    from services.agent_workbench.backlog_phase import (  # noqa: PLC0415
        record_backlog_decision_in_session,
    )
    from services.agent_workbench.roadmap_phase import (  # noqa: PLC0415
        RecordRoadmapDraftInput,
        record_roadmap_draft_in_session,
    )
    from tests.workflow.test_planning_transitions import (  # noqa: PLC0415
        _roadmap_content,
    )
    from tests.workflow.test_vision_backlog_transitions import (  # noqa: PLC0415
        EVALUATED_AT,
    )

    project_id, backlog_id, backlog_fingerprint, spec_version_id = _seed_task_7_backlog(
        engine
    )
    reads = DurableReadProjectionService(engine=engine)
    backlog_data = _data(
        reads.backlog_review(
            project_id=project_id,
            backlog_artifact_id=backlog_id,
        )
    )
    candidate = _json_object(backlog_data["candidate"])
    lineage_data = _json_object(backlog_data["lineage"])
    specification_lineage = _json_object(lineage_data["specification"])
    items = candidate["backlog_items"]
    assert isinstance(items, list)
    backlog_item = _json_object(items[0])
    assert backlog_data == {
        "schema_version": "agileforge.planning-artifact-review.v1",
        "phase": "backlog",
        "project_id": project_id,
        "lineage": {
            "specification": {
                "spec_version_id": spec_version_id,
                "spec_hash": specification_lineage["spec_hash"],
                "status": "approved",
            },
            "product_goal": lineage_data["product_goal"],
        },
        "candidate": candidate,
        "review": {"state": "pending"},
    }
    assert candidate["backlog_artifact_id"] == backlog_id
    assert candidate["artifact_fingerprint"] == backlog_fingerprint
    assert candidate["version_number"] == 1
    assert candidate["supersedes_backlog_artifact_id"] is None
    assert candidate["created_by"] == "operator@example.com"
    assert candidate["created_at"] == EVALUATED_AT.replace(tzinfo=None).isoformat()
    assert candidate["is_complete"] is True
    assert candidate["clarifying_questions"] == []
    assert "spec_item_ids" not in backlog_item
    assert backlog_item["backlog_item_id"] == "PBI-000001"
    assert backlog_item["specification_evidence"] == [
        {
            "spec_item_id": "GOAL.delivery",
            "title": "Immutable delivery",
            "statement": "Planning review uses immutable artifacts.",
            "level": None,
            "acceptance_criteria": [],
            "verification_method": None,
        },
        {
            "spec_item_id": "REQ.delivery",
            "title": "Exact planning lineage",
            "statement": "Persist exact accepted Specification lineage.",
            "level": "MUST",
            "acceptance_criteria": [
                "The persisted artifact retains exact parent identities."
            ],
            "verification_method": "acceptance-test",
        },
    ]

    with Session(engine) as session:
        backlog = session.get(BacklogArtifact, backlog_id)
        assert backlog is not None
        record_backlog_decision_in_session(
            session,
            artifact=backlog,
            decision="accepted",
            rationale="Accept exact Backlog.",
            reviewer="operator@example.com",
            idempotency_key="projection-accept-backlog",
            decided_at=EVALUATED_AT + timedelta(seconds=1),
        )
        roadmap_content = _roadmap_content()
        roadmap = record_roadmap_draft_in_session(
            session,
            inputs=RecordRoadmapDraftInput(
                project_id=project_id,
                backlog_artifact_id=backlog_id,
                backlog_artifact_fingerprint=backlog_fingerprint,
                canonical_content=roadmap_content,
                content_fingerprint=canonical_hash(roadmap_content),
                supersedes_roadmap_artifact_id=None,
                actor="operator@example.com",
                recorded_at=EVALUATED_AT + timedelta(seconds=2),
            ),
        )
        session.commit()
        roadmap_id = int(roadmap.roadmap_artifact_id or 0)

    terminal_backlog_data = _data(
        reads.backlog_review(
            project_id=project_id,
            backlog_artifact_id=backlog_id,
        )
    )
    assert terminal_backlog_data == {
        **backlog_data,
        "review": {
            "state": "accepted",
            "rationale": "Accept exact Backlog.",
            "reviewer": "operator@example.com",
            "decided_at": (
                EVALUATED_AT.replace(tzinfo=None) + timedelta(seconds=1)
            ).isoformat(),
        },
    }

    roadmap_data = _data(
        reads.roadmap_review(
            project_id=project_id,
            roadmap_artifact_id=roadmap_id,
        )
    )
    roadmap_candidate = _json_object(roadmap_data["candidate"])
    releases = roadmap_candidate["roadmap_releases"]
    assert isinstance(releases, list)
    release = _json_object(releases[0])
    resolved_items = release["backlog_items"]
    assert isinstance(resolved_items, list)
    assert resolved_items == [backlog_item]
    assert "backlog_item_ids" not in release
    assert roadmap_data == {
        "schema_version": "agileforge.planning-artifact-review.v1",
        "phase": "roadmap",
        "project_id": project_id,
        "lineage": {
            "specification": specification_lineage,
            "product_goal": lineage_data["product_goal"],
            "backlog": {
                "backlog_artifact_id": backlog_id,
                "backlog_artifact_fingerprint": backlog_fingerprint,
            },
        },
        "candidate": {
            "roadmap_artifact_id": roadmap_id,
            "artifact_fingerprint": canonical_hash(roadmap_content),
            "version_number": 1,
            "supersedes_roadmap_artifact_id": None,
            "created_by": "operator@example.com",
            "created_at": (
                EVALUATED_AT.replace(tzinfo=None) + timedelta(seconds=2)
            ).isoformat(),
            "roadmap_releases": [release],
            "roadmap_summary": roadmap_content["roadmap_summary"],
            "is_complete": True,
            "clarifying_questions": [],
        },
        "review": {"state": "pending"},
    }


def test_backlog_review_uses_historical_superseded_specification(
    engine: Engine,
) -> None:
    """Historical review never substitutes a newer current Specification."""
    from tests.workflow.lifecycle_fixtures import (  # noqa: PLC0415
        seed_accepted_specification,
    )
    from tests.workflow.test_vision_backlog_transitions import (  # noqa: PLC0415
        EVALUATED_AT,
        _specification_content,
    )

    project_id, backlog_id, _fingerprint, _spec_version_id = _seed_task_7_backlog(
        engine
    )
    with Session(engine) as session:
        seed_accepted_specification(
            session,
            project_id=project_id,
            content=_specification_content("A newer planning contract."),
            recorded_at=EVALUATED_AT + timedelta(minutes=1),
        )
        session.commit()

    data = _data(
        DurableReadProjectionService(engine=engine).backlog_review(
            project_id=project_id,
            backlog_artifact_id=backlog_id,
        )
    )

    assert (
        _json_object(_json_object(data["lineage"])["specification"])["status"]
        == "superseded"
    )
    candidate = _json_object(data["candidate"])
    items = candidate["backlog_items"]
    assert isinstance(items, list)
    evidence = _json_object(items[0])["specification_evidence"]
    assert isinstance(evidence, list)
    assert [_json_object(item)["spec_item_id"] for item in evidence] == [
        "GOAL.delivery",
        "REQ.delivery",
    ]


@pytest.mark.parametrize("decision", [None, "accepted", "feedback", "rejected"])
def test_story_review_renders_exact_candidate_and_only_cited_evidence(
    engine: Engine,
    decision: str | None,
) -> None:
    """Story review resolves immutable item content through its pinned lineage."""
    from services.agent_workbench.roadmap_phase import (  # noqa: PLC0415
        RecordRoadmapDecisionInput,
        record_roadmap_decision_in_session,
    )
    from services.agent_workbench.story_phase import (  # noqa: PLC0415
        RecordStoryDecisionInput,
        RecordStoryDraftInput,
        record_story_decision_in_session,
        record_story_draft_in_session,
    )
    from tests.workflow.test_planning_transitions import (  # noqa: PLC0415
        _story_content,
    )
    from tests.workflow.test_vision_backlog_transitions import (  # noqa: PLC0415
        EVALUATED_AT,
    )

    project_id, roadmap_id = _seed_task_7_roadmap(engine)
    with Session(engine) as session:
        roadmap = session.get(RoadmapArtifact, roadmap_id)
        assert roadmap is not None
        record_roadmap_decision_in_session(
            session,
            inputs=RecordRoadmapDecisionInput(
                artifact=roadmap,
                decision="accepted",
                rationale="Accept Roadmap for Story review.",
                reviewer="operator@example.com",
                idempotency_key="projection-story-roadmap",
                decided_at=EVALUATED_AT + timedelta(seconds=3),
            ),
        )
        backlog = session.get(BacklogArtifact, roadmap.backlog_artifact_id)
        assert backlog is not None
        content = _story_content(spec_item_id="REQ.delivery")
        story = record_story_draft_in_session(
            session,
            inputs=RecordStoryDraftInput(
                project_id=project_id,
                source_backlog_artifact_id=int(backlog.backlog_artifact_id or 0),
                source_backlog_artifact_fingerprint=backlog.content_fingerprint,
                backlog_item_id="PBI-000001",
                roadmap_artifact_id=roadmap_id,
                roadmap_artifact_fingerprint=roadmap.content_fingerprint,
                canonical_content=content,
                content_fingerprint=canonical_hash(content),
                supersedes_story_artifact_id=None,
                actor="operator@example.com",
                recorded_at=EVALUATED_AT + timedelta(seconds=4),
            ),
        )
        if decision is not None:
            record_story_decision_in_session(
                session,
                inputs=RecordStoryDecisionInput(
                    artifact=story,
                    decision=decision,
                    rationale=f"Story {decision} rationale.",
                    reviewer="story-reviewer",
                    idempotency_key=f"projection-story-{decision}",
                    decided_at=EVALUATED_AT + timedelta(seconds=5),
                ),
            )
        session.commit()
        story_id = int(story.story_artifact_id or 0)

    result = DurableReadProjectionService(engine=engine).story_review(
        project_id=project_id,
        story_artifact_id=story_id,
    )
    data = _data(result)
    lineage = _json_object(data["lineage"])
    candidate = _json_object(data["candidate"])
    story_items = candidate["story_items"]
    assert isinstance(story_items, list)
    story_item = _json_object(story_items[0])

    assert data["schema_version"] == "agileforge.planning-artifact-review.v1"
    assert data["phase"] == "story"
    assert data["project_id"] == project_id
    assert _json_object(lineage["backlog_item"])["backlog_item_id"] == "PBI-000001"
    assert _json_object(lineage["roadmap"])["roadmap_artifact_id"] == roadmap_id
    assert candidate["story_artifact_id"] == story_id
    assert candidate["version_number"] == 1
    assert candidate["is_complete"] is True
    assert candidate["clarifying_questions"] == []
    assert data["review"] == (
        {"state": "pending"}
        if decision is None
        else {
            "state": decision,
            "rationale": f"Story {decision} rationale.",
            "reviewer": "story-reviewer",
            "decided_at": (
                EVALUATED_AT.replace(tzinfo=None) + timedelta(seconds=5)
            ).isoformat(),
        }
    )
    assert story_item == {
        "story_item_id": "US-0001",
        "story_title": "Story for Plan immutable work",
        "statement": (
            "As an operator, I want durable planning facts, so that routing "
            "survives restarts."
        ),
        "persona": "operator",
        "acceptance_criteria": ["Verify that planning survives restart."],
        "invest_score": "High",
        "estimated_effort": "M",
        "produced_artifacts": ["planning records"],
        "research_caveats": [],
        "decomposition_warning": None,
        "dependency_candidates": [],
        "specification_evidence": [
            {
                "spec_item_id": "REQ.delivery",
                "title": "Exact planning lineage",
                "statement": "Persist exact accepted Specification lineage.",
                "level": "MUST",
                "acceptance_criteria": [
                    "The persisted artifact retains exact parent identities."
                ],
                "verification_method": "acceptance-test",
            }
        ],
    }
    assert "spec_item_ids" not in story_item
    assert "story_id" not in candidate


def test_story_review_returns_no_partial_candidate_for_corrupt_item_ids(
    engine: Engine,
) -> None:
    """Reject exact Story item-list drift with only typed identity context."""
    from services.agent_workbench.roadmap_phase import (  # noqa: PLC0415
        RecordRoadmapDecisionInput,
        record_roadmap_decision_in_session,
    )
    from services.agent_workbench.story_phase import (  # noqa: PLC0415
        RecordStoryDraftInput,
        record_story_draft_in_session,
    )
    from tests.workflow.test_planning_transitions import (  # noqa: PLC0415
        _story_content,
    )
    from tests.workflow.test_vision_backlog_transitions import (  # noqa: PLC0415
        EVALUATED_AT,
    )

    project_id, roadmap_id = _seed_task_7_roadmap(engine)
    with Session(engine) as session:
        roadmap = session.get(RoadmapArtifact, roadmap_id)
        assert roadmap is not None
        record_roadmap_decision_in_session(
            session,
            inputs=RecordRoadmapDecisionInput(
                artifact=roadmap,
                decision="accepted",
                rationale="Accept Roadmap for corrupt Story read.",
                reviewer="operator@example.com",
                idempotency_key="projection-corrupt-story-roadmap",
                decided_at=EVALUATED_AT + timedelta(seconds=3),
            ),
        )
        backlog = session.get(BacklogArtifact, roadmap.backlog_artifact_id)
        assert backlog is not None
        content = _story_content(spec_item_id="REQ.delivery")
        story = record_story_draft_in_session(
            session,
            inputs=RecordStoryDraftInput(
                project_id=project_id,
                source_backlog_artifact_id=int(backlog.backlog_artifact_id or 0),
                source_backlog_artifact_fingerprint=backlog.content_fingerprint,
                backlog_item_id="PBI-000001",
                roadmap_artifact_id=roadmap_id,
                roadmap_artifact_fingerprint=roadmap.content_fingerprint,
                canonical_content=content,
                content_fingerprint=canonical_hash(content),
                supersedes_story_artifact_id=None,
                actor="operator@example.com",
                recorded_at=EVALUATED_AT + timedelta(seconds=4),
            ),
        )
        session.flush()
        story_id = int(story.story_artifact_id or 0)
        story.story_item_ids_json = canonical_json(["US-9999"])
        session.add(story)
        session.commit()

    result = DurableReadProjectionService(engine=engine).story_review(
        project_id=project_id,
        story_artifact_id=story_id,
    )

    assert _error_code(result) == "PLANNING_ARTIFACT_CONTENT_INVALID"
    assert result["data"] == {
        "project_id": project_id,
        "story_artifact_id": story_id,
    }


def test_backlog_review_returns_typed_error_for_corrupt_canonical_content(
    engine: Engine,
) -> None:
    """A read returns no partial candidate when exact artifact bytes are corrupt."""
    project_id, backlog_id, _fingerprint, _spec_version_id = _seed_task_7_backlog(
        engine
    )
    with Session(engine) as session:
        backlog = session.get(BacklogArtifact, backlog_id)
        assert backlog is not None
        backlog.canonical_content_json = "{}"
        session.add(backlog)
        session.commit()

    result = DurableReadProjectionService(engine=engine).backlog_review(
        project_id=project_id,
        backlog_artifact_id=backlog_id,
    )

    assert _error_code(result) == "PLANNING_ARTIFACT_CONTENT_INVALID"
    assert result["data"] == {
        "project_id": project_id,
        "backlog_artifact_id": backlog_id,
    }


@pytest.mark.parametrize("artifact_kind", ["backlog", "roadmap"])
@pytest.mark.parametrize("corruption", ["formatting", "is_complete_int"])
def test_planning_review_rejects_stored_canonical_content_corruption(
    engine: Engine,
    artifact_kind: str,
    corruption: str,
) -> None:
    """Exact review returns no partial candidate and never rewrites stored bytes."""
    if artifact_kind == "backlog":
        project_id, artifact_id, _fingerprint, _spec_version_id = _seed_task_7_backlog(
            engine
        )
        model = BacklogArtifact
        id_field = "backlog_artifact_id"
    else:
        project_id, artifact_id = _seed_task_7_roadmap(engine)
        model = RoadmapArtifact
        id_field = "roadmap_artifact_id"

    with Session(engine) as session:
        artifact = session.get(model, artifact_id)
        assert artifact is not None
        content = json.loads(artifact.canonical_content_json)
        if corruption == "formatting":
            corrupted = json.dumps(content, indent=2, sort_keys=True)
            assert corrupted != artifact.canonical_content_json
            assert canonical_hash(content) == artifact.content_fingerprint
        else:
            content["is_complete"] = 1
            corrupted = canonical_json(content)
            assert corrupted != artifact.canonical_content_json
            artifact.content_fingerprint = canonical_hash(content)
        artifact.canonical_content_json = corrupted
        session.add(artifact)
        session.commit()

    reads = DurableReadProjectionService(engine=engine)
    if artifact_kind == "backlog":
        result = reads.backlog_review(
            project_id=project_id,
            backlog_artifact_id=artifact_id,
        )
    else:
        result = reads.roadmap_review(
            project_id=project_id,
            roadmap_artifact_id=artifact_id,
        )

    assert _error_code(result) == "PLANNING_ARTIFACT_CONTENT_INVALID"
    assert result["data"] == {
        "project_id": project_id,
        id_field: artifact_id,
    }
    with Session(engine) as session:
        stored = session.get(model, artifact_id)
        assert stored is not None
        assert stored.canonical_content_json == corrupted


@pytest.mark.parametrize(
    "corruption",
    [
        "formatting",
        "is_complete_int",
        "priority_bool",
        "backlog_item_id_bool",
        "incomplete",
        "empty",
        "clarifying_question",
        "skipped_backlog_item_id",
        "duplicate_backlog_item_id",
        "unknown_spec_item_id",
        "noncanonical_spec_item_ids",
    ],
)
def test_workflow_facts_reject_invalid_stored_backlog_content(
    engine: Engine,
    corruption: str,
) -> None:
    """Backlog fact loading returns no snapshot and never repairs invalid bytes."""
    from repositories.workflow import WorkflowFactLoadError  # noqa: PLC0415

    project_id, backlog_id, _fingerprint, _spec_version_id = _seed_task_7_backlog(
        engine
    )
    with Session(engine) as session:
        backlog = session.get(BacklogArtifact, backlog_id)
        assert backlog is not None
        original = backlog.canonical_content_json
        content = _JSON_OBJECT.validate_json(original)
        if corruption == "formatting":
            corrupted = json.dumps(content, indent=2, sort_keys=True)
            assert canonical_hash(content) == backlog.content_fingerprint
        else:
            _mutate_backlog_fact_content(content, corruption)
            corrupted = canonical_json(content)
            backlog.content_fingerprint = canonical_hash(content)
        assert corrupted != original
        corrupted_fingerprint = backlog.content_fingerprint
        backlog.canonical_content_json = corrupted
        session.add(backlog)
        session.commit()

    snapshot = None
    with Session(engine) as session:
        with pytest.raises(WorkflowFactLoadError):
            snapshot = WorkflowFactRepository(session).load(project_id)
        assert snapshot is None
        stored = session.get(BacklogArtifact, backlog_id)
        assert stored is not None
        assert stored.canonical_content_json == corrupted
        assert stored.content_fingerprint == corrupted_fingerprint


def _mutate_roadmap_fact_content(content: JsonObject, corruption: str) -> None:
    releases = content["roadmap_releases"]
    assert isinstance(releases, list)
    first_release = releases[0]
    assert isinstance(first_release, dict)
    if corruption == "is_complete_int":
        content["is_complete"] = 1
    elif corruption == "release_name_int":
        first_release["release_name"] = 7
    elif corruption == "backlog_item_id_bool":
        first_release["backlog_item_ids"] = [True]
    elif corruption == "incomplete":
        content["is_complete"] = False
    elif corruption == "empty":
        content["roadmap_releases"] = []
    elif corruption == "clarifying_question":
        content["clarifying_questions"] = ["Which PBI belongs in release one?"]
    elif corruption == "missing_backlog_item":
        first_release["backlog_item_ids"] = []
    elif corruption == "duplicate_backlog_item":
        first_release["backlog_item_ids"] = ["PBI-000001", "PBI-000001"]
    else:
        assert corruption == "unknown_backlog_item"
        first_release["backlog_item_ids"] = ["PBI-999999"]


@pytest.mark.parametrize(
    "corruption",
    [
        "formatting",
        "is_complete_int",
        "release_name_int",
        "backlog_item_id_bool",
        "incomplete",
        "empty",
        "clarifying_question",
        "missing_backlog_item",
        "duplicate_backlog_item",
        "unknown_backlog_item",
    ],
)
def test_workflow_facts_reject_invalid_stored_roadmap_content(
    engine: Engine,
    corruption: str,
) -> None:
    """Roadmap fact loading returns no snapshot and never repairs invalid bytes."""
    from repositories.workflow import WorkflowFactLoadError  # noqa: PLC0415

    project_id, roadmap_id = _seed_task_7_roadmap(engine)
    with Session(engine) as session:
        roadmap = session.get(RoadmapArtifact, roadmap_id)
        assert roadmap is not None
        original = roadmap.canonical_content_json
        content = _JSON_OBJECT.validate_json(original)
        if corruption == "formatting":
            corrupted = json.dumps(content, indent=2, sort_keys=True)
            assert canonical_hash(content) == roadmap.content_fingerprint
        else:
            _mutate_roadmap_fact_content(content, corruption)
            corrupted = canonical_json(content)
            roadmap.content_fingerprint = canonical_hash(content)
        assert corrupted != original
        corrupted_fingerprint = roadmap.content_fingerprint
        roadmap.canonical_content_json = corrupted
        session.add(roadmap)
        session.commit()

    snapshot = None
    with Session(engine) as session:
        with pytest.raises(WorkflowFactLoadError):
            snapshot = WorkflowFactRepository(session).load(project_id)
        assert snapshot is None
        stored = session.get(RoadmapArtifact, roadmap_id)
        assert stored is not None
        assert stored.canonical_content_json == corrupted
        assert stored.content_fingerprint == corrupted_fingerprint


@pytest.mark.parametrize(
    "backlog_item_ids",
    [[], ["PBI-unknown"], ["PBI-000001", "PBI-000001"]],
)
def test_roadmap_review_returns_typed_error_for_invalid_exact_coverage(
    engine: Engine,
    backlog_item_ids: list[JsonValue],
) -> None:
    """Invalid stored PBI coverage returns no partial Roadmap candidate."""
    project_id, roadmap_id = _seed_task_7_roadmap(engine)
    with Session(engine) as session:
        roadmap = session.get(RoadmapArtifact, roadmap_id)
        assert roadmap is not None
        content = _JSON_OBJECT.validate_json(roadmap.canonical_content_json)
        releases = content["roadmap_releases"]
        assert isinstance(releases, list)
        release = _json_object(releases[0])
        release["backlog_item_ids"] = list(backlog_item_ids)
        releases[0] = release
        roadmap.canonical_content_json = canonical_json(content)
        roadmap.content_fingerprint = canonical_hash(content)
        session.add(roadmap)
        session.commit()

    result = DurableReadProjectionService(engine=engine).roadmap_review(
        project_id=project_id,
        roadmap_artifact_id=roadmap_id,
    )

    assert _error_code(result) == "PLANNING_ARTIFACT_CONTENT_INVALID"
    assert result["data"] == {
        "project_id": project_id,
        "roadmap_artifact_id": roadmap_id,
    }


def test_backlog_review_deep_loads_whole_gold_and_renders_only_cited_evidence(  # noqa: PLR0915
    engine: Engine,
) -> None:
    """The 37-item gold contract survives deep load without bloating review data."""
    from services.agent_workbench.backlog_phase import (  # noqa: PLC0415
        record_backlog_draft_in_session,
    )
    from services.specs.accepted_specification import (  # noqa: PLC0415
        load_accepted_specification,
    )
    from tests.workflow.test_vision_backlog_transitions import (  # noqa: PLC0415
        EVALUATED_AT,
    )

    gold_path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "issue_210"
        / "gold"
        / "canonical-specification.json"
    )
    gold_json = gold_path.read_text(encoding="utf-8")
    gold = _JSON_OBJECT.validate_json(gold_json)
    gold_items = gold["items"]
    assert isinstance(gold_items, list)
    expected_ids = [_json_object(item)["id"] for item in gold_items]
    assert len(expected_ids) == GOLD_SPECIFICATION_ITEM_COUNT
    assert "DATA.001" in expected_ids
    assert "REQ.001" in expected_ids
    content: JsonObject = {
        "backlog_items": [
            {
                "backlog_item_id": "PBI-000001",
                "priority": 1,
                "requirement": "Implement the String Calculator public contract",
                "spec_item_ids": ["DATA.001", "REQ.001"],
                "value_driver": "Strategic",
                "justification": "Cites only the exact input and public API contract.",
                "estimated_effort": "M",
                "technical_note": None,
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }
    gold_payload = SpecificationPayload.model_validate(gold)
    seeded = _seed_lineage(
        engine,
        specification_payload_override=gold_payload,
    )
    project_id = _seeded_int(seeded, "project_id")
    candidate_id = _seeded_int(seeded, "candidate_id")
    candidate_fingerprint = seeded["candidate_fingerprint"]
    spec_hash = seeded["payload_fingerprint"]
    goal_id = _seeded_int(seeded, "goal_id")
    goal_fingerprint = seeded["goal_fingerprint"]
    vision_id = _seeded_int(seeded, "vision_id")
    vision_fingerprint = seeded["vision_fingerprint"]
    assert isinstance(candidate_fingerprint, str)
    assert isinstance(spec_hash, str)
    assert isinstance(goal_fingerprint, str)
    assert isinstance(vision_fingerprint, str)
    with Session(engine) as session:
        decision = SpecificationDecision(
            project_id=project_id,
            specification_candidate_id=candidate_id,
            candidate_fingerprint=candidate_fingerprint,
            decision="accepted",
            rationale="Accept the whole gold contract.",
            reviewer="operator@example.com",
            idempotency_key="accept-gold-specification",
            decided_at=EVALUATED_AT,
        )
        session.add(decision)
        session.flush()
        decision_id = decision.specification_decision_id
        assert decision_id is not None
        registry = SpecRegistry(
            project_id=project_id,
            spec_hash=spec_hash,
            status="approved",
            created_at=EVALUATED_AT,
            source_specification_decision_id=decision_id,
            source_specification_candidate_id=candidate_id,
            source_specification_candidate_fingerprint=candidate_fingerprint,
            source_vision_artifact_id=vision_id,
            source_vision_fingerprint=vision_fingerprint,
            source_product_goal_artifact_id=goal_id,
            source_product_goal_fingerprint=goal_fingerprint,
        )
        session.add(registry)
        session.flush()
        spec_version_id = registry.spec_version_id
        assert spec_version_id is not None
        backlog = record_backlog_draft_in_session(
            session,
            project_id=project_id,
            spec_version_id=spec_version_id,
            spec_hash=spec_hash,
            product_goal_artifact_id=goal_id,
            product_goal_fingerprint=goal_fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
            supersedes_backlog_artifact_id=None,
            artifact_id=101,
            actor="operator@example.com",
            recorded_at=EVALUATED_AT + timedelta(seconds=1),
        )
        loaded = load_accepted_specification(
            session,
            project_id=project_id,
            spec_version_id=spec_version_id,
            spec_hash=spec_hash,
        )
        session.commit()
        backlog_id = int(backlog.backlog_artifact_id or 0)

    assert loaded.canonical_specification_json == gold_json
    assert [item.id for item in loaded.payload.items] == expected_ids
    data = _data(
        DurableReadProjectionService(engine=engine).backlog_review(
            project_id=project_id,
            backlog_artifact_id=backlog_id,
        )
    )
    candidate = _json_object(data["candidate"])
    candidate_items = candidate["backlog_items"]
    assert isinstance(candidate_items, list)
    evidence = _json_object(candidate_items[0])["specification_evidence"]
    assert isinstance(evidence, list)
    assert [_json_object(item)["spec_item_id"] for item in evidence] == [
        "DATA.001",
        "REQ.001",
    ]
    source_items = {_json_object(item)["id"]: _json_object(item) for item in gold_items}
    for rendered in evidence:
        rendered_item = _json_object(rendered)
        source = source_items[rendered_item["spec_item_id"]]
        assert rendered_item == {
            "spec_item_id": source["id"],
            "title": source["title"],
            "statement": source["statement"],
            "level": source.get("level"),
            "acceptance_criteria": source.get("acceptance", []),
            "verification_method": source.get("verification"),
        }


def test_sprint_plan_review_is_durable_before_activation_and_after_drift(  # noqa: PLR0915
    engine: Engine,
) -> None:
    """Render pinned ordered Sprint evidence without operational draft rows."""
    from models.core import Sprint, Task, Team, UserStory  # noqa: PLC0415
    from models.workflow import StoryArtifact  # noqa: PLC0415
    from tests.workflow.test_planning_transitions import (  # noqa: PLC0415
        _domain,
        _guards,
        _record_and_accept_roadmap,
        _record_and_accept_story,
        _record_sprint_plan_draft,
        _seed_accepted_backlog,
    )
    from workflow.requests import DecideSprintPlan  # noqa: PLC0415

    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _artifact_id, story_id = _record_and_accept_story(
        engine,
        domain,
        project_id,
    )
    plan_id, _candidate, _plan, plan_fingerprint = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name="Review Projection Team",
        idempotency_key="review-projection-plan",
    )
    reads = DurableReadProjectionService(engine=engine)
    pending = reads.sprint_plan_review(
        project_id=project_id,
        sprint_plan_artifact_id=plan_id,
    )
    assert tuple(pending) == ("ok", "data", "warnings", "errors")
    assert pending["ok"] is True
    data = _json_object(pending["data"])
    assert data["schema_version"] == "agileforge.planning-artifact-review.v1"
    assert data["phase"] == "sprint_plan"
    candidate = _json_object(data["candidate"])
    selected = candidate["selected_stories"]
    assert isinstance(selected, list)
    selected_story = _json_object(selected[0])
    assert selected_story["story_id"] == story_id
    assert selected_story["specification_evidence"]
    tasks = selected_story["tasks"]
    assert isinstance(tasks, list)
    assert _json_object(tasks[0])["specification_evidence"]
    assert _json_object(data["review"])["state"] == "pending"
    with Session(engine) as session:
        assert session.exec(select(Team)).first() is None
        assert session.exec(select(Sprint)).first() is None
        assert session.exec(select(Task)).first() is None

    position = domain.position(project_id)
    accepted = domain.transition(
        DecideSprintPlan(
            **_guards(position, "planning.sprint.review"),
            idempotency_key="review-projection-accept",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="Pinned review is complete.",
        )
    )
    assert accepted.ok is True
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.title = "MUTATED OPERATIONAL TITLE"
        story.story_description = "MUTATED OPERATIONAL STATEMENT"
        story.persona = "mutated persona"
        story.acceptance_criteria_json = canonical_json(["Mutated criterion."])
        session.add(story)
        session.commit()
    terminal = reads.sprint_plan_review(
        project_id=project_id,
        sprint_plan_artifact_id=plan_id,
    )
    assert terminal["ok"] is True
    terminal_data = _json_object(terminal["data"])
    assert _json_object(terminal_data["review"])["state"] == "accepted"
    terminal_candidate = _json_object(terminal_data["candidate"])
    terminal_selected = terminal_candidate["selected_stories"]
    assert isinstance(terminal_selected, list)
    assert _json_object(terminal_selected[0]) == selected_story

    with Session(engine) as session:
        story_artifact_id = selected_story["story_artifact_id"]
        assert isinstance(story_artifact_id, (int, str))
        story_artifact = session.get(StoryArtifact, int(story_artifact_id))
        assert story_artifact is not None
        story_artifact.canonical_content_json += " "
        session.add(story_artifact)
        session.commit()
    corrupted = reads.sprint_plan_review(
        project_id=project_id,
        sprint_plan_artifact_id=plan_id,
    )
    assert corrupted["ok"] is False
    errors = corrupted["errors"]
    assert isinstance(errors, list)
    assert _json_object(errors[0])["code"] == "PLANNING_ARTIFACT_CONTENT_INVALID"


def test_pending_sprint_plan_review_reports_exact_source_stale_error(
    engine: Engine,
) -> None:
    """Recompute pending candidate identity and fail with the ruled read error."""
    from models.core import UserStory  # noqa: PLC0415
    from tests.workflow.test_planning_transitions import (  # noqa: PLC0415
        _domain,
        _record_and_accept_roadmap,
        _record_and_accept_story,
        _record_sprint_plan_draft,
        _seed_accepted_backlog,
    )

    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    plan_id, _candidate, _plan, _fingerprint = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name="Stale Projection Team",
        idempotency_key="stale-review-projection-plan",
    )
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.story_points = 8
        session.add(story)
        session.commit()
    result = DurableReadProjectionService(engine=engine).sprint_plan_review(
        project_id=project_id,
        sprint_plan_artifact_id=plan_id,
    )
    assert result["ok"] is False
    errors = result["errors"]
    assert isinstance(errors, list)
    assert _json_object(errors[0]) == {
        "code": "SPRINT_PLAN_REVIEW_SOURCE_STALE",
        "message": "Sprint plan review source changed. Draft a new Sprint plan.",
        "details": {
            "project_id": project_id,
            "sprint_plan_artifact_id": plan_id,
        },
    }

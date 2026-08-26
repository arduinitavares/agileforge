"""Tests for pure Sprint selection policy helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from sqlmodel import Session

from models.core import Project, UserStory
from models.product_definition import SpecificationCandidate, SpecificationDecision
from models.workflow import StoryArtifact, StoryArtifactDecision
from repositories.workflow import WorkflowFactRepository
from services import application
from services.contracts.backlog import BacklogItem, BacklogOutput
from services.contracts.story import (
    CanonicalStoryItem,
    InvestDimensionAssessment,
    StoryInvestAssessment,
    StoryItemEnvelope,
)
from services.specs.accepted_specification import (
    AcceptedSpecification,
    AcceptedSpecificationIntegrityError,
)
from services.specs.candidate_contract import (
    CandidateBuildInput,
    CandidateSourceKind,
    CandidateSourceManifestEntry,
    build_candidate_envelope,
    canonical_candidate_json,
    load_candidate_contract,
)
from services.sprint_selection import (
    SprintSelectionError,
    derive_group_slot,
    derive_parent_group,
    select_sprint_story_rows,
)
from services.story_sprint_selection import (
    StorySprintSelectionRequest,
    apply_story_sprint_selection_with_receipt_in_session,
    story_sprint_selection_fact_in_session,
)
from tests.test_story_validation_service import _accepted_story, _validate
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from tests.workflow.test_planning_transitions import (
    _apply_current_dependencies,
    _domain,
    _record_and_accept_roadmap,
    _record_and_accept_story,
    _replace_specification_and_backlog,
    _seed_accepted_backlog,
)
from utils.agileforge_spec_profile_v2 import SpecificationPayload
from utils.spec_schemas import StructuralValidationFailure, ValidationEvidence
from workflow.clock import FixedClock
from workflow.contracts import (
    Blocker,
    FactReference,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    WorkflowError,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.definitions.root import ROOT_GRAPH
from workflow.domain import WorkflowDomain
from workflow.facts import StoryFact
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

GOLD_HASH = "sha256:accepted"


def _invest_assessment() -> StoryInvestAssessment:
    return StoryInvestAssessment(
        independent=InvestDimensionAssessment(
            result="pass",
            rationale="Delivers self-contained increment.",
            evidence="No unbuilt dependencies.",
        ),
        negotiable=InvestDimensionAssessment(
            result="pass",
            rationale="Implementation details open to refinement.",
            evidence="Focuses on user outcome.",
        ),
        valuable=InvestDimensionAssessment(
            result="pass",
            rationale="Directly delivers user capability.",
            evidence="Addresses requirement.",
        ),
        estimable=InvestDimensionAssessment(
            result="pass",
            rationale="Scope is clear and bounded.",
            evidence="Discrete criteria.",
        ),
        small=InvestDimensionAssessment(
            result="pass",
            rationale="Sized for single iteration.",
            evidence="Effort is S.",
        ),
        testable=InvestDimensionAssessment(
            result="pass",
            rationale="Verifiable pass/fail criteria.",
            evidence="Observable verification steps.",
        ),
    )


EXPECTED_PARENT_GROUP = 10
EXPECTED_GROUP_SLOT = 2
EXPECTED_MANUAL_POINTS_USED = 3
DEPENDENT_STORY_ID = 85
GOLD_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "issue_210"
    / "gold"
    / "canonical-specification.json"
)
GOLD_HASH = "sha256:4f39ae394d3910bc52d73256eddc11edd66e57074025e1ec7f037e8e69a33025"
GOLD_ITEM_IDS = {
    "ASSUMPTION.001",
    "CONSTRAINT.001",
    "CONSTRAINT.002",
    "DATA.001",
    "DATA.002",
    "DECISION.001",
    "DECISION.002",
    "DECISION.003",
    "EXAMPLE.001",
    "GOAL.001",
    "GOAL.002",
    "INTERFACE.001",
    "INTERFACE.002",
    "NON_GOAL.001",
    "NON_GOAL.002",
    "NON_GOAL.003",
    "NON_GOAL.004",
    "OPEN_QUESTION.001",
    "QUALITY.001",
    "REQ.001",
    "REQ.002",
    "REQ.003",
    "REQ.004",
    "REQ.005",
    "REQ.006",
    "REQ.007",
    "REQ.008",
    "REQ.009",
    "REQ.010",
    "REQ.011",
    "REQ.012",
    "REQ.013",
    "REQ.014",
    "REQ.015",
    "RISK.001",
    "RISK.002",
    "RISK.003",
}


def _row(story_id: int, priority: int, points: int) -> dict[str, object]:
    return {
        "story_id": story_id,
        "story_title": f"Story {story_id}",
        "priority": priority,
        "story_points": points,
    }


def _select_story_for_sprint(engine: Engine, story_id: int) -> None:
    """Persist the explicit human intent required by candidate projections."""
    with Session(engine) as session:
        story = session.get_one(UserStory, story_id)
        current = story_sprint_selection_fact_in_session(session, story=story)
        apply_story_sprint_selection_with_receipt_in_session(
            session,
            StorySprintSelectionRequest(
                project_id=story.project_id,
                story_id=story_id,
                intent="select",
                expected_state_fingerprint=current.state_fingerprint,
                idempotency_key=f"select-story-{story_id}",
                actor="test-operator",
                correlation_id=None,
            ),
        )
        session.commit()


def test_derive_priority_group_metadata_from_rank_priority() -> None:
    """Verify rank-style priority values expose parent group and child slot."""
    assert derive_parent_group(101) == 1
    assert derive_group_slot(101) == 1
    assert derive_parent_group(1002) == EXPECTED_PARENT_GROUP
    assert derive_group_slot(1002) == EXPECTED_GROUP_SLOT


def test_sprint_selection_rejects_empty_human_scope() -> None:
    """Never invent a Sprint cohort when no durable candidate IDs were supplied."""
    rows = [_row(1, 101, 5), _row(2, 102, 1)]

    with pytest.raises(SprintSelectionError) as exc_info:
        select_sprint_story_rows(
            rows,
            max_story_points=10,
            selected_story_ids=[],
        )

    assert exc_info.value.code == "SPRINT_SELECTION_EMPTY"
    assert "exact durable candidate scope" in str(exc_info.value)


def test_manual_selection_preserves_explicit_story_order() -> None:
    """Verify manual mode preserves the selected_story_ids order."""
    rows = [_row(1, 101, 2), _row(2, 102, 3), _row(3, 201, 1)]

    result = select_sprint_story_rows(
        rows,
        max_story_points=3,
        selected_story_ids=[3, 1],
    )

    assert [row["story_id"] for row in result.selected_rows] == [3, 1]
    assert result.mode == "manual"
    assert result.story_points_used == EXPECTED_MANUAL_POINTS_USED


def test_manual_selection_blocks_explicit_capacity_overflow() -> None:
    """Verify manual mode enforces the explicit point capacity."""
    rows = [_row(1, 101, 5), _row(2, 102, 5)]

    with pytest.raises(SprintSelectionError) as exc_info:
        select_sprint_story_rows(
            rows,
            max_story_points=5,
            selected_story_ids=[1, 2],
        )

    assert exc_info.value.code == "SPRINT_SELECTION_CAPACITY_BLOCKED"
    assert exc_info.value.details["required_story_ids"] == [1, 2]
    assert exc_info.value.details["story_points"] == 10  # noqa: PLR2004
    assert exc_info.value.details["max_story_points"] == 5  # noqa: PLR2004


def test_manual_selection_raises_structured_error_for_missing_story_id() -> None:
    """Verify manual mode reports invalid selected_story_ids with details."""
    rows = [_row(1, 101, 2), _row(2, 102, 3)]

    with pytest.raises(SprintSelectionError) as exc_info:
        select_sprint_story_rows(
            rows,
            max_story_points=5,
            selected_story_ids=[2, 9],
        )

    assert exc_info.value.code == "SPRINT_SELECTION_INVALID"
    assert exc_info.value.details["invalid_selected_ids"] == [9]


def test_manual_selection_raises_structured_error_for_duplicate_story_id() -> None:
    """Verify manual mode reports duplicate selected_story_ids with details."""
    rows = [_row(1, 101, 2), _row(2, 102, 3)]

    with pytest.raises(SprintSelectionError) as exc_info:
        select_sprint_story_rows(
            rows,
            max_story_points=5,
            selected_story_ids=[1, 1],
        )

    assert exc_info.value.code == "SPRINT_SELECTION_DUPLICATE"
    assert exc_info.value.details["duplicate_selected_ids"] == [1]


def _dep_row(
    story_id: int,
    priority: int,
    points: int,
    *,
    blocked_by: list[object] | None = None,
) -> dict[str, object]:
    return {
        "story_id": story_id,
        "story_title": f"Story {story_id}",
        "priority": priority,
        "story_points": points,
        "blocked_by_story_ids": blocked_by or [],
        "prerequisite_story_ids": blocked_by or [],
        "dependency_status": "blocked" if blocked_by else "ready",
    }


def test_manual_selection_blocks_missing_prerequisite() -> None:
    """Verify manual mode blocks selected dependents with omitted prerequisites."""
    rows = [
        _dep_row(85, 101, 3, blocked_by=[66]),
        _dep_row(66, 201, 1),
    ]

    with pytest.raises(SprintSelectionError) as exc_info:
        select_sprint_story_rows(
            rows,
            max_story_points=4,
            selected_story_ids=[85],
        )

    assert exc_info.value.code == "SPRINT_SELECTION_DEPENDENCY_MISSING"
    assert exc_info.value.details["missing_prerequisite_story_ids"] == [66]
    assert exc_info.value.details["dependent_story_id"] == DEPENDENT_STORY_ID


def test_manual_selection_reorders_to_dependency_safe_order() -> None:
    """Verify manual mode reorders selected stories into dependency-safe order."""
    rows = [
        _dep_row(85, 101, 3, blocked_by=[66]),
        _dep_row(66, 201, 1),
    ]

    result = select_sprint_story_rows(
        rows,
        max_story_points=4,
        selected_story_ids=[85, 66],
    )

    assert result.mode == "manual"
    assert result.selected_story_ids == [66, 85]
    assert result.warnings == [
        {
            "code": "SPRINT_SELECTION_MANUAL_REORDERED",
            "message": "Manual Sprint selection was reordered to satisfy dependencies.",
            "requested_story_ids": [85, 66],
            "selected_story_ids": [66, 85],
        }
    ]


def _seed_gold_project(engine: Engine) -> tuple[int, int, str]:
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
    with Session(engine) as session:
        project = Project(name="Task 6 direct root")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        lineage = seed_accepted_specification(
            session,
            project_id=project.project_id,
            content='{"scope":"Task 6 fixture source"}',
            recorded_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        )
        assert lineage.spec.spec_version_id is not None
        candidate = session.get(
            SpecificationCandidate,
            lineage.spec.source_specification_candidate_id,
        )
        decision = session.get(
            SpecificationDecision,
            lineage.spec.source_specification_decision_id,
        )
        assert candidate is not None
        assert decision is not None
        _fixture_payload, fixture_envelope = load_candidate_contract(
            candidate.canonical_envelope_json,
            expected_candidate_fingerprint=candidate.candidate_fingerprint,
        )
        gold_payload = SpecificationPayload.model_validate_json(
            GOLD_PATH.read_text(encoding="utf-8")
        )
        gold_envelope = build_candidate_envelope(
            payload=gold_payload,
            metadata=CandidateBuildInput(
                candidate_kind=fixture_envelope.candidate_kind,
                accepted_vision_id=fixture_envelope.accepted_vision_id,
                accepted_vision_fingerprint=(
                    fixture_envelope.accepted_vision_fingerprint
                ),
                accepted_product_goal_id=(fixture_envelope.accepted_product_goal_id),
                accepted_product_goal_fingerprint=(
                    fixture_envelope.accepted_product_goal_fingerprint
                ),
                registered_source_fingerprint=(
                    fixture_envelope.registered_source_fingerprint
                ),
                source_producer_capability=(
                    fixture_envelope.source_producer_capability
                ),
                source_preparation_capability=(
                    fixture_envelope.source_preparation_capability
                ),
                source_manifest=(
                    *fixture_envelope.source_manifest,
                    CandidateSourceManifestEntry(
                        source_id="SRC.specification-source.context",
                        kind=CandidateSourceKind.REPOSITORY,
                        fingerprint=(
                            "sha256:7f3d98698f2741a3a200a7558c98ee0415bbd670c8184c406cc854db44de64d7"
                        ),
                    ),
                ),
                accepted_fact_fingerprint=(fixture_envelope.accepted_fact_fingerprint),
                producer_input_fingerprint=(
                    fixture_envelope.producer_input_fingerprint
                ),
                producer_capability=fixture_envelope.producer_capability,
                producer_version=fixture_envelope.producer_version,
                model_id=fixture_envelope.model_id,
                model_configuration_fingerprint=(
                    fixture_envelope.model_configuration_fingerprint
                ),
                prompt_version=fixture_envelope.prompt_version,
                prompt_fingerprint=fixture_envelope.prompt_fingerprint,
                workflow_node_attempt_id=(fixture_envelope.workflow_node_attempt_id),
                attempt_fingerprint=fixture_envelope.attempt_fingerprint,
                correlation_id=fixture_envelope.correlation_id,
                produced_at=fixture_envelope.produced_at,
            ),
        )
        candidate.canonical_envelope_json = canonical_candidate_json(
            gold_payload,
            gold_envelope,
        )
        candidate.payload_fingerprint = gold_envelope.payload_fingerprint
        candidate.source_manifest_fingerprint = (
            gold_envelope.source_manifest_fingerprint
        )
        candidate.rendered_view_fingerprint = gold_envelope.review_view_fingerprint
        candidate.candidate_fingerprint = gold_envelope.candidate_fingerprint
        decision.candidate_fingerprint = gold_envelope.candidate_fingerprint
        lineage.spec.spec_hash = gold_envelope.payload_fingerprint
        lineage.spec.source_specification_candidate_fingerprint = (
            gold_envelope.candidate_fingerprint
        )
        session.add(candidate)
        session.add(decision)
        session.add(lineage.spec)
        session.commit()
        result = (
            project.project_id,
            lineage.spec.spec_version_id,
            lineage.spec.spec_hash,
        )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    return result


def _backlog_decision(engine: Engine, project_id: int) -> NodeDecision:
    domain = WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=datetime(2026, 8, 21, 13, tzinfo=UTC)),
    )
    return next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == "backlog.generate"
    )


def test_delivery_application_deep_loads_complete_gold_once(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Load and propagate the sole complete accepted Specification root once."""
    project_id, spec_version_id, spec_hash = _seed_gold_project(engine)
    decision = _backlog_decision(engine, project_id)
    calls: list[tuple[int, int, str]] = []
    real_loader = application.require_current_accepted_specification

    def observed_loader(
        session: Session,
        *,
        project_id: int,
        spec_version_id: int,
        spec_hash: str,
    ) -> object:
        calls.append((project_id, spec_version_id, spec_hash))
        return real_loader(
            session,
            project_id=project_id,
            spec_version_id=spec_version_id,
            spec_hash=spec_hash,
        )

    monkeypatch.setattr(
        application,
        "require_current_accepted_specification",
        observed_loader,
    )
    built = application.DeliveryActionInputService(engine=engine).build(
        project_id=project_id,
        decision=decision,
        node_id="backlog.generate",
    )

    assert built is not None
    assert not isinstance(built, WorkflowError)
    assert calls == [(project_id, spec_version_id, spec_hash)]
    builder_input = built["builder_input"]
    assert isinstance(builder_input, dict)
    assert builder_input["accepted_specification_hash"] == GOLD_HASH
    assert builder_input["accepted_specification_json"] == GOLD_PATH.read_text(
        encoding="utf-8"
    )
    parsed = SpecificationPayload.model_validate_json(
        str(builder_input["accepted_specification_json"])
    )
    assert {item.id for item in parsed.items} == GOLD_ITEM_IDS
    assert "DATA.001" in GOLD_ITEM_IDS
    assert "technical_spec" not in builder_input
    assert "compiled_authority" not in builder_input


@pytest.mark.parametrize(
    "mismatch",
    ["foreign-id", "wrong-hash"],
)
def test_delivery_application_rejects_mismatched_specification_before_execution(
    engine: Engine,
    mismatch: str,
) -> None:
    """Reject a foreign or corrupt Specification graph reference before execution."""
    project_id, spec_version_id, spec_hash = _seed_gold_project(engine)
    decision = _backlog_decision(engine, project_id)
    fact_id = (
        str(spec_version_id + 1_000_000)
        if mismatch == "foreign-id"
        else str(spec_version_id)
    )
    fingerprint = spec_hash if mismatch == "foreign-id" else "sha256:" + "0" * 64
    references = tuple(
        FactReference(
            fact_type=item.fact_type,
            fact_id=fact_id if item.fact_type == "specification" else item.fact_id,
            fingerprint=(
                fingerprint if item.fact_type == "specification" else item.fingerprint
            ),
        )
        for item in decision.fact_references
    )
    built = application.DeliveryActionInputService(engine=engine).build(
        project_id=project_id,
        decision=decision.model_copy(update={"fact_references": references}),
        node_id="backlog.generate",
    )

    assert isinstance(built, WorkflowError)
    assert built.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert built.blockers == (
        Blocker(
            code="SPECIFICATION_NOT_FOUND",
            message=(
                "Exact accepted Specification identity was not found in this Project."
            ),
        ),
    )


def test_sprint_projection_uses_exact_story_item_and_spec_evidence() -> None:
    """Project only exact accepted Story item and Specification identities."""
    story = UserStory(
        story_id=42,
        project_id=1,
        source_story_artifact_id=31,
        source_story_artifact_fingerprint="sha256:story-artifact",
        source_story_item_id="US-0001",
        source_story_item_fingerprint="sha256:story-item",
        accepted_spec_version_id=11,
        accepted_spec_hash=GOLD_HASH,
        spec_item_ids_json='["DATA.001","REQ.001"]',
        title="Implement the accepted operation",
        story_description=(
            "As a calculator user, I want the accepted operation, so that I can "
            "obtain the specified result."
        ),
        acceptance_criteria_json='["Verify the result against DATA.001."]',
        persona="calculator user",
        story_points=3,
        rank="101",
    )
    candidate = StoryFact(
        story_id=42,
        source_story_artifact_id=31,
        source_story_artifact_fingerprint="sha256:story-artifact",
        source_story_item_id="US-0001",
        source_story_item_fingerprint="sha256:story-item",
        accepted_spec_version_id=11,
        accepted_spec_hash=GOLD_HASH,
        spec_item_ids=("DATA.001", "REQ.001"),
        content_accepted=True,
        status="ready",
        story_points=3,
        rank="101",
        structurally_eligible=True,
        structural_eligibility_status="eligible",
        sprint_selection_state="selected",
        sprint_selection_state_fingerprint="sha256:selected",
        selected_scope_fingerprint="sha256:" + ("6" * 64),
        dependency_safe=True,
        sprint_candidate=True,
        readiness_blockers=(),
    )

    canonical_item = CanonicalStoryItem(
        story_item_id="US-0001",
        story_title="Implement the accepted operation",
        statement=story.story_description,
        persona="calculator user",
        acceptance_criteria=("Verify the result against DATA.001.",),
        spec_item_ids=("DATA.001", "REQ.001"),
        invest_assessment=_invest_assessment(),
        estimated_effort="M",
        effort_rationale="Moderate complexity operation.",
        order_rationale="First priority calculation.",
        produced_artifacts=(),
        research_caveats=(),
        dependency_candidates=(),
    )

    projected = application._sprint_planner_story(story, candidate, canonical_item)

    assert projected.story_item_id == "US-0001"
    assert projected.acceptance_criteria == ("Verify the result against DATA.001.",)
    assert projected.spec_item_ids == ("DATA.001", "REQ.001")
    dumped = projected.model_dump(mode="json")
    assert "source_requirement" not in dumped
    assert "evaluated_invariant_ids" not in dumped
    assert "story_compliance_boundary_summaries" not in dumped


def test_story_input_builds_feedback_context_from_host_canonical_prior_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the persisted host Story envelope, never the provider output shape."""
    backlog_item = BacklogItem(
        backlog_item_id="PBI-000001",
        priority=1,
        requirement="Preserve the accepted specification evidence.",
        spec_item_ids=("DATA.001",),
        value_driver="Strategic",
        justification="The prior Story needs exact evidence.",
        estimated_effort="S",
    )
    canonical_item = CanonicalStoryItem(
        story_item_id="US-0001",
        story_title="Preserve Story feedback context",
        statement=(
            "As a reviewer, I want prior Story feedback retained, so that the "
            "successor addresses it."
        ),
        persona="reviewer",
        acceptance_criteria=("The next Story input includes the reviewed artifact.",),
        spec_item_ids=("DATA.001",),
        invest_assessment=_invest_assessment(),
        estimated_effort="S",
        effort_rationale="Straightforward feedback retention.",
        order_rationale="Initial story in backlog chain.",
        produced_artifacts=(),
        research_caveats=(),
        dependency_candidates=(),
    )
    item_envelope = StoryItemEnvelope(
        item=canonical_item,
        item_fingerprint=canonical_hash(canonical_item.model_dump(mode="json")),
    )
    canonical_content = {
        "story_items": [item_envelope.model_dump(mode="json")],
        "is_complete": True,
        "clarifying_questions": [],
    }
    content_fingerprint = canonical_hash(canonical_content)
    prior = StoryArtifact(
        story_artifact_id=7,
        project_id=3,
        source_backlog_artifact_id=5,
        source_backlog_artifact_fingerprint="sha256:backlog",
        backlog_item_id=backlog_item.backlog_item_id,
        roadmap_artifact_id=6,
        roadmap_artifact_fingerprint="sha256:roadmap",
        version_number=1,
        canonical_content_json=canonical_json(canonical_content),
        content_fingerprint=content_fingerprint,
        story_item_ids_json='["US-0001"]',
        created_by="reviewer",
        created_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )
    decision = SimpleNamespace(
        instance_key="backlog_item:PBI-000001",
        fact_references=(
            FactReference(
                fact_type="backlog_item",
                fact_id=backlog_item.backlog_item_id,
                fingerprint=canonical_hash(backlog_item.model_dump(mode="json")),
            ),
            FactReference(
                fact_type="story",
                fact_id="7",
                fingerprint=content_fingerprint,
            ),
        ),
    )
    lineage = SimpleNamespace(
        accepted_specification=SimpleNamespace(
            spec_version_id=1,
            spec_hash=GOLD_HASH,
            canonical_specification_json=GOLD_PATH.read_text(encoding="utf-8"),
        )
    )
    session = Mock(spec=Session)
    session.get.return_value = prior
    session.exec.return_value.one_or_none.return_value = StoryArtifactDecision(
        project_id=3,
        story_artifact_id=7,
        artifact_fingerprint=content_fingerprint,
        decision="feedback",
        rationale="Address the prior feedback exactly.",
        reviewer="reviewer",
        idempotency_key="feedback-7",
        decided_at=datetime(2026, 8, 21, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(
        application,
        "_required_backlog",
        lambda _session, _decision, _lineage: (
            SimpleNamespace(
                project_id=3,
                backlog_artifact_id=5,
                content_fingerprint="sha256:backlog",
            ),
            BacklogOutput(backlog_items=(backlog_item,), is_complete=True),
        ),
    )
    monkeypatch.setattr(
        application,
        "_required_roadmap",
        lambda _session, _decision, _backlog: (
            SimpleNamespace(
                roadmap_artifact_id=6,
                content_fingerprint="sha256:roadmap",
            ),
            SimpleNamespace(model_dump_json=lambda **_kwargs: "{}"),
        ),
    )

    built = application._story_input(
        session,
        cast("NodeDecision", decision),
        cast("application._DeliveryLineage", lineage),
    )

    assert built is not None
    writer_input = built["writer_input"]
    assert isinstance(writer_input, dict)
    assert writer_input["user_input"] == (
        "Previous reviewed Story artifact:\n"
        f"{prior.canonical_content_json}"
        "\nReview outcome: feedback"
        "\nReview rationale: Address the prior feedback exactly."
    )


@pytest.mark.parametrize(
    ("field_name", "tampered_value"),
    [
        ("title", "Tampered operational title"),
        (
            "story_description",
            "As a calculator user, I want a changed operation, so that drift persists.",
        ),
        ("persona", "tampered persona"),
        ("acceptance_criteria_json", '["Tampered acceptance criterion."]'),
        ("spec_item_ids_json", '["REQ.001"]'),
    ],
)
def test_sprint_projection_rejects_operational_story_drift_from_artifact(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    tampered_value: str,
) -> None:
    """Reject every immutable operational Story field that drifts from its item."""
    monkeypatch.setattr(
        application,
        "require_story_ready_for_sprint",
        lambda _session, *, story: story,
    )
    original_item = CanonicalStoryItem(
        story_item_id="US-0001",
        story_title="Implement the accepted operation",
        statement=(
            "As a calculator user, I want the accepted operation, so that I can "
            "obtain the specified result."
        ),
        persona="calculator user",
        acceptance_criteria=("Verify the result against DATA.001.",),
        spec_item_ids=("DATA.001", "REQ.001"),
        invest_assessment=_invest_assessment(),
        estimated_effort="M",
        effort_rationale="Moderate complexity operation.",
        order_rationale="First priority calculation.",
        produced_artifacts=(),
        research_caveats=(),
        dependency_candidates=(),
    )
    item_envelope = StoryItemEnvelope(
        item=original_item,
        item_fingerprint=canonical_hash(original_item.model_dump(mode="json")),
    )
    canonical_content = {
        "story_items": [item_envelope.model_dump(mode="json")],
        "is_complete": True,
        "clarifying_questions": [],
    }
    content_fingerprint = canonical_hash(canonical_content)
    artifact = StoryArtifact(
        story_artifact_id=31,
        project_id=1,
        source_backlog_artifact_id=21,
        source_backlog_artifact_fingerprint="sha256:backlog",
        backlog_item_id="PBI-000001",
        roadmap_artifact_id=22,
        roadmap_artifact_fingerprint="sha256:roadmap",
        version_number=1,
        canonical_content_json=canonical_json(canonical_content),
        content_fingerprint=content_fingerprint,
        story_item_ids_json='["US-0001"]',
        created_by="reviewer",
        created_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )
    operational_values = {
        "title": original_item.story_title,
        "story_description": original_item.statement,
        "acceptance_criteria_json": canonical_json(
            list(original_item.acceptance_criteria)
        ),
        "persona": original_item.persona,
        "spec_item_ids_json": canonical_json(list(original_item.spec_item_ids)),
    }
    story = UserStory(
        story_id=42,
        project_id=1,
        source_story_artifact_id=31,
        source_story_artifact_fingerprint=content_fingerprint,
        source_story_item_id="US-0001",
        source_story_item_fingerprint=item_envelope.item_fingerprint,
        accepted_spec_version_id=11,
        accepted_spec_hash=GOLD_HASH,
        story_points=3,
        rank="101",
        **{**operational_values, field_name: tampered_value},
    )
    candidate = StoryFact(
        story_id=42,
        source_story_artifact_id=31,
        source_story_artifact_fingerprint=content_fingerprint,
        source_story_item_id="US-0001",
        source_story_item_fingerprint=item_envelope.item_fingerprint,
        accepted_spec_version_id=11,
        accepted_spec_hash=GOLD_HASH,
        spec_item_ids=("DATA.001", "REQ.001"),
        content_accepted=True,
        status="ready",
        story_points=3,
        rank="101",
        structurally_eligible=True,
        structural_eligibility_status="eligible",
        sprint_selection_state="selected",
        sprint_selection_state_fingerprint="sha256:selected",
        selected_scope_fingerprint="sha256:" + ("6" * 64),
        dependency_safe=True,
        sprint_candidate=True,
        readiness_blockers=(),
    )
    session = Mock(spec=Session)
    results = [Mock(), Mock(), Mock()]
    results[0].all.return_value = [story]
    results[1].all.return_value = [artifact]
    results[2].all.return_value = [
        StoryArtifactDecision(
            project_id=1,
            story_artifact_id=31,
            artifact_fingerprint=content_fingerprint,
            decision="accepted",
            rationale="Accepted for Sprint planning.",
            reviewer="reviewer",
            idempotency_key="review-31",
            decided_at=datetime(2026, 8, 21, 13, tzinfo=UTC),
        )
    ]
    session.exec.side_effect = results

    with pytest.raises(ValueError, match="immutable Story item"):
        application._sprint_selection_rows(
            session,
            project_id=1,
            accepted_specification=cast(
                "AcceptedSpecification",
                SimpleNamespace(spec_version_id=11, spec_hash=GOLD_HASH),
            ),
            candidates=(candidate,),
            dependencies=(),
        )


@pytest.mark.parametrize("decision_value", [None, "feedback"])
def test_sprint_projection_requires_exact_accepted_story_decision(
    monkeypatch: pytest.MonkeyPatch,
    decision_value: str | None,
) -> None:
    """Only the exact accepted immutable Story artifact can enter planning."""
    monkeypatch.setattr(
        application,
        "require_story_ready_for_sprint",
        lambda _session, *, story: story,
    )
    canonical_item = CanonicalStoryItem(
        story_item_id="US-0001",
        story_title="Implement the accepted operation",
        statement=(
            "As a calculator user, I want the accepted operation, so that I can "
            "obtain the specified result."
        ),
        persona="calculator user",
        acceptance_criteria=("Verify the result against DATA.001.",),
        spec_item_ids=("DATA.001", "REQ.001"),
        invest_assessment=_invest_assessment(),
        estimated_effort="M",
        effort_rationale="Moderate complexity operation.",
        order_rationale="First priority calculation.",
        produced_artifacts=(),
        research_caveats=(),
        dependency_candidates=(),
    )
    item_envelope = StoryItemEnvelope(
        item=canonical_item,
        item_fingerprint=canonical_hash(canonical_item.model_dump(mode="json")),
    )
    canonical_content = {
        "story_items": [item_envelope.model_dump(mode="json")],
        "is_complete": True,
        "clarifying_questions": [],
    }
    content_fingerprint = canonical_hash(canonical_content)
    artifact = StoryArtifact(
        story_artifact_id=31,
        project_id=1,
        source_backlog_artifact_id=21,
        source_backlog_artifact_fingerprint="sha256:backlog",
        backlog_item_id="PBI-000001",
        roadmap_artifact_id=22,
        roadmap_artifact_fingerprint="sha256:roadmap",
        version_number=1,
        canonical_content_json=canonical_json(canonical_content),
        content_fingerprint=content_fingerprint,
        story_item_ids_json='["US-0001"]',
        created_by="reviewer",
        created_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )
    story = UserStory(
        story_id=42,
        project_id=1,
        source_story_artifact_id=31,
        source_story_artifact_fingerprint=content_fingerprint,
        source_story_item_id="US-0001",
        source_story_item_fingerprint=item_envelope.item_fingerprint,
        accepted_spec_version_id=11,
        accepted_spec_hash=GOLD_HASH,
        spec_item_ids_json='["DATA.001","REQ.001"]',
        title=canonical_item.story_title,
        story_description=canonical_item.statement,
        acceptance_criteria_json='["Verify the result against DATA.001."]',
        persona=canonical_item.persona,
        story_points=3,
        rank="101",
    )
    candidate = StoryFact(
        story_id=42,
        source_story_artifact_id=31,
        source_story_artifact_fingerprint=content_fingerprint,
        source_story_item_id="US-0001",
        source_story_item_fingerprint=item_envelope.item_fingerprint,
        accepted_spec_version_id=11,
        accepted_spec_hash=GOLD_HASH,
        spec_item_ids=("DATA.001", "REQ.001"),
        content_accepted=True,
        status="ready",
        story_points=3,
        rank="101",
        structurally_eligible=True,
        structural_eligibility_status="eligible",
        sprint_selection_state="selected",
        sprint_selection_state_fingerprint="sha256:selected",
        selected_scope_fingerprint="sha256:" + ("6" * 64),
        dependency_safe=True,
        sprint_candidate=True,
        readiness_blockers=(),
    )
    decision_rows = (
        []
        if decision_value is None
        else [
            StoryArtifactDecision(
                project_id=1,
                story_artifact_id=31,
                artifact_fingerprint=content_fingerprint,
                decision=decision_value,
                rationale="Review is not accepted.",
                reviewer="reviewer",
                idempotency_key="review-31",
                decided_at=datetime(2026, 8, 21, 13, tzinfo=UTC),
            )
        ]
    )
    session = Mock(spec=Session)
    results = [Mock(), Mock(), Mock()]
    results[0].all.return_value = [story]
    results[1].all.return_value = [artifact]
    results[2].all.return_value = decision_rows
    session.exec.side_effect = results

    with pytest.raises(ValueError, match="accepted Story decision"):
        application._sprint_selection_rows(
            session,
            project_id=1,
            accepted_specification=cast(
                "AcceptedSpecification",
                SimpleNamespace(spec_version_id=11, spec_hash=GOLD_HASH),
            ),
            candidates=(candidate,),
            dependencies=(),
        )


def test_sprint_projection_rejects_candidate_from_prior_specification_root() -> None:
    """Do not plan a candidate whose accepted Specification is no longer current."""
    story = UserStory(
        story_id=42,
        project_id=1,
        source_story_artifact_id=31,
        source_story_artifact_fingerprint="sha256:story-artifact",
        source_story_item_id="US-0001",
        source_story_item_fingerprint="sha256:story-item",
        accepted_spec_version_id=11,
        accepted_spec_hash=GOLD_HASH,
        spec_item_ids_json='["DATA.001","REQ.001"]',
        title="Implement the accepted operation",
        story_description=(
            "As a calculator user, I want the accepted operation, so that I can "
            "obtain the specified result."
        ),
        acceptance_criteria_json='["Verify the result against DATA.001."]',
        persona="calculator user",
        story_points=3,
        rank="101",
    )
    candidate = StoryFact(
        story_id=42,
        source_story_artifact_id=31,
        source_story_artifact_fingerprint="sha256:story-artifact",
        source_story_item_id="US-0001",
        source_story_item_fingerprint="sha256:story-item",
        accepted_spec_version_id=11,
        accepted_spec_hash=GOLD_HASH,
        spec_item_ids=("DATA.001", "REQ.001"),
        content_accepted=True,
        status="ready",
        story_points=3,
        rank="101",
        structurally_eligible=True,
        structural_eligibility_status="eligible",
        sprint_selection_state="selected",
        sprint_selection_state_fingerprint="sha256:selected",
        selected_scope_fingerprint="sha256:" + ("6" * 64),
        dependency_safe=True,
        sprint_candidate=True,
        readiness_blockers=(),
    )
    session = Mock(spec=Session)
    session.exec.return_value.all.return_value = [story]

    with pytest.raises(ValueError, match="current accepted Specification"):
        application._sprint_selection_rows(
            session,
            project_id=1,
            accepted_specification=cast(
                "AcceptedSpecification",
                SimpleNamespace(
                    spec_version_id=12,
                    spec_hash="sha256:new-current-specification",
                ),
            ),
            candidates=(candidate,),
            dependencies=(),
        )


def test_delivery_input_preserves_stale_specification_integrity_error(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return the loader's stale Specification error without provider execution."""
    message = "The requested Specification is no longer current."
    stale_code = "STALE_SPECIFICATION"

    def stale_lineage(_session: Session, **_kwargs: object) -> None:
        raise AcceptedSpecificationIntegrityError(stale_code, message)

    monkeypatch.setattr(application, "_delivery_lineage", stale_lineage)

    result = application.DeliveryActionInputService(engine=engine).build(
        project_id=3,
        decision=cast("NodeDecision", SimpleNamespace()),
        node_id="backlog.generate",
    )

    assert isinstance(result, WorkflowError)
    assert result.code is WorkflowErrorCode.STALE_SPECIFICATION
    assert result.blockers == (Blocker(code="STALE_SPECIFICATION", message=message),)


def test_sprint_input_preserves_stale_specification_integrity_error(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not translate stale Specification lineage to generic Sprint input errors."""
    message = "The requested Specification is no longer current."
    stale_code = "STALE_SPECIFICATION"

    def stale_lineage(_session: Session, **_kwargs: object) -> None:
        raise AcceptedSpecificationIntegrityError(stale_code, message)

    monkeypatch.setattr(application, "_delivery_lineage", stale_lineage)

    result = application.SprintPlanningInputService(engine=engine).build(
        project_id=3,
        decision=cast("NodeDecision", SimpleNamespace()),
        request=cast("application.SprintPlanningRequest", SimpleNamespace()),
    )

    assert isinstance(result, WorkflowError)
    assert result.code is WorkflowErrorCode.STALE_SPECIFICATION
    assert result.blockers == (Blocker(code="STALE_SPECIFICATION", message=message),)


def test_delivery_action_returns_stale_specification_error_before_agent_run() -> None:
    """Surface a valid stale input error without treating it as agent payload."""
    message = "The requested Specification is no longer current."
    error = WorkflowError(
        code=WorkflowErrorCode.STALE_SPECIFICATION,
        message=message,
        blockers=(Blocker(code="STALE_SPECIFICATION", message=message),),
    )
    decision = NodeDecision(
        node_id="backlog.generate",
        child_graph_id="backlog",
        request_kind="record_backlog_draft",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="BACKLOG_GENERATION_REQUIRED",
        decision_fingerprint="sha256:decision",
    )
    position = WorkflowPosition(
        project_id=3,
        graph_version="v2",
        fact_fingerprint="sha256:facts",
        evaluated_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        available_nodes=("backlog.generate",),
        waiting_nodes=(),
        blocked_nodes=(),
        invalid_nodes=(),
        terminal=False,
        decisions=(decision,),
    )
    run_agentic_action = Mock()
    app = cast(
        "application.AgileForgeApplication",
        SimpleNamespace(
            _delivery_action_input=SimpleNamespace(
                replay=lambda _query: None,
                build=lambda **_kwargs: error,
            ),
            position=lambda **_kwargs: position,
            run_agentic_action=run_agentic_action,
        ),
    )

    result = application.AgileForgeApplication._run_delivery_action(
        app,
        application.DeliveryActionRequest(
            project_id=3,
            idempotency_key="stale-delivery-input",
            actor="reviewer",
        ),
        node_id="backlog.generate",
    )

    assert result.ok is False
    assert result.error == error
    run_agentic_action.assert_not_called()


def test_story_fact_requires_exact_current_validation_evidence(engine: Engine) -> None:
    """Missing evidence adds one deterministic blocker to the graph Story fact."""
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        row = session.get(UserStory, story_id)
        assert row is not None
        row.validation_evidence = None
        session.add(row)
        session.commit()
        story = next(
            item
            for item in WorkflowFactRepository(session).load(row.project_id).stories
            if item.story_id == story_id
        )
        assert story.sprint_candidate is False
        assert story.readiness_blockers == ("STORY_VALIDATION_REQUIRED",)

    _validate(engine, story_id)
    with Session(engine) as session:
        row = session.get(UserStory, story_id)
        assert row is not None
        story = next(
            item
            for item in WorkflowFactRepository(session).load(row.project_id).stories
            if item.story_id == story_id
        )
        assert story.structurally_eligible is True
        assert story.sprint_selection_state == "unselected"
        assert story.sprint_candidate is False
        assert story.readiness_blockers == ()


def test_story_fact_rejects_candidate_without_dependency_safe_scope() -> None:
    """Reject a projection that skips the dependency-confirmation intersection."""
    payload = {
        "story_id": 42,
        "source_story_artifact_id": 31,
        "source_story_artifact_fingerprint": "sha256:" + ("1" * 64),
        "source_story_item_id": "US-0001",
        "source_story_item_fingerprint": "sha256:" + ("2" * 64),
        "accepted_spec_version_id": 11,
        "accepted_spec_hash": "sha256:" + ("3" * 64),
        "spec_item_ids": ("REQ.001",),
        "content_accepted": True,
        "status": "To Do",
        "story_points": 3,
        "rank": "101",
        "structurally_eligible": True,
        "structural_eligibility_status": "eligible",
        "sprint_selection_state": "selected",
        "sprint_selection_state_fingerprint": "sha256:" + ("4" * 64),
        "sprint_selection_event_id": 7,
        "sprint_selection_event_fingerprint": "sha256:" + ("5" * 64),
        "selected_scope_fingerprint": "sha256:" + ("6" * 64),
        "readiness_blockers": (),
    }

    valid = StoryFact.model_validate(
        {**payload, "dependency_safe": True, "sprint_candidate": True}
    )

    assert valid.sprint_candidate is True
    with pytest.raises(ValidationError):
        StoryFact.model_validate(
            {**payload, "dependency_safe": False, "sprint_candidate": True}
        )


def test_selected_story_waits_for_current_dependency_review_and_reconciliation(
    engine: Engine,
) -> None:
    """Preserve selected intent while evidence and dependency confirmation stale."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    _select_story_for_sprint(engine, story_id)

    with Session(engine) as session:
        selected = WorkflowFactRepository(session).load(project_id).stories[0]
    assert selected.structurally_eligible is True
    assert selected.sprint_selection_state == "selected"
    assert selected.dependency_safe is False
    assert selected.sprint_candidate is False
    assert selected.selected_scope_fingerprint is not None
    assert selected.selected_scope_fingerprint.startswith("sha256:")

    _apply_current_dependencies(
        engine,
        domain,
        project_id,
        idempotency_key="initial-selected-scope-review",
    )
    with Session(engine) as session:
        reviewed = WorkflowFactRepository(session).load(project_id).stories[0]
        row = session.get_one(UserStory, story_id)
        original_scope_fingerprint = reviewed.selected_scope_fingerprint
        row.rank = "102"
        session.add(row)
        session.commit()
    assert reviewed.dependency_safe is True
    assert reviewed.sprint_candidate is True

    with Session(engine) as session:
        stale = WorkflowFactRepository(session).load(project_id).stories[0]
    assert stale.sprint_selection_state == "selected"
    assert stale.structurally_eligible is False
    assert stale.dependency_safe is False
    assert stale.sprint_candidate is False

    _validate(engine, story_id)
    with Session(engine) as session:
        reconciled = WorkflowFactRepository(session).load(project_id).stories[0]
    assert reconciled.sprint_selection_state == "selected"
    assert reconciled.structurally_eligible is True
    assert reconciled.selected_scope_fingerprint != original_scope_fingerprint
    assert reconciled.dependency_safe is False
    assert reconciled.sprint_candidate is False


def test_one_selected_story_progresses_with_unselected_and_unrefined_backlog(
    engine: Engine,
) -> None:
    """Do not require full Story selection or full Backlog refinement."""
    requirements = (
        "Plan the selected slice",
        "Retain an accepted sibling",
        "Leave one Backlog item unrefined",
    )
    project_id = _seed_accepted_backlog(engine, requirements=requirements)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id, requirements=requirements)
    _first_artifact_id, selected_story_id = _record_and_accept_story(
        engine,
        domain,
        project_id,
        requirement=requirements[0],
        spec_item_id="REQ.planning-1",
        idempotency_suffix="-selected-slice",
    )
    _second_artifact_id, sibling_story_id = _record_and_accept_story(
        engine,
        domain,
        project_id,
        requirement=requirements[1],
        spec_item_id="REQ.planning-2",
        idempotency_suffix="-unselected-sibling",
    )
    _select_story_for_sprint(engine, selected_story_id)
    _apply_current_dependencies(
        engine,
        domain,
        project_id,
        idempotency_key="partial-refinement-dependencies",
    )

    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)

    by_id = {story.story_id: story for story in snapshot.stories}
    assert by_id[selected_story_id].sprint_candidate is True
    assert by_id[sibling_story_id].sprint_selection_state == "unselected"
    assert by_id[sibling_story_id].sprint_candidate is False
    expected_refined_story_count = 2
    assert len(snapshot.stories) == expected_refined_story_count


def test_sprint_input_story_ids_are_exact_candidate_scope_guard(engine: Engine) -> None:
    """Reject omitted request IDs instead of auto-adding durable candidates."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    _select_story_for_sprint(engine, story_id)
    _apply_current_dependencies(
        engine,
        domain,
        project_id,
        idempotency_key="exact-generation-scope-dependencies",
    )
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "planning.sprint.plan"
        and item.category is NodeCategory.AVAILABLE
    )
    service = application.SprintPlanningInputService(engine=engine)

    omitted = service.build(
        project_id=project_id,
        decision=decision,
        request=application.SprintPlanningRequest(
            project_id=project_id,
            selected_story_ids=(),
            max_story_points=8,
            team_name="Exact Scope Team",
            idempotency_key="omitted-generation-scope",
            actor="operator@example.com",
        ),
    )
    exact = service.build(
        project_id=project_id,
        decision=decision,
        request=application.SprintPlanningRequest(
            project_id=project_id,
            selected_story_ids=(story_id,),
            max_story_points=8,
            team_name="Exact Scope Team",
            idempotency_key="exact-generation-scope",
            actor="operator@example.com",
        ),
    )

    assert isinstance(omitted, WorkflowError)
    assert omitted.blockers[0].code == "SPRINT_CANDIDATE_SET_STALE"
    assert isinstance(exact, dict)
    assert exact["locked_story_ids"] == [story_id]


def _apply_validation_evidence_case(
    story: UserStory,
    evidence: ValidationEvidence,
    evidence_case: str,
) -> None:
    if evidence_case == "missing":
        story.validation_evidence = None
    elif evidence_case == "v1":
        story.validation_evidence = '{"spec_version_id":1,"passed":true}'
    elif evidence_case == "malformed":
        story.validation_evidence = "not-json"
    elif evidence_case == "failed":
        story.validation_evidence = canonical_json(
            ValidationEvidence.model_validate(
                {
                    **evidence.model_dump(mode="json"),
                    "structurally_eligible": False,
                    "structural_failures": [
                        StructuralValidationFailure(
                            code="STORY_STATEMENT_INVALID",
                            message="Story statement is invalid.",
                        ).model_dump(mode="json")
                    ],
                }
            ).model_dump(mode="json")
        )
    elif evidence_case == "fingerprint_stale":
        story.validation_evidence = canonical_json(
            ValidationEvidence.model_validate(
                {
                    **evidence.model_dump(mode="json"),
                    "story_validation_input_fingerprint": "sha256:" + ("f" * 64),
                }
            ).model_dump(mode="json")
        )
    elif evidence_case == "points_stale":
        assert story.story_points is not None
        story.story_points += 1
    elif evidence_case == "rank_stale":
        story.rank = "999"


@pytest.mark.parametrize(
    "evidence_case",
    [
        "current_v3",
        "missing",
        "v1",
        "malformed",
        "failed",
        "fingerprint_stale",
        "points_stale",
        "rank_stale",
        "root_stale",
    ],
)
def test_sprint_input_paid_boundary_rechecks_complete_validation_matrix(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    evidence_case: str,
) -> None:
    """Reject every invalid evidence state before the Sprint adapter can run."""
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _artifact_id, story_id = _record_and_accept_story(engine, domain, project_id)
    _validate(engine, story_id)
    _select_story_for_sprint(engine, story_id)
    _apply_current_dependencies(
        engine,
        domain,
        project_id,
        idempotency_key="validated-sprint-boundary",
    )
    position = domain.position(project_id)
    assert any(item.node_id == "planning.sprint.plan" for item in position.decisions)
    with Session(engine) as session:
        frozen_snapshot = WorkflowFactRepository(session).load(project_id)
        story = session.get(UserStory, story_id)
        assert story is not None
        assert story.validation_evidence is not None
        evidence = ValidationEvidence.model_validate_json(
            story.validation_evidence,
            strict=True,
        )
        _apply_validation_evidence_case(story, evidence, evidence_case)
        session.commit()
    if evidence_case == "root_stale":
        _replace_specification_and_backlog(engine, project_id)

    monkeypatch.setattr(
        WorkflowFactRepository,
        "load",
        lambda _self, _project_id: frozen_snapshot,
    )
    paid_sprint_adapter = Mock(return_value=object())
    app = cast(
        "application.AgileForgeApplication",
        SimpleNamespace(
            _sprint_planning_input=application.SprintPlanningInputService(
                engine=engine
            ),
            position=lambda **_kwargs: position,
            run_agentic_action=paid_sprint_adapter,
        ),
    )
    result = application.AgileForgeApplication.generate_sprint(
        app,
        application.SprintPlanningRequest(
            project_id=project_id,
            selected_story_ids=(story_id,),
            max_story_points=8,
            team_name="Validated Team",
            idempotency_key=f"{evidence_case}-validation-input",
            actor="operator@example.com",
        ),
    )
    if evidence_case == "current_v3":
        paid_sprint_adapter.assert_called_once()
        return
    assert result.ok is False
    assert result.error is not None
    expected_code = (
        "STALE_SPECIFICATION"
        if evidence_case == "root_stale"
        else "SPRINT_STORY_VALIDATION_STALE"
    )
    assert result.error.blockers[0].code == expected_code
    paid_sprint_adapter.assert_not_called()
